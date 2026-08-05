# Databricks notebook source
# MAGIC %md
# MAGIC # 00 — Main, END TO END (`direct` mode ONLY)
# MAGIC Runs the whole migration as one sequence in the TARGET workspace:
# MAGIC
# MAGIC ```
# MAGIC 01_Inventory → 02_Export → 00_Account_Preflight → 03_Transform_Review → 04_Import → 05_Validate
# MAGIC   (reads source via OAuth M2M)      (gate)              (gate)            (writes target)
# MAGIC ```
# MAGIC
# MAGIC **Only possible in `direct` mode**, and this notebook asserts that. `airgap` has a manual file
# MAGIC hop in the middle — a single Job cannot span it, and pretending otherwise would produce a job
# MAGIC that always "succeeds" with an empty import. `airgap` uses `00_Main_Source` +
# MAGIC `00_Main_Target` as two Jobs with the ops copy between them.
# MAGIC
# MAGIC **Recommended first run for any workspace pair:** `dry_run=true`. That is a complete
# MAGIC rehearsal — real source read, real bundle written, real create/update/skip decisions, and zero
# MAGIC writes to the target. Then flip to `false`.
# MAGIC
# MAGIC The `run_id` is minted once and passed down, so every stage acts on ONE bundle with nothing to
# MAGIC retype. `LATEST_EXPORT.json` remains the durable fallback, so re-running a single stage in
# MAGIC isolation still finds the right bundle.

# COMMAND ----------

# MAGIC %md ## Widgets (one JSON of Job parameters defines a workspace pair)

# COMMAND ----------

dbutils.widgets.dropdown("connectivity_mode", "direct", ["direct", "airgap"],
                         "Connectivity mode (must be direct)")
dbutils.widgets.dropdown("role", "target", ["source", "target"], "Role (must be target)")
dbutils.widgets.text("source_workspace_id", "", "Source workspace id")
dbutils.widgets.text("target_staging_location", "", "Staging (UC Volume /Volumes/...)")
dbutils.widgets.text("run_id", "", "Run id (blank = mint a fresh one)")
dbutils.widgets.dropdown("dry_run", "true", ["true", "false"],
                         "Dry run (true = full rehearsal, no target writes)")

# direct-mode source connection
dbutils.widgets.text("source_workspace_url", "", "Source workspace URL")
dbutils.widgets.text("source_sp_client_id", "", "Source SP applicationId (not a secret)")
dbutils.widgets.text("source_sp_secret_scope", "", "Secret scope holding the SP secret (preferred)")
dbutils.widgets.text("source_sp_secret_key", "", "Secret key within that scope")
dbutils.widgets.text("spn_secret_value", "", "SP secret (only if no scope/key; redacted)")

# state + import controls
dbutils.widgets.text("state_catalog", "", "State catalog (shared, must exist)")
dbutils.widgets.text("state_schema", "", "State schema (shared, must exist)")
dbutils.widgets.multiselect("import_assets", "all",
                            ["all", "identity", "compute", "workspace", "secrets", "jobs", "sql",
                             "dlt", "dashboards", "genie", "serving", "misc", "acls"],
                            "Asset families to import")
dbutils.widgets.dropdown("retry_mode", "off",
                         ["off", "failed_only", "skipped_only", "failed_and_skipped"], "Retry mode")
dbutils.widgets.dropdown("preflight_enforce", "true", ["true", "false"],
                         "Stop the run on a preflight NO-GO")
dbutils.widgets.text("account_id", "", "Account id (optional)")
dbutils.widgets.dropdown("pause_job_schedules", "true", ["true", "false"],
                         "Pause imported job schedules + continuous triggers")
dbutils.widgets.text("max_workspace_items", "0", "Max workspace items (0 = all)")
dbutils.widgets.text("content_fetch_workers", "8", "Parallel content-fetch workers")

for _t in ["identity", "compute", "workspace", "secrets", "jobs", "sql", "dlt",
           "dashboards", "genie", "serving", "misc"]:
    dbutils.widgets.dropdown(f"migrate_{_t}", "true", ["true", "false"], f"Migrate {_t}")

# COMMAND ----------

# MAGIC %md ## Bootstrap + guard the mode

# COMMAND ----------

# MAGIC %pip install -q databricks-sdk openpyxl requests

# COMMAND ----------

import os
import sys


def _add_repo_root_to_syspath() -> str:
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
    raise RuntimeError("Could not locate the repo root (dir containing `src/`)")


_REPO_ROOT = _add_repo_root_to_syspath()
_NOTEBOOK_DIR = os.path.join(_REPO_ROOT, "notebooks")

from src.config.config_manager import MODE_DIRECT, Config
from src.utils.helpers import now_compact

_mode = (dbutils.widgets.get("connectivity_mode") or "").strip().lower()
if _mode != MODE_DIRECT:
    raise RuntimeError(
        f"00_Main_EndToEnd requires connectivity_mode=direct (got {_mode!r}).\n"
        "In `airgap` mode the bundle is moved between workspaces BY HAND, so a single Job cannot "
        "span the migration — a job that pretended to would always 'succeed' with an empty import.\n"
        "Use the two-Job airgap path instead: 00_Main_Source in the source workspace, then the ops "
        "file copy, then 00_Main_Target in the target.")

# Mint the run_id ONCE here and pass it to every stage, so all six act on one bundle.
_run_id = (dbutils.widgets.get("run_id") or "").strip() or now_compact()
print(f"End-to-end run_id: {_run_id}")

# COMMAND ----------

# MAGIC %md ## Stage sequence
# MAGIC Each stage is an independent notebook invocation with the same parameters, so any one of them
# MAGIC can be re-run alone afterwards (each is separately checkpointed, so a re-run resumes rather
# MAGIC than restarts).

# COMMAND ----------

# Every widget is forwarded, so a stage that doesn't use one simply ignores it. Passing them
# explicitly (rather than relying on inheritance) keeps each stage independently re-runnable.
_PASS_THROUGH = [
    "connectivity_mode", "role", "source_workspace_id", "target_staging_location", "dry_run",
    "source_workspace_url", "source_sp_client_id", "source_sp_secret_scope", "source_sp_secret_key",
    "spn_secret_value", "state_catalog", "state_schema", "import_assets", "retry_mode",
    "preflight_enforce", "account_id", "pause_job_schedules", "max_workspace_items",
    "content_fetch_workers",
] + [f"migrate_{t}" for t in ["identity", "compute", "workspace", "secrets", "jobs", "sql", "dlt",
                             "dashboards", "genie", "serving", "misc"]]


def _args() -> dict:
    args = {"run_id": _run_id}
    for name in _PASS_THROUGH:
        try:
            args[name] = dbutils.widgets.get(name)
        except Exception:
            pass
    return args


# (task name, notebook, timeout seconds, stops the run on failure?)
# Inventory and export can be slow on a large workspace, hence the generous timeouts. The two gates
# stop the run: importing behind a failed gate is exactly what the gates exist to prevent. Validate
# is report-only, so a hiccup there must not mark an otherwise-good migration failed.
_STAGES = [
    ("inventory",        "01_Inventory",          7200, True),
    ("export",           "02_Export",             7200, True),
    ("preflight",        "00_Account_Preflight",   900, True),
    ("transform_review", "03_Transform_Review",    900, True),
    ("import",           "04_Import",             7200, True),
    ("validate",         "05_Validate",           1800, False),
]

_results = {}
_failed_stage = None

for _name, _notebook, _timeout, _stop_on_failure in _STAGES:
    _path = os.path.join(_NOTEBOOK_DIR, _notebook)
    print(f"\n{'=' * 70}\n▶ {_name}  ({_notebook})\n{'=' * 70}")
    if _failed_stage:
        print(f"SKIPPED — `{_failed_stage}` failed earlier and stops the run.")
        _results[_name] = "skipped"
        continue
    try:
        _out = dbutils.notebook.run(_path, _timeout, _args())
        _results[_name] = "ok"
        print(f"✓ {_name} finished")
    except Exception as _exc:
        _results[_name] = f"FAILED: {str(_exc)[:400]}"
        print(f"✗ {_name} FAILED: {str(_exc)[:600]}")
        if _stop_on_failure:
            _failed_stage = _name
        else:
            print(f"  (`{_name}` is report-only, so the run is not marked failed)")

# COMMAND ----------

# MAGIC %md ## Summary

# COMMAND ----------

print(f"End-to-end run {_run_id} — {'DRY RUN' if dbutils.widgets.get('dry_run') == 'true' else 'LIVE'}\n")
for _name, _outcome in _results.items():
    _icon = "✓" if _outcome == "ok" else ("·" if _outcome == "skipped" else "✗")
    print(f"  {_icon} {_name:<18} {_outcome}")

_bundle = (f"{dbutils.widgets.get('target_staging_location').rstrip('/')}/wsmig/"
           f"{dbutils.widgets.get('source_workspace_id')}/{_run_id}")
print(f"\nBundle + reports: {_bundle}")
print("  inventory.html · export_status.xlsx · preflight_report.html · import_status.xlsx")
print("  import_results.html · acl_parity_report.html · manual_actions_import.md")

if _failed_stage:
    # Fail the Job so the operator is not told a partial migration succeeded.
    raise RuntimeError(
        f"the end-to-end run stopped at `{_failed_stage}`: {_results[_failed_stage]}\n"
        f"Every stage is independently checkpointed, so fix the cause and re-run this Job — it "
        f"RESUMES rather than restarting. To re-run just one stage, run its notebook with "
        f"run_id={_run_id}.")

if dbutils.widgets.get("dry_run") == "true":
    print("\nThis was a REHEARSAL — nothing was written to the target workspace.")
    print("Read import_status.xlsx and preflight_report.html, then re-run with dry_run=false.")
