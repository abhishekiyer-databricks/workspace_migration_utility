"""
WorkspaceCollector — directories, notebooks, files, repos + object ACLs (SOURCE workspace).

Recursive walk of /api/2.0/workspace/list with the special-path rules from the migrate review
(master §10a): `/Repos` handled separately (repo pointers, not notebooks); Trash skipped;
user-home roots noted (can't be mkdir'd on target — user must exist first). Inventory records
paths, object types, language, and ACLs; the notebook/file BYTES are pulled in Export (Plan 2),
not here. `max_ws_api_calls` caps directory traversal on very deep trees (safety, like the
inventory script). natural_key = workspace path.
"""
from __future__ import annotations

from src.collectors.base_collector import BaseCollector
from src.utils.helpers import safe_str

# Top-level paths NOT walked as ordinary dirs (handled specially / excluded).
_SKIP_TOP = ("/Repos", "/Projects")


class WorkspaceCollector(BaseCollector):
    object_type = "workspace_object"

    def natural_key(self, obj: dict) -> str:
        return safe_str(obj.get("path"))

    def discover(self) -> list[dict]:
        self._api_calls = 0
        self._max_calls = int(getattr(self.config, "max_ws_api_calls", 0) or 0)
        self._max_items = int(getattr(self.config, "max_workspace_items", 0) or 0)
        objs: list[dict] = []
        self._walk("/", objs)
        objs.extend(self._repos())
        return objs

    def _budget_left(self) -> bool:
        return self._max_calls == 0 or self._api_calls < self._max_calls

    @staticmethod
    def _is_trash(path: str) -> bool:
        return path.startswith("/Users/") and "/Trash" in path

    @staticmethod
    def _is_user_root(path: str) -> bool:
        # /Users/<email> — home root; target creates it when the user exists.
        parts = [p for p in path.split("/") if p]
        return len(parts) == 2 and parts[0] == "Users"

    def _walk(self, path: str, out: list[dict]) -> None:
        if not self._budget_left():
            self.client.warnings.append(
                f"workspace/list: hit max_ws_api_calls={self._max_calls} — tree traversal truncated")
            return
        if self._max_items and len(out) >= self._max_items:
            return
        self._api_calls += 1
        try:
            data = self.client.get("api/2.0/workspace/list", params={"path": path})
        except Exception as exc:  # noqa: BLE001
            self.log.warning("workspace/list failed", path=path, error=str(exc))
            return
        for obj in data.get("objects", []) or []:
            p = safe_str(obj.get("path"))
            otype = safe_str(obj.get("object_type"))  # DIRECTORY | NOTEBOOK | FILE | REPO | LIBRARY
            if any(p == t or p.startswith(t + "/") for t in _SKIP_TOP) or self._is_trash(p):
                continue
            if otype == "REPO":
                continue  # repos come from the /repos API
            record = {
                "path": p,
                "object_type": otype,
                "language": safe_str(obj.get("language")),
                "object_id": safe_str(obj.get("object_id")),
                "is_user_root": self._is_user_root(p),
                "acl": self._object_acl(otype, obj.get("object_id")),
            }
            out.append(record)
            if otype == "DIRECTORY":
                self._walk(p, out)

    def _object_acl(self, otype: str, object_id) -> list | None:
        # /Shared ACL is immutable on target (handled at import); still inventory others.
        perm_type = {"NOTEBOOK": "notebooks", "DIRECTORY": "directories"}.get(otype)
        if not perm_type:
            return None
        return self.fetch_acl(perm_type, object_id)

    def _repos(self) -> list[dict]:
        """Union /Repos and /Workspace path_prefix to catch legacy + modern git folders."""
        seen, repos = set(), []
        for extra in ({}, {"path_prefix": "/Workspace"}):
            try:
                raw = self.client.get_paginated("api/2.0/repos", "repos",
                                                token_key="next_page_token", params=extra)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("repos list failed", extra=str(extra), error=str(exc))
                continue
            for r in raw:
                rid = safe_str(r.get("id") or r.get("path"))
                if rid in seen:
                    continue
                seen.add(rid)
                repos.append({
                    "path": safe_str(r.get("path")),
                    "object_type": "REPO",
                    "repo_id": safe_str(r.get("id")),
                    "url": safe_str(r.get("url")),
                    "branch": safe_str(r.get("branch")),
                    "acl": self.fetch_acl("repos", r.get("id")),
                    "_raw": r,
                })
        return repos
