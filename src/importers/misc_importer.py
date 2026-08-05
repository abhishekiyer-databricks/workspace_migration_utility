"""
MiscImporter — phase 11: global init scripts → cluster libraries → workspace conf (Plan 3 §6, D6).

Three unrelated assets share a phase because each is small and each depends on something earlier.

**Cluster libraries are DEFERRED by default, and that is a deliberate decision (D6).**
`libraries/install` needs a RUNNING cluster, but the compute phase deliberately STOPS every cluster
right after creating it so the migration doesn't burn the customer's DBUs. Force-starting clusters to
install libraries would silently spend their money, so instead each library is recorded `manual` with
its cluster and spec, and `library_force_start_clusters=true` opts into starting them. Silently
burning DBUs is not a default.

**Workspace conf is written key-by-key, from a documented key set only.** `PATCH workspace-conf`
accepts arbitrary keys, and blanket-writing whatever the source returned could flip a security
setting (token access, IP-list enforcement) on the target. So each key is applied individually — one
rejected key doesn't take the others down with it — and the note names what changed.

**IP access lists are NOT here**: they are account-level for this customer, so a workspace-scoped tool
cannot see or migrate them (master §6a) — a customer/account-admin task, out of scope by decision.
"""
from __future__ import annotations

import json

from src.importers.base_importer import BaseImporter, PrerequisiteMissing
from src.utils.helpers import safe_str

# Workspace-conf keys this tool is willing to write. Anything else is reported rather than applied:
# a conf key can change the workspace's SECURITY posture, and blanket-writing an unknown key from a
# source workspace is not a risk worth taking silently.
KNOWN_CONF_KEYS = frozenset({
    "enableTokensConfig", "maxTokenLifetimeDays", "enableDeprecatedClusterNamedInitScripts",
    "enableDeprecatedGlobalInitScripts", "enableIpAccessLists", "enableProjectTypeInWorkspace",
    "enableWorkspaceFilesystem", "enableDbfsFileBrowser", "enableExportNotebook",
    "enableNotebookTableClipboard", "enableResultsDownloading", "enableUploadDataUis",
    "enableWebTerminal", "enforceUserIsolation", "storeInteractiveNotebookResultsInCustomerAccount",
    "enableVerboseAuditLogs",
})


class MiscImporter(BaseImporter):
    component = "misc"
    asset_types = ("global_init_script", "cluster_library", "workspace_conf")

    # Workspace conf is DECLARATIVE against a workspace that always exists, so an "adopt" must still
    # write the value — otherwise a conf key would never actually be applied.
    declarative_asset_types = ("workspace_conf",)

    def load(self) -> list[dict]:
        """Init scripts → libraries (need clusters) → conf."""
        return self.units_for("global_init_script", "cluster_library", "workspace_conf")

    def existing_keys(self) -> dict:
        out: dict = {}
        scripts = (self.client.get("api/2.0/global-init-scripts") or {}).get("scripts") or []
        found = {safe_str(s.get("name")): safe_str(s.get("script_id"))
                 for s in scripts if s.get("name")}
        self.context.setdefault("global_init_script_target_ids", {}).update(found)
        out.update(found)

        # Conf keys already SET on target. Only the documented keys are probed — asking for an
        # unknown key errors on some workspaces.
        try:
            conf = self.client.get("api/2.0/workspace-conf",
                                   params={"keys": ",".join(sorted(KNOWN_CONF_KEYS))}) or {}
            for key, value in (conf.items() if isinstance(conf, dict) else []):
                out[key] = safe_str(value)
        except Exception as exc:  # noqa: BLE001 — a conf read failure must not stop the phase
            self.log.warning("workspace-conf read failed", error=str(exc)[:200])
        return out

    def create_one(self, unit: dict) -> dict:
        asset_type = safe_str(unit.get("asset_type"))
        if asset_type == "global_init_script":
            return self._create_init_script(unit)
        if asset_type == "cluster_library":
            return self._install_library(unit)
        if asset_type == "workspace_conf":
            return self._set_conf(unit)
        raise RuntimeError(f"misc importer got an unexpected asset_type {asset_type!r}")

    def update_one(self, unit: dict, target_id: str) -> dict:
        asset_type = safe_str(unit.get("asset_type"))
        if asset_type == "global_init_script":
            payload = unit.get("payload") or {}
            self.client.patch(f"api/2.0/global-init-scripts/{target_id}",
                              self._script_body(unit, payload))
            return {"target_id": target_id}
        if asset_type == "workspace_conf":
            return self._set_conf(unit)
        if asset_type == "cluster_library":
            # A changed library spec is a DIFFERENT unit (the spec IS the natural key), so this path
            # only fires for a re-attempt of the same spec — and install is idempotent.
            return self._install_library(unit)
        return {"target_id": target_id}

    # ── global init scripts ───────────────────────────────────────────────
    def _create_init_script(self, unit: dict) -> dict:
        payload = unit.get("payload") or {}
        created = self.client.post("api/2.0/global-init-scripts", self._script_body(unit, payload))
        sid = safe_str(created.get("script_id"))
        self.context.setdefault("global_init_script_target_ids", {})[self.natural_key(unit)] = sid
        enabled = bool(payload.get("enabled"))
        return {"target_id": sid,
                "note": (f"position {payload.get('position')}, "
                         f"{'ENABLED as on source' if enabled else 'disabled as on source'}")}

    def _script_body(self, unit: dict, payload: dict) -> dict:
        """The create body. `script` carries the base64 body exactly as exported."""
        body = {"name": safe_str(payload.get("name")) or self.natural_key(unit),
                "script": payload.get("script_b64") or payload.get("script") or "",
                # The source's enabled state is preserved rather than forced: an init script runs on
                # EVERY cluster launch, so silently enabling one would change cluster behaviour
                # workspace-wide, and silently disabling one would break it.
                "enabled": bool(payload.get("enabled"))}
        if payload.get("position") is not None:
            body["position"] = payload["position"]
        return body

    # ── cluster libraries (deferred by default — D6) ──────────────────────
    def _install_library(self, unit: dict) -> dict:
        payload = unit.get("payload") or {}
        library = payload.get("library")
        src_cluster = safe_str(payload.get("cluster_id"))
        target_cluster, key = self.remap_id("cluster", src_cluster)
        if not target_cluster:
            raise PrerequisiteMissing(
                f"cannot install a library: source cluster {src_cluster!r}"
                + (f" ({key!r})" if key else "")
                + " has no target equivalent. Import the compute family first, then re-run with "
                  "retry_mode=failed_only.")

        if not self.config.imports.library_force_start_clusters:
            state = self._cluster_state(target_cluster)
            if state != "RUNNING":
                # Starting the cluster would spend the customer's money without being asked. So this
                # is recorded as outstanding work with everything needed to finish it, not attempted.
                raise PrerequisiteMissing(
                    f"library `{_library_label(library)}` could not be installed because target "
                    f"cluster `{key or target_cluster}` is {state or 'not running'} — "
                    f"`libraries/install` needs a RUNNING cluster, and the migration deliberately "
                    f"stops clusters after creating them so it does not consume DBUs. Either start "
                    f"the cluster and re-run with retry_mode=failed_only, or set "
                    f"library_force_start_clusters=true to let the tool start it.")

        self.client.post("api/2.0/libraries/install",
                         {"cluster_id": target_cluster, "libraries": [library]})
        return {"target_id": f"{target_cluster}:{_library_label(library)}",
                "note": f"installed on target cluster {key or target_cluster}"}

    def _cluster_state(self, cluster_id: str) -> str:
        try:
            got = self.client.get("api/2.0/clusters/get", params={"cluster_id": cluster_id}) or {}
            return safe_str(got.get("state"))
        except Exception:  # noqa: BLE001
            return ""

    # ── workspace conf ────────────────────────────────────────────────────
    def _set_conf(self, unit: dict) -> dict:
        """Apply ONE conf key. Unknown keys are reported, never blanket-written.

        A conf key can change the workspace's security posture (token access, IP-list enforcement),
        so writing whatever the source happened to return would be an unreviewed security change.
        """
        payload = unit.get("payload") or {}
        key = safe_str(payload.get("key")) or self.natural_key(unit)
        value = payload.get("value")
        if key not in KNOWN_CONF_KEYS:
            raise PrerequisiteMissing(
                f"workspace conf key `{key}` is not in this tool's documented key set, so it was NOT "
                f"written — a conf key can change the workspace's security posture, and applying an "
                f"unrecognised one from the source without review is not something the tool will do. "
                f"Set it by hand if it is wanted: value on source was {value!r}.")
        # One key per call: a single rejected key must not take the others down with it.
        self.client.patch("api/2.0/workspace-conf", {key: safe_str(value)})
        return {"target_id": key, "note": f"{key} set to {safe_str(value)!r}"}


def _library_label(library) -> str:
    """A short human label for a library spec, for notes and target ids."""
    if not isinstance(library, dict):
        return safe_str(library)
    for kind in ("pypi", "maven", "cran"):
        spec = library.get(kind)
        if isinstance(spec, dict):
            return f"{kind}:{safe_str(spec.get('package') or spec.get('coordinates'))}"
    for kind in ("jar", "whl", "egg", "requirements"):
        if library.get(kind):
            return f"{kind}:{safe_str(library[kind])}"
    return json.dumps(library, sort_keys=True)[:80]
