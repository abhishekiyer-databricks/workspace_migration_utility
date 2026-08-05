# Databricks notebook source
# MAGIC %md
# MAGIC # 04 — Import (TARGET side)
# MAGIC Creates assets on the TARGET workspace from a verified bundle, in dependency order:
# MAGIC identity → compute → workspace → secrets → jobs → SQL → DLT → dashboards → genie → serving
# MAGIC → misc → **ACLs last** (a grant needs both id maps, so it can only run at the end).
# MAGIC
# MAGIC **Dry run is the default.** `dry_run=true` is a full rehearsal: real reads, real bundle, real
# MAGIC create/update/skip decisions, and **zero writes** to the target. Run it that way first.
# MAGIC
# MAGIC **Every unit is fail-soft.** One asset's failure — for any reason — is recorded with its
# MAGIC reason and the run continues. Only four things stop the run, all *before* any unit is
# MAGIC attempted: a bad bundle manifest, a preflight NO-GO, an unreachable state schema when live,
# MAGIC and (in `direct` mode) a source client that cannot authenticate. Each is a case where
# MAGIC continuing would give a *wrong* target rather than an incomplete one.
# MAGIC
# MAGIC **Re-runs are expected and safe.** Every asset is UPSERTed against the Delta migration state
# MAGIC table, so a second run creates what's new, updates what changed on source, and skips what
# MAGIC didn't. See `plans/PLAN_3_import.md`.

# COMMAND ----------

# MAGIC %md ## Widgets
# MAGIC `role` MUST be **target**. In `direct` mode every stage runs here, so `role=target` is
# MAGIC correct for inventory/export too.

# COMMAND ----------

dbutils.widgets.dropdown("connectivity_mode", "airgap", ["airgap", "direct"],
                         "Connectivity mode")
dbutils.widgets.dropdown("role", "target", ["source", "target"], "Role (must be target)")
dbutils.widgets.text("source_workspace_id", "", "Source workspace id (identifies the bundle)")
dbutils.widgets.text("target_staging_location", "", "Target staging (UC Volume /Volumes/...)")
dbutils.widgets.text("run_id", "", "Run id (blank = resume, else LATEST_EXPORT.json)")

# Dry run FIRST. Flip to false only once the rehearsal reads clean.
dbutils.widgets.dropdown("dry_run", "true", ["true", "false"],
                         "Dry run (true = decide everything, write nothing)")

# The migration state table: ONE shared catalog+schema for every workspace pair, both assumed to
# already exist. The tool owns the table NAMES. Required when dry_run=false.
dbutils.widgets.text("state_catalog", "", "State catalog (shared, must exist)")
dbutils.widgets.text("state_schema", "", "State schema (shared, must exist)")

# This session's work list. Separate from the migrate_* toggles (which are BUNDLE scope): the
# selector narrows what to import NOW, over what the bundle already contains. `acls` is
# independently selectable because ACL replay is the pass most likely to need a second attempt.
dbutils.widgets.multiselect("import_assets", "all",
                            ["all", "identity", "compute", "workspace", "secrets", "jobs", "sql",
                             "dlt", "dashboards", "genie", "serving", "misc", "acls"],
                            "Asset families to import THIS run")

# Narrow the run to outstanding units after fixing a prerequisite. One dropdown, not booleans, so an
# invalid combination cannot be set.
dbutils.widgets.dropdown("retry_mode", "off",
                         ["off", "failed_only", "skipped_only", "failed_and_skipped"],
                         "Retry mode (narrows the work list only)")

dbutils.widgets.dropdown("preflight_enforce", "true", ["true", "false"],
                         "Fail the run on a preflight NO-GO")
dbutils.widgets.dropdown("skip_manifest_verify", "false", ["true", "false"],
                         "Skip bundle verification (only for a deliberately pruned bundle)")
dbutils.widgets.dropdown("force_full_import", "false", ["true", "false"],
                         "Ignore the checkpoint and re-evaluate every unit")
dbutils.widgets.dropdown("allow_deletes", "false", ["true", "false"],
                         "Allow deleting target objects removed from source (default: report only)")
dbutils.widgets.dropdown("library_force_start_clusters", "false", ["true", "false"],
                         "Start stopped clusters to install libraries (consumes DBUs)")
dbutils.widgets.text("account_id", "", "Account id (optional; enables account-level checks)")

# `direct`-mode only — how to reach the SOURCE. The secret is EITHER a scope pointer (preferred:
# a widget value is visible on the run page and kept in run history) OR spn_secret_value.
dbutils.widgets.text("source_workspace_url", "", "[direct] Source workspace URL")
dbutils.widgets.text("source_sp_client_id", "", "[direct] Source SP applicationId (not a secret)")
dbutils.widgets.text("source_sp_secret_scope", "", "[direct] Secret scope holding the SP secret")
dbutils.widgets.text("source_sp_secret_key", "", "[direct] Secret key within that scope")
dbutils.widgets.text("spn_secret_value", "", "[direct] SP secret (only if no scope/key; redacted)")

# Transform options
dbutils.widgets.dropdown("pause_job_schedules", "true", ["true", "false"],
                         "Pause imported job schedules AND continuous triggers")
dbutils.widgets.text("user_domain_mapping", "", "old.com=new.com,...")
dbutils.widgets.text("user_id_mapping", "", "old@a.com=new@b.com,...")

# Per-asset toggles — BUNDLE scope, set identically on both sides.
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
    """Find the repo root (the dir containing `src/`) and prepend it to sys.path."""
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

from src.auth.token_manager import build_clients
from src.config.config_manager import ROLE_TARGET, Config
from src.exporters.artifact_writer import ArtifactWriter
from src.importers.import_runner import ImportRunner, resolve_import_run_id
from src.state.sql_backend import build_sql_backend
from src.state.state_store import StateStore
from src.utils import logger as _logger

# COMMAND ----------

# MAGIC %md ## Config + which bundle to import
# MAGIC Precedence: explicit `run_id` → resume an incomplete import → `LATEST_EXPORT.json` → fail
# MAGIC loudly. A run_id is never invented: that would import an empty bundle and report a
# MAGIC spuriously clean run.

# COMMAND ----------

cfg = Config.from_dbutils(dbutils, spark)
assert cfg.role == ROLE_TARGET, f"04_Import must run with role=target (got {cfg.role!r})"

# In a multi-task Job, `01_Inventory` publishes the run_id so every later task acts on one bundle.
_widget_run_id = (dbutils.widgets.get("run_id") or "").strip()
if not _widget_run_id:
    try:
        _tv = dbutils.jobs.taskValues.get(taskKey="inventory", key="run_id", debugValue="")
        if _tv:
            _widget_run_id = str(_tv).strip()
            print(f"run_id taken from the inventory task's values: {_widget_run_id}")
    except Exception:
        pass   # not a multi-task job — fall through to resume / the pointer

_run_id, _how = resolve_import_run_id(cfg, _widget_run_id)
cfg.run_id = _run_id

source_client, client = build_clients(cfg, dbutils=dbutils, spark=spark)

print(f"Target workspace : {cfg.ctx.workspace_url}")
print(f"Source ws id     : {cfg.source_workspace_id}")
print(f"Run id           : {cfg.run_id}   (resolved via: {_how})")
print(f"Bundle           : {cfg.output_path}")
print(f"Connectivity     : {cfg.connectivity_mode}")
print(f"Mode             : {'DRY RUN — nothing will be written' if cfg.dry_run else 'LIVE'}")
print(f"Families         : {', '.join(cfg.imports.selected_families)}")
print(f"Retry mode       : {cfg.imports.retry_mode}")

# COMMAND ----------

# MAGIC %md ## Bundle summary (what is about to be imported)

# COMMAND ----------

aw = ArtifactWriter(cfg, dbutils=dbutils, spark=spark)
_logger.set_log_file(os.path.join(aw.root, "execution_import.log"))

_index = aw.read_json("export_index.json") or {}
_bundle_cfg = aw.read_json("config_resolved.json") or {}
print(f"Bundle produced in `{_bundle_cfg.get('connectivity_mode', '?')}` mode, "
      f"tool version {_index.get('tool_version', '?')}, at {_index.get('generated_utc', '?')}")
print(f"{len(_index.get('units', []))} units in the bundle:")
for _at, _counts in sorted((_index.get("counts") or {}).items()):
    print(f"  {_at:<24} {_counts}")

# COMMAND ----------

# MAGIC %md ## Migration state table
# MAGIC One shared catalog+schema across all workspace pairs, keyed by `source_workspace_id`. The
# MAGIC tool owns the table names; the catalog/schema must already exist.

# COMMAND ----------

state = None
if cfg.state_enabled:
    _backend = build_sql_backend(cfg, spark=spark, client=client)
    state = StateStore(_backend, cfg)
    state.ensure_table()
    state.load(force=True)
    print(f"State table : {cfg.state_table_fqn}")
    print(f"Identity map: {cfg.identity_map_table_fqn}")
    print(f"Existing rows for this workspace pair: {len(state._cache)}")
    if state._cache:
        print("Where this pair is up to:")
        for _action, _n in sorted(state.summary().items(), key=lambda kv: -kv[1]):
            print(f"  {_action:<22} {_n}")
else:
    print("State store DISABLED (dry run with no state_catalog) — a first-look rehearsal needs no "
          "UC setup. A LIVE import requires state_catalog + state_schema.")

# COMMAND ----------

# MAGIC %md ## Preflight gate (verify only — creates nothing)

# COMMAND ----------

from src.importers.preflight import Preflight

_pf = Preflight(client, cfg, aw, state=state, dbutils=dbutils,
                source_client=source_client if cfg.is_direct else None).run()

print(f"\n=== PREFLIGHT: {_pf['verdict']} ===")
for _grade, _key in (("BLOCKING", "blocking"), ("DEGRADING", "degrading"),
                     ("COSMETIC", "cosmetic")):
    for _item in _pf.get(_key) or []:
        print(f"  [{_grade}] {_item}")
print(f"\npreflight_report.html written to the bundle. "
      f"{'Import will NOT run.' if _pf['verdict'] == 'NO-GO' and cfg.imports.preflight_enforce else ''}")

# COMMAND ----------

# MAGIC %md ## Run the import

# COMMAND ----------

runner = ImportRunner(client, cfg, aw, state=state, dbutils=dbutils, preflight_verdict=_pf)
result = runner.run()

print(f"\n=== IMPORT {result['run_status'].upper()} "
      f"({'DRY RUN' if cfg.dry_run else 'LIVE'}) in {result['elapsed_sec']}s ===")
_totals = result.get("totals", {})
for _k in ("total", "created", "updated", "adopted", "skipped", "created_with_warning",
           "manual", "not_selected", "skipped_no_object", "failed"):
    print(f"  {_k:<22} {_totals.get(_k, 0):>6}")

print("\nPer phase:")
for _phase in result.get("per_phase", []):
    print(f"  {_phase['component']:<12} total={_phase['total']:<5} created={_phase['created']:<5} "
          f"updated={_phase['updated']:<4} skipped={_phase['skipped']:<5} "
          f"failed={_phase['failed']:<4} manual={_phase['manual']:<4} "
          f"({_phase['elapsed_sec']}s)")

# COMMAND ----------

# MAGIC %md ## Failures, warnings and manual actions
# MAGIC A failure here is a per-unit outcome, not a broken run. Fix the cause, then re-run this
# MAGIC notebook with `retry_mode=failed_only` to attempt exactly those units.

# COMMAND ----------

_results = aw.read_json("import_results.json") or {}
_units = _results.get("units", [])

_failed = [u for u in _units if u.get("import_status") == "failed"]
_warned = [u for u in _units if u.get("import_status") == "created_with_warning"]
_manual = [u for u in _units if u.get("import_status") == "manual"]
_no_obj = [u for u in _units if u.get("import_status") == "skipped_no_object"]

print(f"=== FAILURES ({len(_failed)}) — fix, then retry_mode=failed_only ===")
for _u in _failed[:40]:
    print(f"  [{_u.get('failure_category')}] {_u['asset_type']}/{_u['natural_key']}")
    print(f"      {_u.get('note')}")

print(f"\n=== CREATED BUT DEGRADED ({len(_warned)}) — they exist, but verify before use ===")
for _u in _warned[:25]:
    print(f"  {_u['asset_type']}/{_u['natural_key']}: {str(_u.get('note'))[:180]}")

print(f"\n=== MANUAL STEPS ({len(_manual)}) ===")
for _u in _manual[:25]:
    print(f"  {_u['asset_type']}/{_u['natural_key']}: {str(_u.get('note'))[:160]}")

print(f"\n=== PERMISSIONS PENDING AN OBJECT ({len(_no_obj)}) — normal for DAB/repos ===")
for _u in _no_obj[:15]:
    print(f"  [{_u.get('failure_category')}] {_u['natural_key']}")

# COMMAND ----------

# MAGIC %md ## ACL parity — the report to read after an import
# MAGIC Source-vs-target permission diff, proven by re-reading every object we touched rather than
# MAGIC assumed. `inherited` grants are dropped on BOTH sides so like is compared with like.

# COMMAND ----------

_parity = aw.read_json("acl_parity_report.json") or {}
if _parity:
    print(f"objects re-read and diffed: {_parity.get('objects_checked', 0)}")
    for _verdict, _n in sorted((_parity.get("counts") or {}).items(), key=lambda kv: -kv[1]):
        print(f"  {_verdict:<20} {_n}")
    _bad = [o for o in _parity.get("objects", []) if o.get("verdict") != "match"]
    for _o in _bad[:25]:
        print(f"  {_o.get('verdict')}: {_o.get('perm_object_type')}/{_o.get('object')} "
              f"missing={_o.get('missing_on_target')} extra={_o.get('extra_on_target')}")
    print(f"\nNOTE: {_parity.get('known_limitation', '')}")
else:
    print("no ACL parity report (the acls family was not selected this run)")

# COMMAND ----------

# MAGIC %md ## Artifacts + what to do next

# COMMAND ----------

print(f"Bundle: {aw.root}\n")
for _name in ("import_results.json", "import_results.html", "import_status.xlsx",
              "manual_actions_import.md", "acl_parity_report.json", "acl_parity_report.html",
              "preflight_report.json", "preflight_report.html", "execution_import.log"):
    _p = os.path.join(aw.root, _name)
    print(f"  {'✓' if os.path.exists(_p) else '·'} {_name}")

print("\nNext steps:")
if cfg.dry_run:
    print("  1. Read import_status.xlsx — every unit's intended action, failures first.")
    print("  2. Fix anything BLOCKING in preflight_report.html.")
    print("  3. Re-run with dry_run=false (state_catalog + state_schema are then required).")
else:
    print("  1. Read acl_parity_report.html — the source-vs-target permission diff.")
    print("  2. Work through manual_actions_import.md (secret values, Git repos, legacy dashboards).")
    print("  3. Re-run with retry_mode=failed_only once prerequisites are fixed.")
    print("  4. Re-run with import_assets=acls + retry_mode=skipped_only after a DAB redeploy.")

# The log is appended locally then mirrored: appending straight onto a UC Volume silently fails and
# used to truncate the log to one line.
_logger.flush_log_file()
