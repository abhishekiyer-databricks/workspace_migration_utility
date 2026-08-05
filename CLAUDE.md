# Workspace Migration Utility

## What this project is
A **notebook-based** Databricks workspace migration utility. It migrates **all non-UC
workspace assets** from a **source workspace** to a **target workspace**, both on Azure,
**Azure region 1 → Azure region 2 (same cloud, cross-region)**. It is designed to be run
**entirely from inside Databricks as notebooks** (no terminal, no local Python), and to
be **generic + config-driven** so the same code migrates 100+ workspace pairs for the
customer.

## Runtime model (REVISED 2026-08-04) — TWO modes, both must work (`connectivity_mode` widget)
The deployment model is not fixed, so the utility supports both. See PLAN_0_master §1a.
- **Mode A `airgap` (default)** — the two-sided model described below.
- **Mode B `direct`** — **all** stages run **inside the TARGET workspace**; `01_Inventory` +
  `02_Export` read the source over REST using a **source workspace-admin SP's client id +
  secret** (OAuth M2M; the secret comes from a target-workspace secret scope OR a widget — both
  supported, always redacted from artifacts/logs — see PLAN_3_import.md §2a). The
  bundle is written straight to `target_staging_location`, so there is **no manual hop** and the
  whole migration can run as **one end-to-end Job** (`00_Main_EndToEnd`, master §4a).
- Both modes produce/consume the **same bundle**, so import/transform/validate are mode-agnostic;
  the mode is recorded in `manifest.json` + `config_resolved.json`. Only `01`/`02` and
  `auth/token_manager.build_clients()` are mode-aware.

### Mode A detail — AIR-GAPPED, two-sided (NO source↔target connectivity)
- **There is NO network connectivity between source and target workspaces.** The pull model
  is dead. The utility runs on **two sides that never talk to each other**:
  - **SOURCE side** (`01_Inventory`, `02_Export`): runs **inside the source workspace**;
    reads source assets; **writes a bundle** to a **source staging location**.
  - **HANDOFF**: the **customer ops team physically moves** the bundle from the source
    location to a **target staging location** (download + upload), made readable by target.
  - **TARGET side** (`00_Account_Preflight`, `03_Transform_Review`, `04_Import`,
    `05_Validate`): runs **inside the target workspace**; reads the bundle; writes target.
- The same Git folder is pulled into **both** workspaces; a `role` widget (`source`/`target`)
  selects behaviour and guards mis-runs. **No live cross-workspace REST call, ever.**
- **Staging (DECIDED):** two widgets — `source_staging_location` + `target_staging_location`
  — each a **UC Volume path (`/Volumes/…`)**: managed OR an ADLS-backed external volume
  (register ADLS as a UC external location → external volume). Always FUSE-mounted so file I/O
  is uniform; **raw `abfss://` is not used**. Bundle is run-isolated + self-describing:
  `manifest.json` (asset list, counts, checksums, source ws id, tool version) lets the target
  verify the upload arrived complete before acting.
- **Hive metastore + UC are OUT of scope.** Assets only. (UC Volumes may be used purely as
  staging storage — not UC migration.)

## Auth model (REVISED 2026-08-04) — per-mode
- **PATs are not allowed / disabled in the customer WS** (still true in both modes).
- **`direct` mode DOES use OAuth M2M** (client-credentials) for the **source** workspace only —
  a source workspace-admin SP's `client_id` + secret, the secret read at runtime from a
  **target-workspace secret scope** via `dbutils.secrets.get`. Never a widget value, never in
  `config_resolved.json`. The earlier blanket "no OAuth M2M" applied to the air-gap-only design.
- The workspace a notebook **runs in** is always reached with the run-as SP's notebook-context
  token (SDK ambient auth), in both modes.

### Mode A detail — each side uses its own run-as SP; NO cross-workspace auth
- Each side runs as a **Databricks Job whose run-as identity is a workspace-admin SP** on
  **that** workspace. All API calls use that SP's **notebook-context token** against its own
  workspace only (SDK `WorkspaceClient` ambient auth, context-token fallback).
- A workspace **never authenticates to the other workspace** — the file bundle is the only
  thing that crosses. So the "same Databricks account?" question is irrelevant to the build.

## Config / driver (decided) — widget-based, no credentials in widgets
- Common widgets: `connectivity_mode` (airgap|direct), `role` (source|target), `run_id`,
  `source_workspace_id`.
- Source side: `source_staging_location`, `max_scim/max_workspace_items/max_ws_api_calls`,
  `verbose`. Target side: `target_staging_location`, `dry_run`, `account_id`, transform options,
  `import_assets` (which families to import this session), `state_catalog`/`state_schema`/
  (one shared catalog+schema across all pairs, assumed to exist; table names tool-owned),
  `preflight_enforce`, `retry_mode` (off|failed_only|skipped_only|failed_and_skipped).
- `direct` mode only: `source_workspace_url`, `source_sp_client_id`, and the secret **either** as
  `source_sp_secret_scope`+`source_sp_secret_key` (pointer, preferred when both set) **or**
  `spn_secret_value` (widget fallback). Always redacted from artifacts + logs.
- Per-asset toggles (all default TRUE; flip to FALSE to skip), set on both sides. Same values
  usable as Job params. **No credentials in any widget** (run-as SP context token).
- Asset scope decisions: INCLUDE legacy SQL queries/alerts,
  workspace conf, Excel output, global init scripts, cluster libraries.
  **Legacy SQL dashboards: inventoried + exported, NOT imported** (create endpoint deprecated/absent
  on modern workspaces, verified live) → `manual` + rebuild note; underlying queries still migrate.
  **Git repos: OUT OF SCOPE for import** (customer 2026-08-05) — inventoried + exported as metadata
  only (url/provider/branch/path; zero file bytes, the collector never descends into a git folder),
  which is the manual recreate runbook. Never created on target. EXCLUDE PATs/tokens
  (disabled) and **IP access lists** (account-level in this customer — configured in the
  account console / account API; a workspace-scoped tool can't see or migrate them → customer/
  account-admin manual task). REMOVE UC assets (registered models, connections, delta sharing, clean rooms) +
  MLflow. **Agent Bricks agents EXCLUDED** (Multi-Agent Supervisor / Knowledge Assistant /
  Custom LLM / Information Extraction): no workspace REST recreate path — a deployed agent is
  backed by a UC-registered MLflow ResponsesAgent model + UC assets + UI-only orchestration
  (all out of scope); IE agents are a UC Beta `agent-services` object. Model serving endpoints
  (non-agent) still INVENTORIED but **DOWNGRADED to migration-manual/conditional**: an endpoint
  only points at a model — if it serves a UC-registered model (out of scope) it can't be
  auto-recreated on target; only external-model endpoints are auto-migratable. Collector flags
  `migratable` + `migration_note` per endpoint. Apps / Lakebase / Vector Search = inventory-only,
  migration flagged manual for v1. **Genie spaces**: AUTO-MIGRATABLE (verified live fvm1
  2026-08-01). The current Genie API `GET /api/2.0/genie/spaces/{id}?include_serialized_space=true`
  returns the full `serialized_space` JSON, recreatable via `create_space`/`update_space`
  (approach adopted from the `client_shared_utils/workspace_asset_migration` reference). Export
  captures `serialized_space`+title+description+warehouse_id; import remaps `warehouse_id`.
  Caveat: `serialized_space` references UC tables by FQN → those must pre-exist on target (UC out
  of scope). (Supersedes the old "separate Genie repo / un-exportable protobuf" plan.)

## Account-level preflight (decided) — VERIFY only, run once before workspace #1
- Migration is done **one workspace at a time**, but there may be **one-time account-level
  prerequisites** (only relevant if region-2 is a SEPARATE Databricks account): Entra→SCIM
  provisioning, account groups, UMI/Entra SPs must exist in the target account first.
- Ship a `00_Account_Preflight` notebook (TARGET side) that **verifies** (does NOT perform)
  these: reads the exported bundle's identity classification, lists the account identities
  referenced, and reports which are present/absent/assigned in the target account → **go/no-go
  gate**. Actual Entra/SCIM setup stays a **customer IT one-time task** (needs Entra admin).
- Account model (same vs new account) is **still unknown**; preflight detects & handles both.

## Why it exists (context that isn't in the code)
- A prior tool by a senior engineer already does workspace migration:
  https://github.com/vivekravichandiran/WorkspaceMigration  (Azure → GCP, cross-cloud).
  We are using it as a **reference/base**, not a dependency.
- Two hard constraints make that tool unusable for this customer:
  1. **No terminal Python.** The customer's workspaces sit behind **front-end private
     connectivity** and are only reachable from inside a **VDI**, where running Python
     on a terminal is not permitted. Everything must run as Databricks notebooks.
  2. **Feature gap — Databricks-managed groups.** The customer has groups created
     *inside* Databricks (Databricks-managed / workspace-local), **not** managed by
     Entra ID / SCIM provisioning. These must be enumerated and recreated with
     membership + entitlements intact. The reference tool delegates identity to the
     `databrickslabs/migrate` terminal tool, which we cannot use here.

## Key differences from the reference tool
| Aspect | Reference tool | This utility |
|---|---|---|
| Runtime | Terminal Python package + bash | Databricks **notebooks** only |
| Cloud path | Azure → GCP (cross-cloud) | Azure region1 → Azure region2 (**same cloud, cross-region**) |
| Cluster/node transforms | Heavy (GCP node mapping, availability, spark conf rewrite) | **Not needed** — same cloud/region; keep transforms minimal |
| Identity engine | `databrickslabs/migrate` (external) | **Own REST/SDK implementation** (needed for DB-managed groups) |
| Config | `gcp_import_config.json` (GCP-specific) | Generic per-pair config; batch-driven for 100+ pairs |
| Scope | UC + non-UC | **Non-UC assets only** |

## Reference repos
- **WorkspaceMigration** (senior's, Azure→GCP, terminal): https://github.com/vivekravichandiran/WorkspaceMigration
  — reuse the REST export/import PATTERNS (`workspace_export/exporters/simple.py`,
  `workspace_import/extra_importers.py`, `sp_migrator.py`) and config concepts. Drop the
  GCP/bash/`databrickslabs-migrate` parts.
- **uc-inventory-migration** (senior's, notebook-based UC tool — our HOUSE STYLE to match):
  https://github.com/vivekravichandiran/uc-inventory-migration — adopt its structure and
  conventions so this utility is consistent with the customer's other tool. Cloned locally
  at `/tmp/uc-inventory-migration_ref` during design.

## Repo layout & storage (decided — mirror the UC tool)
Thin notebooks + importable `src/` package. Same deploy pattern the customer already knows.
Deployed twice — same Git folder pulled into BOTH workspaces; a `role` widget selects side.
```
notebooks/   00_Main_Source, 00_Main_Target, 00_Account_Preflight, 01_Inventory (source),
             02_Export (source), 03_Transform_Review (target), 04_Import (target),
             05_Validate (target)   (thin; widgets + %run/orchestrate)
src/         config/  auth/(context-token client for THIS ws)  collectors/(read this ws)
             importers/(write this ws)  identity/  state/(target Delta upsert state)
             transform/  reports/  exporters/  utils/
requirements.txt
```
Config is **widget-based** — no config files; the same widget values double as job params.

Planning docs live in `plans/`: `plans/PLAN_0_master.md` is the MASTER plan;
`plans/PLAN_<n>_*.md` are the per-feature sub-plans (each is the review gate before that
feature's code). Plan 1 = setup + inventory (source side).

Conventions carried over from the UC tool:
- **`Config` dataclass** built via `from_dbutils()`/`from_dict()`; holds `role`, staging
  locations, per-asset toggles, transform options. Auth = this workspace's run-as SP context
  token (no creds in config).
- **Bootstrap**: the repo is a Git folder in each workspace; each notebook prepends the repo
  root to `sys.path` — no zip/init-script. Optionally wrap the source-side notebooks and the
  target-side notebooks as two multi-task Databricks **Jobs** (run-as workspace-admin SP).
- **BaseCollector-style** abstract class (discover→enrich→validate→run, per-collector stats,
  failures never stop the pipeline). Mirror it with a `BaseImporter`.
- **Output**: run-isolated dir per run, `execution.log`, HTML + Excel + JSON artifacts, a
  "Migration Plan"-style checklist. Reuse their reports/exporters style.
- **requirements**: `requests`, `openpyxl`, `databricks-sdk` (drop `networkx` unless we build a
  dependency graph). Installed via `%pip`/bootstrap.

## Reference tool — what to reuse vs drop
Cloned locally at `/tmp/WorkspaceMigration_ref` during design (re-clone from the URL above).
- **Reuse the patterns** in `workspace_export/exporters/simple.py` and
  `workspace_import/extra_importers.py`: list → strip runtime fields → save JSON;
  import = load → skip-if-exists → POST. These cover SQL warehouses, DLT pipelines,
  repos, Lakeview/AI-BI dashboards, Genie spaces, model serving endpoints.
- **Reuse the config concepts**: `workspace_excludes` (regex path/job/cluster filters),
  `user_id_mapping`, `user_domain_mapping`, PAUSE-schedules-on-import, skip/force-recreate jobs.
- **Drop**: all GCP cluster rewriting, node_type_mapping.csv, Azure→GCP availability
  mapping, bash orchestrators, the `databrickslabs/migrate` dependency and its stubs.
- **Genie spaces (UPDATED — now auto-migratable, verified live 2026-08-01)**: the OLD caveat
  ("`serialized_space` is an un-exportable protobuf") is **obsolete** — the current Genie API
  `GET /api/2.0/genie/spaces/{id}?include_serialized_space=true` returns the full serialized_space
  JSON, recreatable via `create_space`/`update_space`. Export captures it; import remaps
  warehouse_id. Only caveat left: the serialized_space references UC tables by FQN (must pre-exist
  on target; UC out of scope).
- **Secret values caveat (still applies)**: secret scope *values* are never exported by
  the API — only scope names + ACLs migrate; values must be re-populated on target.

## Notebooks (scaffolded as stubs; see plans/PLAN_0_master.md §4)
`airgap` mode: `01_Inventory`/`02_Export` run in the SOURCE; the rest run in the TARGET.
`direct` mode: ALL of them run in the TARGET (01/02 read the source over REST).
- `00_Account_Preflight` — VERIFY-only account prereqs (run once before workspace #1)
- `01_Inventory` — read-only enumeration + identity classification → report (Plan 1)
- `02_Export` (source) — dump enabled assets → source staging **bundle** (JSON + notebook SOURCE/DBC) + manifest/checksums. Checkpointed.
- `03_Transform_Review` (target) — verify manifest; apply mappings/excludes → pre/post diff for sign-off
- `04_Import` (target) — create on target in dependency order; idempotent + checkpointed + dry-run
- `05_Validate` (target) — target vs export-manifest reconciliation report
- `00_Main_Source` / `00_Main_Target` — optional per-side orchestrators (`airgap`)
- `00_Main_EndToEnd` — single Job running inventory→export→preflight→transform→import→validate
  (**`direct` mode only**; the manual hop makes this impossible in `airgap`)
Reusable logic lives in the importable `src/` package (Git folder); notebooks stay thin.

### Asset dependency order (non-UC; Hive metastore + UC OUT of scope)
Identity (users → SPs → groups incl. nested + entitlements) → Compute (pools → policies →
clusters) → Workspace content (dirs → notebooks → files → repos → ACLs) → Secrets (scopes +
ACLs; values manual) → Jobs → SQL (warehouses, queries, alerts, legacy dashboards) → DLT →
AI/BI dashboards → Genie (manual) → model serving → misc (global init scripts, cluster
libraries, workspace conf). PATs + IP access lists (account-level) excluded.

## Identity model (decided — the core of this utility)

Split across the air-gap: **classification is done SOURCE-side** (during inventory/export,
written into the bundle as `identity_classification.json`); **reconciliation + creation are
TARGET-side** (during import, reading the bundle). Read the roster from the **source
WORKSPACE SCIM** (`/api/2.0/preview/scim/v2/...`), NOT account level — we reproduce exactly
the identities assigned to that workspace. **Entitlements are workspace-scoped**: captured
per identity on source, applied on target (`allow-cluster-create`, `databricks-sql-access`,
`workspace-access`, `allow-instance-pool-create`). Classify each identity and act accordingly:

| Type | Scope | New ID on target? | Action | ACL remap? |
|---|---|---|---|---|
| Entra/SCIM users | Account | No (email stable) | Ensure assigned to target WS + set entitlements | No |
| Azure UMI / Entra SPs | Account | No (`applicationId` stable) | Add to target WS by same applicationId + entitlements | No |
| Databricks-managed SPs | Workspace-local | **Yes** (new applicationId) | Recreate; build `old→new` map | **Yes** |
| Databricks-managed groups | Workspace-local | Yes (new id) | Recreate: members (users/SPs/nested groups) + entitlements + roles, nested-first | reference remap |

**Detection-driven, so we DON'T need the "same account?" answer to build:** at runtime the
utility lists what already exists on the target and only creates/assigns what's missing.
- Same account → Entra users/UMI-SPs/account-groups already exist at account level; if SCIM
  already assigns them to target WS we skip create+assign and only set entitlements + ACLs.
- Different account, or not yet assigned → account identities must be provisioned/assigned
  first. If the running SP has **account-admin**, the utility can assign them
  (PermissionAssignments API); if only **workspace-admin**, it **detects the gap and reports
  it** as a prerequisite for customer IT/SCIM rather than failing silently.
- Credential baseline = **workspace-admin**; account-admin is optional and unlocks
  auto-assignment of account identities. Utility degrades gracefully + reports either way.

Persist a per-pair `identity_map.json`: `sp_mapping` (old→new appId), `group_map`,
`user_map` (mostly identity), and a `manual_actions` list for anything requiring
account-admin / customer IT.

## Incremental / repeatable runs (decided — the utility runs the SAME workspace many times)
- Re-runs must carry over source changes over time (new jobs/pools, **edited policies**, etc.).
  Skip-if-exists alone would silently drop UPDATES → wrong. So every asset is **UPSERTed**.
- **TARGET-side Delta state store** (`src/state/state_store.py`), keyed by
  `(source_ws_id, asset_type, natural_key)`, storing **BOTH source and target object ids** +
  content **fingerprint**. Decision per asset: create / update (asset's edit API against the
  stored **target** id) / skip (fingerprint unchanged) / report deleted-in-source (never
  auto-delete by default). Storing both ids lets a re-run edit the right target object (e.g.
  source policy "p1" → target id 9; a later source edit updates target id 9, not a duplicate).
  Keeps the air-gap: source never needs target state.
- Export emits a stable **natural_key** + fingerprint per asset into the bundle. **Identity map
  (old→new SP/group ids) is persisted in the state store** so re-runs reuse, not duplicate.
- Every re-run emits a **change report** (created/updated/unchanged/deleted-in-source).
- Inventory (Plan 1) is read-only: its only obligation is recording a stable `natural_key`
  per asset. State/fingerprint/update-APIs are Export (Plan 2) + Import (Plans 3–7).

## Conventions / decisions
- Notebooks must be runnable with **no terminal**; any pip installs use `%pip`.
- Everything **idempotent** (skip-if-exists) and **checkpointed** so a re-run resumes; plus
  **cross-run UPSERT** via the Delta state store (see above).
- **Dry-run** supported on every mutating step.
- Auth = the run-as workspace-admin SP's **notebook-context token** for the workspace the notebook
  runs in. Never hard-code credentials; no PATs. **`airgap` mode additionally never calls the other
  workspace; `direct` mode DOES call the source, via OAuth M2M** (see the Auth model section).
- Generic first: no customer- or workspace-specific values in code; all in widgets/config.

## Status (2026-08-05)
- **Plans 1, 2 AND 3 are IMPLEMENTED and live-tested** fvm1 → target_ws. The whole pipeline runs:
  inventory → export → preflight → import (all 12 phases) → reports.
- **Plan 3 (import) is complete**: `src/state/state_store.py` + `sql_backend.py`, all 12 importers
  in `src/importers/` (identity, compute, workspace, secrets, jobs, sql, dlt, dashboards, genie,
  serving, misc, acls) + `base_importer` / `phases` / `import_runner` / `preflight`,
  `src/reports/import_report.py`, and the notebooks `00_Account_Preflight`, `04_Import`,
  `00_Main_EndToEnd`. `01`/`02` now have mode-aware role guards.
- **Test suite**: ~250 offline tests (`python3 -m pytest`), plus live harnesses:
  `live_direct_mode.py` (OAuth M2M, 13/13), `live_state_store.py` (real Delta MERGE, 24/24),
  `live_e2e_migration.py` (the full migration + idempotency + update + adopt + retry + ACL parity).
  `pytest.ini` keeps the offline suite safe to run anywhere; live harnesses are invoked explicitly.
- **pip needs the Databricks proxy**: `pip3 install --index-url https://pypi-proxy.cloud.databricks.com/simple <pkg>`.
- **Bugs found and fixed by the live E2E run** (each has a regression test):
  1. `checkpoint.json` was checksummed in `manifest.json`, but IMPORT writes it — so the first
     import invalidated the bundle and every later run was refused. Now excluded, along with the
     import-side outputs (`artifact_writer._excluded_from_manifest`).
  2. API errors carried no server explanation ("400 Client Error: Bad Request for url: …"), making
     every failure unactionable AND breaking `RESOURCE_ALREADY_EXISTS` detection. `ApiClient` now
     folds `error_code` + `message` into the raised error (`HTTPStatusError`).
  3. A >10 MB workspace FILE exports fine (500 MB ceiling) but `workspace/import` caps its base64
     body at 10 MB. Large files now use the streaming `workspace-files/import-file` route
     (verified with 120 MB).
  4. Platform-internal dirs (`.db_internal`) were being mkdir'd and 400ing — now skipped.
  5. Group membership was patched during each group's own create, which silently under-populated any
     group whose members came later in the bundle. Now a true second pass after all groups exist.
  6. Secret-scope ACLs were being sent to `PUT permissions/...` (which 404s) instead of
     `secrets/acls/put`, and nothing populated the MANAGE principal — the secrets importer now reads
     it from `export/acls.json` (it is needed at create time; the ACL phase runs last).
- **Deferred to Plan 4** (unchanged): `03_Transform_Review` and `05_Validate` are still stubs — the
  cross-stage inventoried→exported→imported reconciliation. `import_results.json` is already written
  in the shape Plan 4 joins on.
