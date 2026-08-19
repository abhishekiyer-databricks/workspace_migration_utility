"""
GenieImporter — phase 9: Genie spaces (Plan 3 §6).

**Genie spaces ARE auto-migratable** — verified live on fvm1 2026-08-01. This supersedes the older
"serialized_space is an un-exportable protobuf" belief that earlier plans carried: the current API
returns the full `serialized_space` JSON on
`GET /api/2.0/genie/spaces/{id}?include_serialized_space=true`, and it round-trips through
`create_space` / `update_space`.

Same shape as Lakeview: the serialized payload is carried VERBATIM, only `warehouse_id` is remapped,
and the one real caveat is that `serialized_space` pins UC tables by fully-qualified name. UC is out
of scope, so those tables must pre-exist on target — a space can create successfully and still be
unusable until they do, which is why the note says so explicitly.
"""
from __future__ import annotations

from src.importers.base_importer import BaseImporter
from src.utils.helpers import safe_str


class GenieImporter(BaseImporter):
    component = "genie"
    asset_types = ("genie_space",)

    def load(self) -> list[dict]:
        return self.units_for("genie_space")

    def existing_keys(self) -> dict:
        """`{title: space_id}` for spaces already on target."""
        spaces = self.client.get_paginated("api/2.0/genie/spaces", "spaces",
                                           params={"page_size": 100})
        found = {safe_str(s.get("title")): safe_str(s.get("space_id"))
                 for s in spaces if s.get("title")}
        self.context.setdefault("genie_space_target_ids", {}).update(found)
        return found

    def create_one(self, unit: dict) -> dict:
        body, note = self._body(unit)
        # PLAN 8 Bug 7 (Genie sibling): recreate the space in its SOURCE folder, not the caller's
        # home. Verified live that Genie create HONORS parent_path. Only on CREATE — an update
        # doesn't move an existing space.
        parent = safe_str((unit.get("payload") or {}).get("parent_path"))
        if parent:
            body["parent_path"] = parent
            self.remap_parent_path(body)
        try:
            created = self.client.post("api/2.0/genie/spaces", body)
        except Exception as exc:  # noqa: BLE001
            self.missing_parent_prerequisite(exc, body.get("parent_path"), self.natural_key(unit))
            raise
        sid = safe_str(created.get("space_id"))
        self.context.setdefault("genie_space_target_ids", {})[self.natural_key(unit)] = sid
        return {"target_id": sid, "note": note}

    def update_one(self, unit: dict, target_id: str) -> dict:
        """`update_space` — PATCH on the space, same body shape as create."""
        body, note = self._body(unit)
        self.client.patch(f"api/2.0/genie/spaces/{target_id}", body)
        return {"target_id": target_id, "note": note}

    def _body(self, unit: dict) -> tuple[dict, str]:
        payload = dict(unit.get("payload") or {})
        body = {"title": safe_str(payload.get("title")) or self.natural_key(unit)}
        if payload.get("description"):
            body["description"] = payload["description"]
        # Verbatim: the API round-trips exactly what it emitted, and rewriting it risks corrupting a
        # schema we don't own.
        if payload.get("serialized_space") is not None:
            body["serialized_space"] = payload["serialized_space"]

        notes = []
        src_wh = safe_str(payload.get("warehouse_id"))
        if src_wh:
            target_wh, key = self.remap_id("sql_warehouse", src_wh)
            if not target_wh:
                target_wh = next(
                    iter((self.context.get("sql_warehouse_target_ids") or {}).values()), "")
                if target_wh:
                    notes.append(f"source warehouse {src_wh!r}"
                                 + (f" ({key!r})" if key else "")
                                 + f" is not on target, so the space was attached to an existing "
                                   f"warehouse ({target_wh}) — re-point it if that is wrong")
            if target_wh:
                body["warehouse_id"] = target_wh

        notes.append("serialized_space carried verbatim. NOTE it references Unity Catalog tables by "
                     "fully-qualified name, and UC is OUT OF SCOPE for this utility — the space "
                     "cannot answer questions until those tables exist on target.")
        return body, " ".join(notes)
