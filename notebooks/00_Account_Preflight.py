# Databricks notebook source
# MAGIC %md
# MAGIC # 00 — Preflight (TARGET, run BEFORE EACH import)
# MAGIC VERIFY-only: verify the bundle manifest; read its identity classification; check account
# MAGIC identities exist/assigned in the target; check the state schema, grants, AKV/AAD path and
# MAGIC job notebook paths; REPORT gaps **graded** BLOCKING / DEGRADING / COSMETIC. Does NOT perform
# MAGIC Entra/SCIM setup and creates nothing. Go/no-go gate. STUB.
# MAGIC
# MAGIC **Runs on the `target` side.** Implemented as part of **`plans/PLAN_3_import.md` §9**
# MAGIC (build step 5 — right after identity, whose reconciliation logic it reuses).
# MAGIC
# MAGIC **Scope note:** runs **per workspace before each import**, not once per account — only the
# MAGIC account-identity checks are account-wide; the bundle/state/path checks are per-pair.
# MAGIC With `preflight_enforce=true` (default) a NO-GO raises so `04_Import` cannot run behind it.
# MAGIC
# MAGIC **STUB** — no logic yet. All config is via widgets (or job params).

# COMMAND ----------

# MAGIC %md ## Widgets
# MAGIC `role` MUST be **target** for this notebook. See PLAN_0_master §5 for the full widget list.

# COMMAND ----------

# TODO: declare widgets (PLAN_3_import.md §2a, §7b + PLAN_0_master.md §5).
# common: connectivity_mode, role, run_id, source_workspace_id
# target: target_staging_location, dry_run, account_id, preflight_enforce
# state:  state_catalog, state_schema        (shared; assumed to exist)
# direct: source_workspace_url, source_sp_client_id,
#         source_sp_secret_scope + source_sp_secret_key  (preferred)  OR  spn_secret_value
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

# TODO: cfg = Config.from_dbutils(dbutils, spark); assert cfg.role == "target";
#       resolve the run (LATEST_EXPORT.json / run_id), verify_manifest(), run the graded checks,
#       write preflight_report.{json,html}, raise on NO-GO when cfg.preflight_enforce.
raise NotImplementedError("00 — Preflight (TARGET): stub — implement per PLAN_3_import.md §9 (build step 5)")
