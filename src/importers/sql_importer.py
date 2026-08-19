"""
SqlImporter — phase 6: warehouses → legacy queries → legacy alerts → alerts v2 (Plan 3 §6, §6d).

Warehouses go FIRST because everything downstream — queries, alerts, DLT, Lakeview dashboards and
Genie spaces — carries a `warehouse_id` that must be remapped to the target's.

**Legacy SQL dashboards are NEVER attempted** (D10/§6d). `POST /api/2.0/preview/sql/dashboards` — the
old Redash-style create — no longer works on modern workspaces (verified live while building the fvm1
fixtures, which is why no live fixture for one exists). Read/list still work, so they inventory and
export fine; only creation is gone. Attempting it would produce a permanent red failure on every run
forever, which trains the operator to ignore red — so each is recorded `manual` with enough detail to
rebuild it as an AI/BI dashboard. Their underlying `legacy_query` objects still migrate, so only the
visual layout is hand-rebuilt. Export already marks them `manual`, so the base class handles it and
this importer has no dashboard code path at all.
"""
from __future__ import annotations

from typing import Optional

from src.importers.base_importer import BaseImporter, UnsupportedOperation
from src.utils.helpers import safe_str

# The v1 alert `op` vocabulary. The modern `condition.op` uses words; v1 wants symbols.
_V1_OPS = {"GREATER_THAN": ">", "GREATER_THAN_OR_EQUAL": ">=", "LESS_THAN": "<",
           "LESS_THAN_OR_EQUAL": "<=", "EQUAL": "==", "NOT_EQUAL": "!="}


def _legacy_alert_options(condition) -> Optional[dict]:
    """Translate a modern `condition` block into the v1 `options` a legacy create requires.

    Returns None when it does not map cleanly — the caller then reports a manual rebuild rather than
    guessing at an alert's trigger, which is the kind of thing that must not be approximated.
    """
    if not isinstance(condition, dict):
        return None
    op = _V1_OPS.get(safe_str(condition.get("op")))
    column = ((condition.get("operand") or {}).get("column") or {}).get("name")
    threshold = ((condition.get("threshold") or {}).get("value") or {})
    value = next((threshold[k] for k in ("string_value", "double_value", "bool_value")
                  if k in threshold), None)
    if not op or not column or value is None:
        return None
    return {"column": safe_str(column), "op": op, "value": safe_str(value)}


class SqlImporter(BaseImporter):
    component = "sql"
    asset_types = ("sql_warehouse", "legacy_query", "legacy_alert", "alert_v2",
                   "legacy_dashboard")

    def load(self) -> list[dict]:
        """Warehouses → queries → alerts. Legacy dashboards ride along as `manual` units."""
        return self.units_for("sql_warehouse", "legacy_query", "legacy_alert", "alert_v2",
                              "legacy_dashboard")

    def existing_keys(self) -> dict:
        """`{name: id}` per SQL asset type, published for the phases that remap onto them."""
        out: dict = {}

        warehouses = (self.client.get("api/2.0/sql/warehouses") or {}).get("warehouses") or []
        found = {safe_str(w.get("name")): safe_str(w.get("id"))
                 for w in warehouses if w.get("name")}
        self.context.setdefault("sql_warehouse_target_ids", {}).update(found)
        out.update(found)

        # Queries and alerts are cursor APIs, and a truncated list here means a DUPLICATE query on
        # every re-run — so both go through the paginating helper rather than a bare get.
        queries = self.client.get_paginated("api/2.0/sql/queries", "results",
                                            params={"page_size": 100})
        q_found = {safe_str(q.get("display_name") or q.get("name")): safe_str(q.get("id"))
                   for q in queries if (q.get("display_name") or q.get("name"))}
        self.context.setdefault("legacy_query_target_ids", {}).update(q_found)
        out.update(q_found)

        alerts = self.client.get_paginated("api/2.0/alerts", "results", params={"page_size": 100})
        a_found = {safe_str(a.get("display_name") or a.get("name")): safe_str(a.get("id"))
                   for a in alerts if (a.get("display_name") or a.get("name"))}
        self.context.setdefault("alert_v2_target_ids", {}).update(a_found)
        out.update(a_found)

        return out

    # ── create ────────────────────────────────────────────────────────────
    def create_one(self, unit: dict) -> dict:
        asset_type = safe_str(unit.get("asset_type"))
        if asset_type == "sql_warehouse":
            return self._create_warehouse(unit)
        if asset_type == "legacy_query":
            return self._create_query(unit)
        if asset_type == "legacy_alert":
            return self._create_legacy_alert(unit)
        if asset_type == "alert_v2":
            return self._create_alert_v2(unit)
        raise RuntimeError(f"sql importer got an unexpected asset_type {asset_type!r}")

    def update_one(self, unit: dict, target_id: str) -> dict:
        """The edit APIs — note each SQL asset uses a DIFFERENT shape."""
        asset_type = safe_str(unit.get("asset_type"))
        payload = dict(unit.get("payload") or {})
        if asset_type == "sql_warehouse":
            # Warehouses edit via `/{id}/edit`, not a PUT on the collection.
            self.client.post(f"api/2.0/sql/warehouses/{target_id}/edit",
                             self._warehouse_body(payload, unit))
            return {"target_id": target_id}
        if asset_type == "legacy_query":
            self.client.post(f"api/2.0/sql/queries/{target_id}",
                             {"query": self._query_body(payload)})
            return {"target_id": target_id}
        if asset_type == "legacy_alert":
            self.client.put(f"api/2.0/sql/alerts/{target_id}", self._legacy_alert_body(payload))
            return {"target_id": target_id}
        if asset_type == "alert_v2":
            self.client.patch(f"api/2.0/alerts/{target_id}", self._alert_v2_body(payload))
            return {"target_id": target_id}
        return {"target_id": target_id}

    # ── warehouses ────────────────────────────────────────────────────────
    def _create_warehouse(self, unit: dict) -> dict:
        body = self._warehouse_body(unit.get("payload") or {}, unit)
        created = self.client.post("api/2.0/sql/warehouses", body)
        wh_id = safe_str(created.get("id"))
        self.context.setdefault("sql_warehouse_target_ids", {})[self.natural_key(unit)] = wh_id
        return {"target_id": wh_id,
                "note": "same cloud/region, so warehouse_type and size are kept verbatim"}

    def _warehouse_body(self, payload: dict, unit: dict) -> dict:
        body = dict(payload)
        body["name"] = safe_str(body.get("name")) or self.natural_key(unit)
        # `creator_name` names a SOURCE identity: the target attributes the warehouse to the caller,
        # and an unknown name is rejected outright.
        body.pop("creator_name", None)
        return body

    # ── legacy queries ────────────────────────────────────────────────────
    def _create_query(self, unit: dict) -> dict:
        body = self._query_body(unit.get("payload") or {})
        try:
            created = self.client.post("api/2.0/sql/queries", {"query": body})
        except Exception as exc:  # noqa: BLE001
            self.missing_parent_prerequisite(exc, body.get("parent_path"), self.natural_key(unit))
            raise
        qid = safe_str(created.get("id"))
        self.context.setdefault("legacy_query_target_ids", {})[self.natural_key(unit)] = qid
        parent = safe_str(body.get("parent_path"))
        return {"target_id": qid, "note": f"created in {parent}" if parent else ""}

    def _query_body(self, payload: dict) -> dict:
        body = dict(payload)
        # PLAN 8 Bug 7: PRESERVE + remap `parent_path` so the query lands in its SOURCE folder. It
        # used to be popped, which dropped every query at the workspace root (the object was
        # `Created` but never appeared in the user's/target directory tree).
        self.remap_parent_path(body)
        self._remap_warehouse(body)
        return body

    # ── alerts ────────────────────────────────────────────────────────────
    def _create_legacy_alert(self, unit: dict) -> dict:
        """Create a LEGACY (v1) SQL alert — which needs the legacy `options` shape.

        Verified live: `POST /api/2.0/sql/alerts` rejects the modern payload with "Missing alert
        definition". The v1 create wants a flat `options{column, op, value}`, but the current LIST/GET
        surface returns the NEW `condition{op, operand, threshold}` shape instead — so the exported
        payload cannot be posted back as-is. The condition is translated where it maps cleanly, and
        anything that doesn't is reported as a manual rebuild rather than an opaque 400.
        """
        payload = self._legacy_alert_body(unit.get("payload") or {})
        if "options" not in payload:
            options = _legacy_alert_options(payload.get("condition"))
            if options is None:
                raise UnsupportedOperation(
                    f"legacy alert `{self.natural_key(unit)}` cannot be recreated: the v1 create API "
                    f"requires the old flat `options{{column, op, value}}` shape, but the current "
                    f"read API only returns the newer `condition` shape, and this alert's condition "
                    f"does not translate cleanly. Rebuild it on target as an Alerts V2 alert (the "
                    f"underlying query HAS migrated), or recreate it by hand.")
            payload["options"] = options
            payload.pop("condition", None)
        created = self.client.post("api/2.0/sql/alerts", payload)
        return {"target_id": safe_str(created.get("id")),
                "note": "legacy (v1) alert — its `condition` was translated to the v1 `options` shape"}

    def _legacy_alert_body(self, payload: dict) -> dict:
        """Remap the alert's QUERY id — an alert holding a source query id is inert on target."""
        body = dict(payload)
        src_query = safe_str(body.get("query_id"))
        if src_query:
            target_id, key = self.remap_id("legacy_query", src_query)
            if target_id:
                body["query_id"] = target_id
            else:
                self.result.warnings.append(
                    f"legacy alert references source query {src_query!r}"
                    + (f" ({key!r})" if key else "")
                    + " which has no target equivalent — import the sql family first, then re-run "
                      "with retry_mode=failed_only")
        self._remap_warehouse(body)
        return body

    def _create_alert_v2(self, unit: dict) -> dict:
        # Verified against the SDK's `create_alert`: /api/2.0/alerts takes the AlertV2 body FLAT,
        # NOT wrapped in {"alert": ...} the way legacy queries are.
        body = self._alert_v2_body(unit.get("payload") or {})
        try:
            created = self.client.post("api/2.0/alerts", body)
        except Exception as exc:  # noqa: BLE001
            self.missing_parent_prerequisite(exc, body.get("parent_path"), self.natural_key(unit))
            raise
        aid = safe_str(created.get("id"))
        self.context.setdefault("alert_v2_target_ids", {})[self.natural_key(unit)] = aid
        return {"target_id": aid}

    def _alert_v2_body(self, payload: dict) -> dict:
        body = dict(payload)
        # PLAN 8 Bug 7: keep + remap `parent_path` (was popped) so the alert lands in its folder.
        # PLAN 8 Bug 10: `evaluation` + `schedule` (both REQUIRED by create) now travel because the
        # collector enriches the shallow LIST via GET-by-id — nothing to add here beyond remaps.
        self.remap_parent_path(body)
        self._remap_warehouse(body)
        return body

    # ── shared ────────────────────────────────────────────────────────────
    def _remap_warehouse(self, body: dict) -> None:
        """Remap `warehouse_id` (and the legacy `data_source_id` spelling) onto a target warehouse.

        A stale source warehouse id leaves the object existing but unable to run, so an unresolvable
        one is never left in place silently. Falling back to an existing target warehouse keeps the
        object RUNNABLE, which is more useful than a query that errors on open — and the substitution
        is reported so the operator can re-point it deliberately.
        """
        for field in ("warehouse_id", "data_source_id"):
            src = safe_str(body.get(field))
            if not src:
                continue
            target_id, key = self.remap_id("sql_warehouse", src)
            if target_id:
                body[field] = target_id
                continue
            fallback = next(iter((self.context.get("sql_warehouse_target_ids") or {}).values()), "")
            if fallback:
                body[field] = fallback
                self.result.warnings.append(
                    f"{field} pointed at source warehouse {src!r}"
                    + (f" ({key!r})" if key else "")
                    + f", which is not on target, so it was pointed at an existing warehouse "
                      f"({fallback}) to keep the object runnable — re-point it if that is not the "
                      f"warehouse you want.")
            else:
                body.pop(field, None)
                self.result.warnings.append(
                    f"{field}={src!r} could not be remapped and the target has NO warehouse at all "
                    f"— the object was created without one and will not run until you attach one.")
