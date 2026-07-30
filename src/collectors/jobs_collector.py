"""
JobsCollector — jobs / workflows (SOURCE workspace).

Uses API 2.1 with `expand_tasks=true` so MULTI_TASK jobs include their `tasks` (a 2.0-only
list drops them — migrate review, master §10a). Cursor-paginated. Captures ACLs and whether
an IS_OWNER grant exists (jobs without one are malformed — flagged for the import side).
natural_key = job name (duplicate names are possible; Export/Import handle the `name:::id`
suffix trick, so inventory records both name and id).
"""
from __future__ import annotations

from src.collectors.base_collector import BaseCollector
from src.utils.helpers import safe_str


class JobsCollector(BaseCollector):
    object_type = "job"

    def natural_key(self, obj: dict) -> str:
        return safe_str(obj.get("name"))

    def discover(self) -> list[dict]:
        raw = self.client.get_paginated(
            "api/2.1/jobs/list", "jobs",
            token_key="next_page_token",
            params={"limit": 25, "expand_tasks": "true"},
        )
        items = []
        for j in raw:
            settings = j.get("settings", {}) or {}
            job_id = safe_str(j.get("job_id"))
            acl = self.fetch_acl("jobs", job_id)
            items.append({
                "job_id": job_id,
                "name": safe_str(settings.get("name")),
                "format": safe_str(settings.get("format")),   # SINGLE_TASK | MULTI_TASK
                "creator_user_name": safe_str(j.get("creator_user_name")),
                "run_as": settings.get("run_as") or settings.get("run_as_user_name"),
                "has_owner_acl": self._has_owner(acl),
                # DAB-deployed jobs carry settings.deployment.kind == "BUNDLE" → should be
                # redeployed to the target via their bundle, not migrated by this tool (Plan 1a §4).
                "deployed_by_dab": self._is_dab(settings),
                "acl": acl,
                "settings": settings,   # full spec (tasks, job_clusters, schedule, continuous)
                "_raw": j,
            })
        return items

    @staticmethod
    def _is_dab(settings: dict) -> bool:
        dep = settings.get("deployment") or {}
        return safe_str(dep.get("kind")) == "BUNDLE"

    @staticmethod
    def _has_owner(acl) -> bool:
        for entry in acl or []:
            for p in entry.get("all_permissions", []):
                if p.get("permission_level") == "IS_OWNER":
                    return True
        return False
