"""
MiscCollector — global init scripts, cluster libraries, IP access lists, workspace conf.

PATs/tokens are EXCLUDED (disabled in the customer workspace). Each sub-fetch is independent
and best-effort so one failing (e.g. a feature disabled) never drops the others.
natural_key varies by misc_type (script name / list label / conf key / cluster+library).
"""
from __future__ import annotations

import json

from src.collectors.base_collector import BaseCollector
from src.utils.helpers import safe_str

# workspace-conf keys we read + carry (documented default set; operator can trim). master §11.
_WS_CONF_KEYS = [
    "enableTokensConfig", "maxTokenLifetimeDays", "enableIpAccessLists",
    "enableExportNotebook", "enableResultsDownloading", "enableWebTerminal",
    "enableDbfsFileBrowser", "enableUploadDataUis",
    "storeInteractiveNotebookResultsInCustomerAccount",
]


class MiscCollector(BaseCollector):
    object_type = "misc"

    def natural_key(self, obj: dict) -> str:
        return safe_str(obj.get("_natural_key"))

    def discover(self) -> list[dict]:
        out: list[dict] = []
        out.extend(self._global_init_scripts())
        out.extend(self._cluster_libraries())
        out.extend(self._ip_access_lists())
        out.extend(self._workspace_conf())
        return out

    def _global_init_scripts(self) -> list[dict]:
        try:
            raw = self.client.get("api/2.0/global-init-scripts").get("scripts", []) or []
        except Exception as exc:  # noqa: BLE001
            self.log.warning("global-init-scripts failed", error=str(exc))
            return []
        items = []
        for s in raw:
            sid = safe_str(s.get("script_id"))
            script = {}
            try:
                script = self.client.get(f"api/2.0/global-init-scripts/{sid}") or {}
            except Exception as exc:  # noqa: BLE001
                self.log.warning("gis detail failed", script_id=sid, error=str(exc))
            items.append({
                "misc_type": "global_init_script",
                "script_id": sid,
                "name": safe_str(s.get("name")),
                "_natural_key": safe_str(s.get("name")),
                "position": s.get("position"),
                "enabled": s.get("enabled"),
                "script_b64": script.get("script"),   # base64 body from detail
                "_raw": s,
            })
        return items

    def _cluster_libraries(self) -> list[dict]:
        try:
            raw = self.client.get("api/2.0/libraries/all-cluster-statuses").get("statuses", []) or []
        except Exception as exc:  # noqa: BLE001
            self.log.warning("all-cluster-statuses failed", error=str(exc))
            return []
        items = []
        for st in raw:
            cid = safe_str(st.get("cluster_id"))
            for ls in st.get("library_statuses", []) or []:
                lib = ls.get("library", {}) or {}
                items.append({
                    "misc_type": "cluster_library",
                    "cluster_id": cid,
                    "library": lib,
                    "_natural_key": f"{cid}:{json.dumps(lib, sort_keys=True)}",
                    "status": safe_str(ls.get("status")),
                })
        return items

    def _ip_access_lists(self) -> list[dict]:
        try:
            raw = self.client.get("api/2.0/ip-access-lists").get("ip_access_lists", []) or []
        except Exception as exc:  # noqa: BLE001
            self.log.warning("ip-access-lists failed", error=str(exc))
            return []
        items = []
        for l in raw:
            items.append({
                "misc_type": "ip_access_list",
                "list_id": safe_str(l.get("list_id")),
                "label": safe_str(l.get("label")),
                "_natural_key": safe_str(l.get("label")),
                "list_type": safe_str(l.get("list_type")),
                "ip_addresses": l.get("ip_addresses", []),
                "enabled": l.get("enabled"),
                "_raw": l,
            })
        return items

    def _workspace_conf(self) -> list[dict]:
        items = []
        for key in _WS_CONF_KEYS:
            try:
                data = self.client.get("api/2.0/workspace-conf", params={"keys": key})
                val = data.get(key) if isinstance(data, dict) else None
            except Exception as exc:  # noqa: BLE001
                self.log.warning("workspace-conf failed", key=key, error=str(exc))
                continue
            if val is not None:
                items.append({
                    "misc_type": "workspace_conf",
                    "key": key,
                    "_natural_key": key,
                    "value": val,
                })
        return items
