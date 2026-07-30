"""
DashboardsCollector — AI/BI (Lakeview) dashboards (SOURCE workspace).

List is cursor-paginated (/api/2.0/lakeview/dashboards); per-dashboard detail carries
`serialized_dashboard` + `warehouse_id` needed to recreate. natural_key = display_name.
"""
from __future__ import annotations

from src.collectors.base_collector import BaseCollector
from src.utils.helpers import safe_str


class DashboardsCollector(BaseCollector):
    object_type = "lakeview_dashboard"

    def natural_key(self, obj: dict) -> str:
        return safe_str(obj.get("display_name"))

    def discover(self) -> list[dict]:
        raw = self.client.get_paginated(
            "api/2.0/lakeview/dashboards", "dashboards",
            token_key="next_page_token", params={"page_size": 100},
        )
        items = []
        for d in raw:
            did = safe_str(d.get("dashboard_id"))
            full = {}
            try:
                full = self.client.get(f"api/2.0/lakeview/dashboards/{did}") or {}
            except Exception as exc:  # noqa: BLE001
                self.log.warning("dashboard detail failed", dashboard_id=did, error=str(exc))
            items.append({
                "dashboard_id": did,
                "display_name": safe_str(full.get("display_name") or d.get("display_name")),
                "warehouse_id": safe_str(full.get("warehouse_id")),
                "parent_path": safe_str(full.get("parent_path")),
                "serialized_dashboard": full.get("serialized_dashboard"),
                "_raw": d,
            })
        return items
