"""
ServingCollector — model serving endpoints (SOURCE workspace).

Skips platform-managed `databricks-*` endpoints (not user-owned). Also skips **Agent Bricks
agent endpoints** (`task=agent/*`, e.g. Multi-Agent Supervisor `mas-*`): they are NOT
recreatable via workspace REST — a deployed agent is backed by a UC-registered MLflow
ResponsesAgent model plus UC volumes/tables/functions/indexes and UI-only orchestration
metadata, all outside this non-UC workspace utility's scope. Since the import side can't stand
one up on the target, we don't inventory them. natural_key = endpoint name.
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
            if str(e.get("task") or "").startswith("agent/"):
                continue  # Agent Bricks agent — not recreatable via workspace REST (see docstring)
            items.append({
                "name": name,
                "config": e.get("config", {}),
                "tags": e.get("tags"),
                "acl": self.fetch_acl("serving-endpoints", e.get("id") or name),
                "_raw": e,
            })
        return items
