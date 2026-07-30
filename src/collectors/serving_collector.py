"""
ServingCollector — model serving endpoints (SOURCE workspace).

Skips platform-managed `databricks-*` endpoints (not user-owned). natural_key = endpoint name.
"""
from __future__ import annotations

from src.collectors.base_collector import BaseCollector
from src.utils.helpers import safe_str


class ServingCollector(BaseCollector):
    object_type = "serving_endpoint"

    def natural_key(self, obj: dict) -> str:
        return safe_str(obj.get("name"))

    def discover(self) -> list[dict]:
        raw = self.client.get("api/2.0/serving-endpoints").get("endpoints", []) or []
        items = []
        for e in raw:
            name = safe_str(e.get("name"))
            if name.startswith("databricks-"):
                continue  # platform-managed, not user-owned
            items.append({
                "name": name,
                "config": e.get("config", {}),
                "tags": e.get("tags"),
                "acl": self.fetch_acl("serving-endpoints", e.get("id") or name),
                "_raw": e,
            })
        return items
