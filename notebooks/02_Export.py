# Databricks notebook source
# MAGIC %md
# MAGIC # 02 — Export (SOURCE side)
# MAGIC Dump enabled SOURCE assets -> source staging bundle (JSON + notebook SOURCE/DBC) + manifest.json + checksums. Idempotent + checkpointed. STUB.
# MAGIC
# MAGIC **Runs on the `source` side** (air-gapped model — see `plans/PLAN_0_master.md` §1,§3).
# MAGIC **STUB** — no logic yet. All config is via widgets (or job params); asset toggles
# MAGIC default **true** (set to **false** to skip a component).

# COMMAND ----------

# MAGIC %md ## Widgets
# MAGIC `role` MUST be **source** for this notebook. See PLAN_0_master §5 for the full widget list.

# COMMAND ----------

# TODO: declare widgets. Common: role, run_id, source_workspace_id.
# source side: source_staging_location, max_scim, max_workspace_items, max_ws_api_calls, verbose, per-asset toggles
# dbutils.widgets.dropdown("role", "source", ["source", "target"], "Role")

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

# TODO: cfg = Config.from_dbutils(dbutils, spark); assert cfg.role == "source"; wire this stage.
raise NotImplementedError("02 — Export (SOURCE side): stub — implement after PLAN_1 approval")
