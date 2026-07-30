"""
SqlCollector — SQL warehouses + legacy SQL (queries, alerts, dashboards) (SOURCE workspace).

Warehouses: /api/2.0/sql/warehouses (not paginated today — verify). Legacy queries/alerts/
dashboards: cursor-paginated /api/2.0/sql/{queries,alerts,dashboards}. natural_key = name
(warehouse/query/alert) or dashboard name.
"""
from __future__ import annotations

from src.collectors.base_collector import BaseCollector
from src.utils.helpers import safe_str


class SqlCollector(BaseCollector):
    object_type = "sql"

    def natural_key(self, obj: dict) -> str:
        return safe_str(obj.get("_natural_key"))

    # endpoint segment -> (singular sql_type label, permissions-API object type)
    # permissions object types verified live: queries / alerts / dashboards (Plan 1a §1).
    _LEGACY = {"queries": ("legacy_query", "queries"),
               "alerts": ("legacy_alert", "alerts"),
               "dashboards": ("legacy_dashboard", "dashboards")}

    def discover(self) -> list[dict]:
        out: list[dict] = []
        out.extend(self._warehouses())
        for kind in self._LEGACY:
            out.extend(self._legacy(kind, "results"))
        return out

    def _warehouses(self) -> list[dict]:
        raw = self.client.get("api/2.0/sql/warehouses").get("warehouses", []) or []
        items = []
        for w in raw:
            items.append({
                "sql_type": "warehouse",
                "id": safe_str(w.get("id")),
                "name": safe_str(w.get("name")),
                "_natural_key": safe_str(w.get("name")),
                "warehouse_type": safe_str(w.get("warehouse_type")),
                "acl": self.fetch_acl("sql/warehouses", w.get("id")),
                "_raw": w,
            })
        return items

    def _legacy(self, kind: str, result_key: str) -> list[dict]:
        try:
            raw = self.client.get_paginated(
                f"api/2.0/sql/{kind}", result_key,
                token_key="next_page_token", params={"page_size": 100},
            )
        except Exception as exc:  # noqa: BLE001
            # A 404 means the legacy endpoint isn't present on this workspace (deprecated /
            # feature-off) — that's expected, not an error. Log quietly at INFO.
            if "404" in str(exc):
                self.log.info("legacy sql endpoint absent (skipping)", kind=kind)
            else:
                self.log.warning("legacy sql fetch failed", kind=kind, error=str(exc))
            return []
        sql_type, perm_type = self._LEGACY[kind]
        items = []
        for o in raw:
            name = safe_str(o.get("name") or o.get("title"))
            oid = safe_str(o.get("id"))
            items.append({
                "sql_type": sql_type,   # legacy_query / legacy_alert / legacy_dashboard
                "id": oid,
                "name": name,
                "_natural_key": name or oid,
                "acl": self.fetch_acl(perm_type, oid),   # ACLs (Plan 1a §1)
                "_raw": o,
            })
        return items
