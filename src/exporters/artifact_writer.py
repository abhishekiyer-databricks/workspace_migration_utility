"""
ArtifactWriter — run-isolated staging bundle I/O + manifest + checkpointing.

AIR-GAPPED model:
  • source side WRITES the bundle to `source_staging_location`;
  • target side READS the same bundle (uploaded by ops) from `target_staging_location`.
  • Staging location is a UC Volume path ("/Volumes/…") — managed OR an ADLS-backed external
    volume; either way it is FUSE-mounted so plain file I/O works. (Raw abfss:// is NOT used.)
    Mind the openpyxl-on-FUSE .xlsx gotcha: render to /tmp, then byte-copy to the Volume.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from typing import Any, Optional

from src.utils.helpers import now_iso
from src.utils.logger import get_logger

TOOL_VERSION = "0.1.0"
_LOG = get_logger("artifact_writer")


class ArtifactWriter:
    def __init__(self, config, dbutils=None, spark=None) -> None:
        self.config = config
        self.dbutils = dbutils
        self.spark = spark
        self._root = config.output_path  # <staging>/wsmig/<src_ws_id>/<run_id>

    @property
    def root(self) -> str:
        return self._root

    def _abs(self, rel_path: str) -> str:
        return os.path.join(self._root, rel_path)

    def ensure_output_path(self) -> str:
        """Create the run-isolated bundle dir (+ standard subdirs). Return it."""
        for sub in ("", "export", "export/identity", "export/compute",
                    "export/workspace", "export/secrets", "export/dashboards", "export/misc"):
            os.makedirs(self._abs(sub) if sub else self._root, exist_ok=True)
        _LOG.info("staging ready", path=self._root)
        return self._root

    # ── JSON ──────────────────────────────────────────────────────────────
    def write_json(self, rel_path: str, data: Any) -> None:
        p = self._abs(rel_path)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)

    def read_json(self, rel_path: str) -> Optional[Any]:
        p = self._abs(rel_path)
        if not os.path.isfile(p):
            return None
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    def write_bytes(self, rel_path: str, data: bytes) -> None:
        """For notebook SOURCE/DBC / workspace file content."""
        p = self._abs(rel_path)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)

    def write_text_local_then_copy(self, rel_path: str, render_fn) -> Optional[str]:
        """For openpyxl/xlsx: render to a local /tmp seekable path via `render_fn(local_path)`,
        then byte-copy the finished file to the FUSE Volume (no seeks on the Volume).

        `render_fn` takes the local path and writes the file there. Returns the Volume path on
        success, or None if rendering produced nothing (logged, non-fatal).
        """
        import tempfile
        dst = self._abs(rel_path)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmpdir = tempfile.mkdtemp(prefix="wsmig_")
        local = os.path.join(tmpdir, os.path.basename(rel_path))
        try:
            render_fn(local)
            if not (os.path.isfile(local) and os.path.getsize(local) > 0):
                _LOG.warning("nothing rendered", rel_path=rel_path)
                return None
            with open(local, "rb") as src, open(dst, "wb") as out:
                for chunk in iter(lambda: src.read(1024 * 1024), b""):
                    out.write(chunk)
            return dst
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    # ── manifest (handoff integrity) ──────────────────────────────────────
    @staticmethod
    def _file_checksum(path: str) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    def build_manifest(self, asset_counts: dict) -> dict:
        """Build a self-describing manifest: file list + sha256 + counts + provenance.

        The target verifies this before acting, so a partial/garbled upload is caught.
        """
        files = []
        for dirpath, _dirs, names in os.walk(self._root):
            for name in sorted(names):
                if name == "manifest.json":
                    continue
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, self._root)
                files.append({"path": rel, "bytes": os.path.getsize(full),
                              "sha256": self._file_checksum(full)})
        return {
            "tool_version": TOOL_VERSION,
            "source_workspace_id": self.config.source_workspace_id,
            "run_id": self.config.run_id,
            "created_utc": now_iso(),
            "asset_counts": asset_counts,
            "files": files,
        }

    def write_manifest(self, asset_counts: dict) -> dict:
        manifest = self.build_manifest(asset_counts)
        self.write_json("manifest.json", manifest)
        _LOG.info("manifest written", files=len(manifest["files"]))
        return manifest

    def verify_manifest(self) -> dict:
        """(target side) Verify the uploaded bundle against manifest.json.

        Returns {"ok": bool, "missing": [...], "mismatched": [...], "manifest": {...}}.
        """
        manifest = self.read_json("manifest.json")
        if manifest is None:
            return {"ok": False, "missing": ["manifest.json"], "mismatched": [], "manifest": None}
        missing, mismatched = [], []
        for entry in manifest.get("files", []):
            p = self._abs(entry["path"])
            if not os.path.isfile(p):
                missing.append(entry["path"])
            elif self._file_checksum(p) != entry["sha256"]:
                mismatched.append(entry["path"])
        return {"ok": not missing and not mismatched,
                "missing": missing, "mismatched": mismatched, "manifest": manifest}

    # ── checkpointing ─────────────────────────────────────────────────────
    def _checkpoint_path(self) -> str:
        return self._abs("checkpoint.json")

    def _load_checkpoint(self) -> dict:
        return self.read_json("checkpoint.json") or {}

    def is_done(self, component: str, item_key: str) -> bool:
        cp = self._load_checkpoint()
        return item_key in cp.get(component, [])

    def mark_done(self, component: str, item_key: str) -> None:
        cp = self._load_checkpoint()
        cp.setdefault(component, [])
        if item_key not in cp[component]:
            cp[component].append(item_key)
        self.write_json("checkpoint.json", cp)

    def mark_done_bulk(self, component: str, item_keys) -> None:
        """Record many item_keys done in ONE checkpoint write (avoids O(n²) per-item rewrites on
        the Volume when marking a large batch — e.g. thousands of fetched notebooks; Plan 2 §7c)."""
        item_keys = [k for k in item_keys]
        if not item_keys:
            return
        cp = self._load_checkpoint()
        existing = cp.setdefault(component, [])
        seen = set(existing)
        for k in item_keys:
            if k not in seen:
                existing.append(k)
                seen.add(k)
        self.write_json("checkpoint.json", cp)
