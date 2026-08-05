# Databricks notebook source
# MAGIC %md
# MAGIC # 00 — Account & target Preflight (TARGET side) — **VERIFY ONLY**
# MAGIC The go/no-go gate that runs **per workspace, before each import** (the account-level identity
# MAGIC checks are the once-per-account part; everything else is per-pair).
# MAGIC
# MAGIC **This notebook creates nothing.** It reads the bundle and inspects the target, then grades
# MAGIC every finding:
# MAGIC
# MAGIC | Grade | Meaning | Effect |
# MAGIC |---|---|---|
# MAGIC | **BLOCKING** | import cannot produce a correct target | NO-GO → with `preflight_enforce=true`, `04_Import` refuses to run |
# MAGIC | **DEGRADING** | import proceeds, but the **named units** below will be incomplete | GO-WITH-WARNINGS |
# MAGIC | **COSMETIC** | no effect on other assets | listed in the runbook only |
# MAGIC
# MAGIC So "must every manual step be finished first?" is not all-or-nothing: preflight tells you
# MAGIC which grade you are in, and blocks only when proceeding would be *wrong*. What it prevents is
# MAGIC importing against a target missing its account identities — which produces thousands of
# MAGIC half-migrated ACLs, far more work to unwind than to prevent.

# COMMAND ----------

# MAGIC %md ## Widgets (the same set `04_Import` uses, so both can be Job tasks with one param JSON)

# COMMAND ----------

dbutils.widgets.dropdown("connectivity_mode", "airgap", ["airgap", "direct"], "Connectivity mode")
dbutils.widgets.dropdown("role", "target", ["source", "target"], "Role (must be target)")
dbutils.widgets.text("source_workspace_id", "", "Source workspace id")
dbutils.widgets.text("target_staging_location", "", "Target staging (UC Volume /Volumes/...)")
dbutils.widgets.text("run_id", "", "Run id (blank = LATEST_EXPORT.json)")
dbutils.widgets.dropdown("dry_run", "true", ["true", "false"],
                         "Dry run (decides whether the state table is REQUIRED)")
dbutils.widgets.text("state_catalog", "", "State catalog (shared, must exist)")
dbutils.widgets.text("state_schema", "", "State schema (shared, must exist)")
dbutils.widgets.text("account_id", "", "Account id (optional; enables account-level checks)")
dbutils.widgets.dropdown("preflight_enforce", "true", ["true", "false"],
                         "Raise on NO-GO (so the Job task fails and import cannot follow)")
dbutils.widgets.dropdown("skip_manifest_verify", "false", ["true", "false"],
                         "Skip bundle verification")
dbutils.widgets.text("source_workspace_url", "", "[direct] Source workspace URL")
dbutils.widgets.text("source_sp_client_id", "", "[direct] Source SP applicationId")
dbutils.widgets.text("source_sp_secret_scope", "", "[direct] Secret scope for the SP secret")
dbutils.widgets.text("source_sp_secret_key", "", "[direct] Secret key")
dbutils.widgets.text("spn_secret_value", "", "[direct] SP secret (only if no scope/key; redacted)")

# COMMAND ----------

# MAGIC %md ## Bootstrap `src/`

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

from src.auth.token_manager import build_clients
from src.config.config_manager import ROLE_TARGET, Config
from src.exporters.artifact_writer import ArtifactWriter
from src.importers.import_runner import resolve_import_run_id
from src.importers.preflight import NO_GO, Preflight
from src.state.sql_backend import build_sql_backend
from src.state.state_store import StateStore

# COMMAND ----------

# MAGIC %md ## Resolve the bundle + build the clients

# COMMAND ----------

cfg = Config.from_dbutils(dbutils, spark)
assert cfg.role == ROLE_TARGET, f"Preflight must run with role=target (got {cfg.role!r})"

_widget_run_id = (dbutils.widgets.get("run_id") or "").strip()
if not _widget_run_id:
    try:
        _tv = dbutils.jobs.taskValues.get(taskKey="inventory", key="run_id", debugValue="")
        if _tv:
            _widget_run_id = str(_tv).strip()
    except Exception:
        pass
cfg.run_id, _how = resolve_import_run_id(cfg, _widget_run_id)

source_client, client = build_clients(cfg, dbutils=dbutils, spark=spark)
aw = ArtifactWriter(cfg, dbutils=dbutils, spark=spark)

state = None
if cfg.state_enabled:
    state = StateStore(build_sql_backend(cfg, spark=spark, client=client), cfg)

print(f"Target : {cfg.ctx.workspace_url}")
print(f"Bundle : {cfg.output_path}   (run resolved via: {_how})")
print(f"Mode   : {cfg.connectivity_mode}   |   {'dry run' if cfg.dry_run else 'LIVE'}")

# COMMAND ----------

# MAGIC %md ## Run every check

# COMMAND ----------

report = Preflight(client, cfg, aw, state=state, dbutils=dbutils,
                   source_client=source_client if cfg.is_direct else None).run()

print(f"\n{'=' * 70}\nVERDICT: {report['verdict']}\n{'=' * 70}\n")
for _f in report["findings"]:
    _mark = "OK  " if _f["ok"] else _f["grade"][:4]
    print(f"[{_mark}] {_f['check']}")
    if _f["detail"]:
        print(f"         {_f['detail']}")
    for _unit in (_f.get("affected_units") or [])[:12]:
        print(f"           · {_unit}")
    _extra = len(_f.get("affected_units") or []) - 12
    if _extra > 0:
        print(f"           … and {_extra} more (see preflight_report.html)")

# COMMAND ----------

# MAGIC %md ## Gate
# MAGIC With `preflight_enforce=true` (the default) a NO-GO raises here, so a Job task fails and
# MAGIC `04_Import` cannot run behind it. A customer who has accepted the gaps can set it `false` —
# MAGIC the same problems then surface as per-unit failures instead of a stop.

# COMMAND ----------

print(f"Report: {os.path.join(aw.root, 'preflight_report.html')}")

# Publish the verdict so a multi-task Job's import step can read it without re-running the checks.
try:
    dbutils.jobs.taskValues.set(key="preflight_verdict", value=report["verdict"])
except Exception:
    pass   # not running as a Job task

if report["verdict"] == NO_GO and cfg.imports.preflight_enforce:
    raise RuntimeError(
        "PREFLIGHT NO-GO — import must not run against this target yet:\n  - "
        + "\n  - ".join(report["blocking"])
        + "\n\nFix the blocking prerequisites above, or set preflight_enforce=false to proceed with "
          "them accepted (they will then surface as per-unit failures).")

print(f"\n{report['verdict']} — safe to proceed to 04_Import."
      + ("  Note the DEGRADING findings above: the units they name will be incomplete."
         if report["degrading"] else ""))
