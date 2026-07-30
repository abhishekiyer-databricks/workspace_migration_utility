"""
GenieCollector — Genie spaces (SOURCE workspace).

Metadata only: `serialized_space` is an internal protobuf NOT exposed by GET, so auto-create
is impossible → import emits manual-recreation instructions (master §6). We capture title,
description, warehouse_id (for target warehouse remap) and the backing Lakeview dashboard's
serialized content for best-effort manual recreation. natural_key = title.
"""
from __future__ import annotations

from src.collectors.base_collector import BaseCollector
from src.utils.helpers import safe_str


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
            items.append({
                "space_id": safe_str(s.get("space_id") or s.get("id")),
                "title": safe_str(s.get("title")),
                "description": safe_str(s.get("description")),
                "warehouse_id": safe_str(s.get("warehouse_id")),
                "migratable": False,   # serialized_space not exportable → manual
                "_raw": s,
            })
        return items
