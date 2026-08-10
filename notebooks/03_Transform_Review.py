# Databricks notebook source
# MAGIC %md
# MAGIC # 03 — Transform & Review (TARGET side)
# MAGIC Verify manifest/checksums of the uploaded bundle; apply mappings/excludes/strip-runtime on staged copies -> pre/post diff report for sign-off. STUB.
# MAGIC
# MAGIC **Runs on the `target` side** (air-gapped model — see `plans/PLAN_0_master.md` §1,§3).
# MAGIC **STUB** — no logic yet. All config is via widgets (or job params); asset toggles
# MAGIC default **true** (set to **false** to skip a component).

# COMMAND ----------

# MAGIC %md ## Widgets
# MAGIC This is the TARGET-side review stage (role is derived, not a widget — PLAN 7 §C).

# COMMAND ----------

# TODO: declare widgets. Common: connectivity_mode, run_id, source_workspace_id, staging_location.
# target side: dry_run, account_id, transform options. Build via
# Config.from_dbutils(dbutils, spark, stage=STAGE_IMPORT).

# COMMAND ----------

# MAGIC %md ## Bootstrap src/ onto sys.path

# COMMAND ----------

# TODO: prepend the Git-folder root (this repo, pulled into THIS workspace) to sys.path,
#       %pip install requirements, then import from src/.
# import sys; sys.path.insert(0, _REPO_ROOT)
# from src.config.config_manager import Config

# COMMAND ----------

# MAGIC %md ## Run

# COMMAND ----------

# TODO: cfg = Config.from_dbutils(dbutils, spark, stage=STAGE_IMPORT); wire this stage.
raise NotImplementedError("03 — Transform & Review (TARGET side): stub — implement in Plan 4")
