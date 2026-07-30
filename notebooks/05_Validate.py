# Databricks notebook source
# MAGIC %md
# MAGIC # 05 — Validate (TARGET side)
# MAGIC Reconcile created TARGET objects against the export manifest -> validation report + manual actions. STUB.
# MAGIC
# MAGIC **Runs on the `target` side** (air-gapped model — see `plans/PLAN_0_master.md` §1,§3).
# MAGIC **STUB** — no logic yet. All config is via widgets (or job params); asset toggles
# MAGIC default **true** (set to **false** to skip a component).

# COMMAND ----------

# MAGIC %md ## Widgets
# MAGIC `role` MUST be **target** for this notebook. See PLAN_0_master §5 for the full widget list.

# COMMAND ----------

# TODO: declare widgets. Common: role, run_id, source_workspace_id.
# target side: target_staging_location, dry_run, account_id, transform options, per-asset toggles
# dbutils.widgets.dropdown("role", "target", ["source", "target"], "Role")

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

# TODO: cfg = Config.from_dbutils(dbutils, spark); assert cfg.role == "target"; wire this stage.
raise NotImplementedError("05 — Validate (TARGET side): stub — implement after PLAN_1 approval")
