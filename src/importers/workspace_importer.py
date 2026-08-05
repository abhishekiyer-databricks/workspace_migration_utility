"""
WorkspaceImporter — phase 3: directories → notebooks → workspace files (Plan 3 §6).

Repos are OUT OF SCOPE for import (D9/§6a): recorded `manual` with their url/provider/branch/path as
the recreate runbook, never created. The base class handles that from `import_action`, so there is no
repo creation code here at all — which is the point.

The traps this phase exists to avoid, all things a naive "mkdirs then import" gets wrong:

  • **Directories go top-down.** `mkdirs` does create parents, but creating them in order keeps the
    per-directory ACL rows meaningful and the report readable.
  • **A user's home directory CANNOT be mkdir'd.** `/Users/<email>` exists only once that user is
    provisioned on the workspace — which is *why* identity is phase 1. Attempting it fails with an
    opaque error, so an unprovisioned owner is reported as a named prerequisite instead.
  • **Special roots must be skipped**: `/Repos`, `/Users`, `/Shared`, `/Workspace` themselves, and
    Trash. They exist by construction or are not ours to create.
  • **`.bundle/` content must be skipped** — branched on `import_action`, NEVER on `migration_mode`
    (D10). Importing bundle state files points the customer's next `databricks bundle deploy` at
    SOURCE-workspace object ids, so it would corrupt their deployment. The base class does this from
    the unit's action, and a test asserts the branch.
  • **Notebooks and files take different routes.** A notebook needs `format=SOURCE` + its language,
    or a `.py` lands as an opaque FILE and nothing runs. A workspace file needs `format=AUTO`, which
    preserves it verbatim.
"""
from __future__ import annotations

import base64
import os
import posixpath

from src.importers.base_importer import BaseImporter, PrerequisiteMissing
from src.utils.helpers import safe_str

# Roots that must never be created: they exist by construction, or are not ours to make.
_SKIP_ROOTS = ("/Repos", "/Users", "/Shared", "/Workspace")
_SKIP_PREFIXES = ("/Users/Trash", "/Trash")

# Platform-internal directories the workspace walk returns but that must NOT be recreated. Found
# live: `mkdirs` on `.db_internal` returns a bare 400, because these are managed by Databricks (they
# hold things like MLflow/notebook internals) and appear on their own when needed. Matched as a path
# SEGMENT so both the directory itself and anything under it is skipped.
_INTERNAL_SEGMENTS = ("/.db_internal", "/.ide", "/.databricks")

# Fallback language inference, used only for a unit with no recorded language.
_LANG_BY_EXT = {".py": "PYTHON", ".sql": "SQL", ".scala": "SCALA", ".r": "R", ".ipynb": "PYTHON"}

# `workspace/import` carries its content base64-encoded in a JSON body, and that body is capped at
# 10 MB — SEPARATELY from the 500 MB workspace-files ceiling that export works to. Verified live: a
# 120 MB file exported fine and then failed to import with "exceeded max size (10485760 bytes)".
# Anything larger goes through the streaming route instead.
BASE64_IMPORT_CAP = 10 * 1024 * 1024


def is_skippable_path(path: str) -> bool:
    """Whether a path must not be created: a workspace root, Trash, or a platform-internal dir."""
    p = safe_str(path).rstrip("/")
    if not p or p == "/":
        return True
    if p in _SKIP_ROOTS:
        return True
    if any(p.startswith(pre) for pre in _SKIP_PREFIXES):
        return True
    # Platform-internal (`.db_internal` etc.) — `mkdirs` returns a bare 400 for these, and they are
    # recreated by Databricks itself when needed.
    return any(seg in p + "/" for seg in (s + "/" for s in _INTERNAL_SEGMENTS))


def is_user_home(path: str) -> bool:
    """`/Users/<email>` exactly — the one directory that cannot be created, only provisioned."""
    parts = [seg for seg in safe_str(path).split("/") if seg]
    return len(parts) == 2 and parts[0] == "Users"


class WorkspaceImporter(BaseImporter):
    component = "workspace"
    asset_types = ("directory", "notebook", "workspace_file", "repo")

    def load(self) -> list[dict]:
        """Directories (shallowest first) → notebooks → files → repos.

        Sorting directories by depth makes the pass top-down, so content lands into a tree that
        already exists and a create failure means something real rather than "the parent wasn't there
        yet". Repo units are all `manual`; they ride along so they get a reported outcome.
        """
        dirs = sorted(self.units_for("directory"),
                      key=lambda u: safe_str(u.get("natural_key")).count("/"))
        return dirs + self.units_for("notebook", "workspace_file") + self.units_for("repo")

    # ── existence ─────────────────────────────────────────────────────────
    def existing_keys(self) -> dict:
        """`{path: path}` for the units' paths that already exist on target.

        Probed per unit with `workspace/get-status` rather than walking the whole tree, which on a
        large workspace would be thousands of calls to answer a question we only have for the paths
        in the bundle. A path we don't probe just takes the create route, where
        `RESOURCE_ALREADY_EXISTS` adopts it — equivalent outcome, cheaper.
        """
        found: dict = {}
        for unit in self.load():
            path = self.natural_key(unit)
            if not path or safe_str(unit.get("import_action")) in ("manual", "dab_redeploy"):
                continue
            if self._get_status(path):
                found[path] = path
                self.context.setdefault("workspace_paths", set()).add(path)
        return found

    def _get_status(self, path: str) -> dict:
        """`workspace/get-status` for a path, or {} if absent. Never raises — absent 404s."""
        try:
            return self.client.get("api/2.0/workspace/get-status", params={"path": path}) or {}
        except Exception:  # noqa: BLE001
            return {}

    # ── create ────────────────────────────────────────────────────────────
    def create_one(self, unit: dict) -> dict:
        asset_type = safe_str(unit.get("asset_type"))
        if asset_type == "directory":
            return self._create_directory(unit)
        if asset_type in ("notebook", "workspace_file"):
            return self._upload_content(unit)
        raise RuntimeError(f"workspace importer got an unexpected asset_type {asset_type!r}")

    def update_one(self, unit: dict, target_id: str) -> dict:
        """Content is re-uploaded with `overwrite=true`; a directory has nothing to update."""
        if safe_str(unit.get("asset_type")) == "directory":
            return {"target_id": target_id or self.natural_key(unit),
                    "note": "a directory has no mutable attributes — nothing to update"}
        return self._upload_content(unit, overwrite=True)

    # ── directories ───────────────────────────────────────────────────────
    def _create_directory(self, unit: dict) -> dict:
        path = self.natural_key(unit)
        if is_skippable_path(path):
            return {"target_id": path,
                    "note": "workspace root / Trash path — exists by construction, not created"}
        if is_user_home(path):
            # A home directory appears when the USER is provisioned; it cannot be mkdir'd. Its
            # absence means the owner isn't on this workspace — a prerequisite, not an API problem.
            if self._get_status(path):
                return {"target_id": path, "note": "user home directory — already provisioned"}
            owner = [s for s in path.split("/") if s][-1]
            raise PrerequisiteMissing(
                f"`{path}` is a USER HOME directory, which cannot be created — it appears only once "
                f"`{owner}` is provisioned on this workspace. Assign that user (identity phase / "
                f"Entra SCIM), then re-run with retry_mode=failed_only; the notebooks beneath it "
                f"import once it exists.")
        self.client.post("api/2.0/workspace/mkdirs", {"path": path})
        self.context.setdefault("workspace_paths", set()).add(path)
        return {"target_id": path}

    # ── notebooks + workspace files ───────────────────────────────────────
    def _upload_content(self, unit: dict, overwrite: bool = False) -> dict:
        """Upload the bytes exported into the bundle for this unit.

        A unit whose bytes never reached the bundle (oversize, or a failed fetch) is a MANUAL copy,
        not a create — reported as such rather than uploading an empty file, which would look
        successful while losing the content.
        """
        path = self.natural_key(unit)
        if is_skippable_path(path):
            # Content inside a platform-internal directory (`.db_internal`, `.ide`) — Databricks owns
            # these, and the parent cannot be created anyway.
            return {"target_id": path,
                    "note": "inside a platform-internal directory — owned by Databricks, not "
                            "recreated by this tool"}
        content_ref = safe_str(unit.get("content_ref"))
        if not content_ref:
            raise PrerequisiteMissing(
                f"no exported content for `{path}` — its bytes are not in the bundle (oversize, or "
                f"the export fetch failed), so it cannot be recreated here. Copy it across by hand; "
                f"see the oversize table in the export report.")

        data = self._read_content(content_ref)
        parent = posixpath.dirname(path)
        if parent and not is_skippable_path(parent):
            # Cheap insurance: a content unit whose parent directory was filtered out, or whose
            # family ran alone, would otherwise fail on a missing parent.
            try:
                self.client.post("api/2.0/workspace/mkdirs", {"path": parent})
            except Exception:  # noqa: BLE001 — idempotent; a real problem resurfaces on import
                pass

        is_notebook = safe_str(unit.get("asset_type")) == "notebook"

        # A WORKSPACE FILE over the base64 cap must use the STREAMING route. Found live: a 120 MB
        # file exports fine (the export ceiling is 500 MB) but `workspace/import` rejects it with
        # "File size imported is (125829120 bytes), exceeded max size (10485760 bytes)" — the base64
        # body has its own 10 MB limit. `workspace-files/import-file` streams the bytes and accepts
        # it. Notebooks have no such escape hatch: >10 MB genuinely cannot be created as a notebook
        # by any API, so those were never exported (recorded oversize instead).
        if not is_notebook and len(data) > BASE64_IMPORT_CAP:
            self._stream_upload(path, data, overwrite)
            self.context.setdefault("workspace_paths", set()).add(path)
            return {"target_id": path,
                    "note": (f"{len(data)} bytes uploaded via the streaming workspace-files route "
                             f"(over the {BASE64_IMPORT_CAP // (1024 * 1024)} MB base64 limit)")}

        body = {"path": path, "content": base64.b64encode(data).decode("ascii"),
                "overwrite": bool(overwrite)}
        if is_notebook:
            # format=SOURCE + language is what makes this land as a NOTEBOOK. Without it a `.py`
            # arrives as an opaque file and nothing runs.
            body["format"] = "SOURCE"
            body["language"] = self._language(unit, path)
            body["object_type"] = "NOTEBOOK"
        else:
            body["format"] = "AUTO"     # preserves a workspace file verbatim

        self.client.post("api/2.0/workspace/import", body)
        self.context.setdefault("workspace_paths", set()).add(path)
        return {"target_id": path, "note": f"{len(data)} bytes uploaded"}

    def _stream_upload(self, path: str, data: bytes, overwrite: bool) -> None:
        """Upload raw bytes via `workspace-files/import-file` (no base64, no 10 MB cap).

        Verified live with a 120 MB file. Goes through `requests` directly because the shared
        ApiClient sends JSON bodies, and this endpoint takes an octet-stream with the path in the URL.
        """
        import requests
        url = (f"{self.client.base_url}/api/2.0/workspace-files/import-file/"
               f"{path.lstrip('/')}?overwrite={'true' if overwrite else 'false'}")
        headers = self.client.auth_headers("application/octet-stream")   # this run's live token
        resp = requests.post(url, data=data, headers=headers, timeout=600)
        if resp.status_code >= 400:
            raise RuntimeError(f"streaming upload of {path} failed: HTTP {resp.status_code}: "
                               f"{resp.text[:300]}")

    def _read_content(self, content_ref: str) -> bytes:
        full = os.path.join(self.staging.root, content_ref)
        if not os.path.isfile(full):
            raise PrerequisiteMissing(
                f"the bundle's content file `{content_ref}` is missing, so this object's bytes "
                f"cannot be uploaded. In airgap mode that usually means an incomplete ops copy — "
                f"re-copy the whole run directory (manifest verification also catches this).")
        with open(full, "rb") as f:
            return f.read()

    @staticmethod
    def _language(unit: dict, path: str) -> str:
        """The notebook's language: the recorded one, else inferred from the content extension."""
        lang = safe_str((unit.get("payload") or {}).get("language")).upper()
        if lang:
            return lang
        ext = os.path.splitext(safe_str(unit.get("content_ref")) or path)[1].lower()
        return _LANG_BY_EXT.get(ext, "PYTHON")
