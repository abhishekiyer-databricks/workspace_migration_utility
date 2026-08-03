# Databricks notebook source
# MAGIC %md
# MAGIC # 02 — Export (SOURCE side)
# MAGIC Turns the Plan 1 inventory into a portable, self-describing **bundle**: a create-ready
# MAGIC JSON payload per migratable unit, the actual notebook/file **bytes**, `export/acls.json`,
# MAGIC the `export_index.json` tie-back ledger, `export_status.xlsx`, and `manifest.json` (checksums).
# MAGIC
# MAGIC **Runs on the `source` side** (air-gapped model — `plans/PLAN_0_master.md` §1,§3), right
# MAGIC after `01_Inventory`. Idempotent + checkpointed + fail-soft. Reads only THIS workspace;
# MAGIC writes only to `source_staging_location`. No target calls, no secrets.

# COMMAND ----------

# MAGIC %md ## Widgets  ( `role` MUST be **source** )

# COMMAND ----------

dbutils.widgets.dropdown("role", "source", ["source", "target"], "Role (must be source)")
dbutils.widgets.text("source_workspace_id", "", "Source workspace id")
dbutils.widgets.text("source_staging_location", "", "Source staging (UC Volume path /Volumes/...)")
dbutils.widgets.text("run_id", "", "Run id (blank = use LATEST_INVENTORY / resume incomplete)")
dbutils.widgets.text("max_scim", "0", "Max SCIM per type (0 = all)")
dbutils.widgets.text("max_workspace_items", "0", "Max workspace items (0 = all)")
dbutils.widgets.text("max_ws_api_calls", "0", "Max workspace/list calls (0 = unlimited)")
dbutils.widgets.text("content_fetch_workers", "8", "Parallel content-fetch workers")
dbutils.widgets.dropdown("force_full_export", "false", ["true", "false"],
                         "Ignore checkpoint/resume — re-export everything")
# Per-asset toggles (all default true; set false to skip a family — still recorded as 'skip').
for _t in ["identity", "compute", "workspace", "secrets", "jobs", "sql", "dlt",
           "dashboards", "genie", "serving", "misc"]:
    dbutils.widgets.dropdown(f"migrate_{_t}", "true", ["true", "false"], f"Migrate {_t}")

# COMMAND ----------

# MAGIC %md ## Bootstrap `src/` onto sys.path + install requirements

# COMMAND ----------

# MAGIC %pip install -q databricks-sdk openpyxl requests

# COMMAND ----------

import os
import sys


def _add_repo_root_to_syspath() -> str:
    """Find the repo root (dir containing `src/`) and prepend it to sys.path (see 01_Inventory)."""
    candidates = []
    try:
        ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
        nb_path = ctx.notebookPath().get()
        repo_dir = os.path.dirname(os.path.dirname(nb_path))
        candidates += [repo_dir, "/Workspace" + repo_dir]
    except Exception:
        pass
    here = os.getcwd()
    candidates += [here, os.path.dirname(here), os.path.dirname(os.path.dirname(here))]
    for cand in candidates:
        if cand and os.path.isdir(os.path.join(cand, "src")):
            if cand not in sys.path:
                sys.path.insert(0, cand)
            return cand
    raise RuntimeError("Could not locate the repo root (dir containing `src/`). Tried: "
                       + ", ".join(repr(c) for c in candidates))


_REPO_ROOT = _add_repo_root_to_syspath()
print(f"repo root on sys.path: {_REPO_ROOT}")

from src.config.config_manager import Config, ROLE_SOURCE
from src.auth.token_manager import build_client
from src.exporters.artifact_writer import ArtifactWriter
from src.exporters.bundle_state import resolve_export_run_id
from src.exporters.export_runner import ExportRunner
from src.utils import logger as _logger

# COMMAND ----------

# MAGIC %md ## Build config + resolve which run to export

# COMMAND ----------

cfg = Config.from_dbutils(dbutils, spark)  # reads role, staging, safety caps, toggles
assert cfg.role == ROLE_SOURCE, f"This notebook must run with role=source (got {cfg.role!r})"

# Resolve the run_id (Plan 2 §2b): explicit widget → task-value (2-task job) → incomplete-bundle
# resume → LATEST_INVENTORY.json pointer → fail loudly. Never invent a run_id (empty bundle).
_widget_run_id = (dbutils.widgets.get("run_id") or "").strip()
if not _widget_run_id:
    try:
        _tv = dbutils.jobs.taskValues.get(taskKey="inventory", key="run_id", debugValue="")
        if _tv:
            _widget_run_id = str(_tv).strip()
            print(f"run_id taken from Inventory task values: {_widget_run_id}")
    except Exception as _exc:
        pass  # not a 2-task job / no task values — fall through to pointer/resume

_force_full = (dbutils.widgets.get("force_full_export") or "false").strip().lower() == "true"
_run_id, _how = resolve_export_run_id(cfg, _widget_run_id, _force_full)
cfg.run_id = _run_id

client = build_client(cfg, dbutils=dbutils, spark=spark)  # context token for THIS workspace
print(f"Source workspace : {cfg.ctx.workspace_url}")
print(f"Run id           : {cfg.run_id}  (resolved via: {_how})")
print(f"Bundle           : {cfg.output_path}")

# COMMAND ----------

# MAGIC %md ## Run export → bundle in staging

# COMMAND ----------

aw = ArtifactWriter(cfg, dbutils=dbutils, spark=spark)
_logger.set_log_file(os.path.join(aw.ensure_output_path(), "execution_export.log"))

_workers = int((dbutils.widgets.get("content_fetch_workers") or "8") or 8)
result = ExportRunner(client, cfg, aw, dbutils=dbutils,
                      content_fetch_workers=_workers,
                      force_full_export=_force_full).run()

print("\n=== Export complete ===")
print(f"  total            {result['total']:>6}")
for k in ("success", "failure", "skipped_oversize", "manual", "dab", "skip"):
    print(f"  {k:<16} {result.get(k, 0):>6}")
print(f"\nBundle: {result['output_path']}")
print("  export/ + export_index.json + export/acls.json + export_status.xlsx + manifest.json")

# Verify the manifest we just wrote checksums cleanly (handoff-integrity self-check).
_verify = aw.verify_manifest()
print(f"\nManifest self-check: {'OK' if _verify['ok'] else 'PROBLEM'}")
if not _verify["ok"]:
    print("  missing:", _verify["missing"][:10])
    print("  mismatched:", _verify["mismatched"][:10])
