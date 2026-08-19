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
    """`/Users/<principal>` exactly — the one directory that cannot be created, only provisioned."""
    parts = [seg for seg in safe_str(path).split("/") if seg]
    return len(parts) == 2 and parts[0] == "Users"


def home_owner(path: str) -> str:
    """The `<principal>` segment of a `/Users/<principal>[/...]` path, else "".

    A service principal's home is `/Users/<applicationId>` (a bare UUID), a user's is
    `/Users/<email>`. Used to remap an SP home when the SP was recreated with a NEW applicationId.
    """
    parts = [seg for seg in safe_str(path).split("/") if seg]
    return parts[1] if len(parts) >= 2 and parts[0] == "Users" else ""


def _looks_like_app_id(owner: str) -> bool:
    """A UUID-shaped owner is a service principal home; an email is a user home."""
    o = safe_str(owner)
    return "@" not in o and o.count("-") == 4 and len(o) >= 32


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
            # Probe the TARGET path — an SP-home path is remapped to its new appId, so a re-run
            # adopts the already-migrated content instead of trying to recreate it (IMP-6). The
            # existence map is keyed by the SOURCE natural_key, since that is what the base loop
            # matches a unit on.
            target_path, _ = self._remap_home_path(path)
            if self._get_status(target_path):
                found[path] = target_path
                self.context.setdefault("workspace_paths", set()).add(target_path)
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

    def _sp_roster_status(self, app_id: str) -> str:
        """Whether `app_id` appears in the SOURCE identity roster (A2). Cached per importer.

        Reads `identity_classification.json` from the bundle (written at inventory time) once and
        indexes every service-principal applicationId in it. Returns "in_roster" (the SP existed on
        source but wasn't migrated this run), "absent" (deleted in source), or "unknown" (the
        classification file is missing/unreadable, so we cannot tell).
        """
        if not hasattr(self, "_sp_roster_cache"):
            roster: set[str] = set()
            have_roster = False
            try:
                from src.exporters import bundle_paths as _BP
                doc = self.staging.read_json(_BP.IDENTITY_CLASSIFICATION_JSON) or {}
                identities = doc.get("identities")
                if identities is not None:
                    have_roster = True
                    for ident in identities:
                        if safe_str(ident.get("identity_type")) == "service_principal":
                            aid = safe_str(ident.get("applicationId"))
                            if aid:
                                roster.add(aid)
            except Exception:  # noqa: BLE001 — a missing/garbled file just means "unknown"
                have_roster = False
            self._sp_roster_cache = (roster if have_roster else None)
        cache = self._sp_roster_cache
        if cache is None:
            return "unknown"
        return "in_roster" if safe_str(app_id) in cache else "absent"

    def _remap_home_path(self, path: str) -> tuple[str, str]:
        """Remap a service-principal HOME path from the source appId to the target appId (IMP-6).

        A Databricks-managed SP is recreated on target with a NEW applicationId, but its home path
        `/Users/<oldAppId>/...` was captured against the source id. Rewrite the owner segment to the
        new appId (via `sp_mapping`), so the SP's files/notebooks land inside the SP's REAL home on
        target instead of a `/Users/<oldAppId>` directory that can never exist (that was the two
        failed dirs). Returns `(remapped_path, note)`; `note` is "" when nothing was remapped.

        Account SPs and users keep their identifier (email / preserved appId), so they never appear
        in `sp_mapping` and pass through unchanged — only genuinely recreated SPs are rewritten.
        """
        owner = home_owner(path)
        if not (owner and _looks_like_app_id(owner)):
            return path, ""
        new_app_id = (self.identity_map.get("sp_mapping") or {}).get(owner, "")
        if not new_app_id or new_app_id == owner:
            return path, ""
        remapped = path.replace(f"/Users/{owner}", f"/Users/{new_app_id}", 1)
        return remapped, (f"service-principal home remapped {owner} → {new_app_id} "
                          f"(the SP was recreated with a new applicationId on target)")

    # ── home-presence guard (PLAN 8 Bug 8/14) ────────────────────────────
    def _home_present(self, home_root: str) -> bool:
        """Cached: does the `/Users/<owner>` home exist on target (remapped for a recreated SP)?

        One `get-status` per DISTINCT home root, cached — far cheaper than letting every descendant
        attempt an API call and 400 with DIRECTORY_PROTECTED / parent-missing."""
        if not home_root:
            return True
        cache = getattr(self, "_home_present_cache", None)
        if cache is None:
            cache = self._home_present_cache = {}
        if home_root not in cache:
            owner = home_owner(home_root)
            sp_map = self.identity_map.get("sp_mapping") or {}
            if sp_map.get(owner):
                # An SP in the identity map (created/adopted this run) has its home auto-provisioned
                # at SP-create (verified live), so its content lands — don't gate it on a get-status
                # that could race provisioning. Users are NOT here: a user home appears only on first
                # login, so it must be probed.
                cache[home_root] = True
            else:
                remapped, _ = self._remap_home_path(home_root)
                cache[home_root] = bool(self._get_status(remapped))
        return cache[home_root]

    def _guard_home_present(self, path: str) -> None:
        """Short-circuit a DESCENDANT of a user/SP home that is ABSENT on target with ONE clean
        `prerequisite_missing` (Bug 8/14), instead of the raw DIRECTORY_PROTECTED / "parent folder
        does not exist" errors that swamped the failure list. The home ROOT itself is handled in
        `_create_directory`; this covers everything BENEATH it (subdirs, notebooks, files)."""
        owner = home_owner(path)
        if not owner:
            return                                   # not under /Users — not a home descendant
        home_root = f"/Users/{owner}"
        if safe_str(path).rstrip("/") == home_root:  # the root itself, handled elsewhere
            return
        if self._home_present(home_root):
            return
        raise PrerequisiteMissing(
            f"`{path}` lives under `{home_root}`, whose owner is not present on target yet — a home "
            f"is provisioned only once its owner is assigned / logs in. Provision/assign `{owner}` "
            f"(identity phase / Entra SCIM), then re-run with retry_mode=failed_only; ALL content "
            f"under this home imports once it exists.")

    # ── directories ───────────────────────────────────────────────────────
    def _create_directory(self, unit: dict) -> dict:
        path = self.natural_key(unit)
        if is_skippable_path(path):
            return {"target_id": path,
                    "note": "workspace root / Trash path — exists by construction, not created"}
        if is_user_home(path):
            # A home directory appears when its OWNER is provisioned; it cannot be mkdir'd. For a
            # RECREATED service principal the source home path (`/Users/<oldAppId>`) can never exist
            # on target — but the SP's NEW home (`/Users/<newAppId>`) is auto-provisioned at SP
            # create (verified live), so the home ROOT is a skip either way, not a failure.
            remapped, note = self._remap_home_path(path)
            if note:
                return {"target_id": remapped,
                        "note": f"SP home root — auto-provisioned when the SP was created; {note}"}
            if self._get_status(path):
                return {"target_id": path, "note": "user home directory — already provisioned"}
            owner = home_owner(path)
            # A UUID-shaped owner NOT in the SP map is an SP that was never migrated — its home
            # cannot and should not be created. Say WHY the appId is missing (A2): distinguish
            # "present in the source roster but not migrated this run" (identity skipped/filtered)
            # from "deleted in source" (absent from the roster), because the operator's next step
            # differs — re-run the identity phase vs. confirm the SP was deliberately removed.
            if _looks_like_app_id(owner):
                roster = self._sp_roster_status(owner)
                if roster == "in_roster":
                    why = ("this SP IS present in the source roster but was NOT migrated in this "
                           "run (its identity family was skipped or filtered). Re-run the identity "
                           "phase for this workspace pair, then re-run with retry_mode=failed_only")
                elif roster == "absent":
                    why = ("this appId is NOT in the source roster — the SP was deleted in source. "
                           "Its home content is not migrated by design; confirm the SP was meant to "
                           "be removed")
                else:
                    why = ("the source identity classification is unavailable, so whether this SP "
                           "was skipped or deleted cannot be determined — check the identity phase")
                raise PrerequisiteMissing(
                    f"`{path}` is a SERVICE PRINCIPAL home for applicationId `{owner}`, which is not "
                    f"in this run's identity map, so its home cannot be created — an SP home only "
                    f"appears when the SP is provisioned. {why}.")
            raise PrerequisiteMissing(
                f"`{path}` is a USER HOME directory, which cannot be created — it appears only once "
                f"`{owner}` is provisioned on this workspace. Assign that user (identity phase / "
                f"Entra SCIM), then re-run with retry_mode=failed_only; the notebooks beneath it "
                f"import once it exists.")
        # Content BELOW a home whose owner is absent on target → one clean prerequisite, not a raw
        # DIRECTORY_PROTECTED / parent-missing error (Bug 8/14).
        self._guard_home_present(path)
        # Content BELOW a home: remap the SP-home prefix so it lands in the SP's real target home.
        remapped, note = self._remap_home_path(path)
        self.client.post("api/2.0/workspace/mkdirs", {"path": remapped})
        self.context.setdefault("workspace_paths", set()).add(remapped)
        return {"target_id": remapped, "note": note}

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
        # A descendant of a home whose owner is absent on target → one clean prerequisite (Bug 8/14),
        # rather than the raw parent-missing / DIRECTORY_PROTECTED error per notebook/file.
        self._guard_home_present(path)
        # A notebook/file under a recreated SP's home must follow the home to its NEW appId path
        # (IMP-6), or it would try to write into a `/Users/<oldAppId>` tree that cannot exist.
        path, home_note = self._remap_home_path(path)
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
            note = (f"{len(data)} bytes uploaded via the streaming workspace-files route "
                    f"(over the {BASE64_IMPORT_CAP // (1024 * 1024)} MB base64 limit)")
            return {"target_id": path, "note": f"{note}. {home_note}" if home_note else note}

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
        note = f"{len(data)} bytes uploaded"
        return {"target_id": path, "note": f"{note}. {home_note}" if home_note else note}

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
