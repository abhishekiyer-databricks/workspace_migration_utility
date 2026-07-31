"""
GenieCollector — Genie spaces (SOURCE workspace).

Captures title, description, warehouse_id (for target warehouse remap) and ACLs. natural_key
= title. (Migration approach for Genie is being defined with the customer — no migratability
flag is asserted here.)
"""
from __future__ import annotations

from src.collectors.base_collector import BaseCollector
from src.utils.helpers import dab_path_info, safe_str


class GenieCollector(BaseCollector):
    object_type = "genie_space"

    def natural_key(self, obj: dict) -> str:
        return safe_str(obj.get("title"))

    def discover(self) -> list[dict]:
        raw = self.client.get_paginated(
            "api/2.0/genie/spaces", "spaces",
            token_key="next_page_token", params={"page_size": 100},
        )
        items = []
        for s in raw:
            sid = safe_str(s.get("space_id") or s.get("id"))
            # Genie spaces expose no deployment field; the list carries only `parent_path`
            # (coarser than dashboards' `path`), so DAB detection keys off that `.bundle/` folder.
            dab = dab_path_info(s.get("parent_path"))
            items.append({
                "space_id": sid,
                "title": safe_str(s.get("title")),
                "description": safe_str(s.get("description")),
                "warehouse_id": safe_str(s.get("warehouse_id")),
                "parent_path": safe_str(s.get("parent_path")),
                "deployed_by_dab": dab["deployed_by_dab"],
                "dab_scope": dab["dab_scope"],
                "acl": self.fetch_acl("genie", sid),   # ACLs (Plan 1a §1)
                "_raw": s,
            })
        return items
