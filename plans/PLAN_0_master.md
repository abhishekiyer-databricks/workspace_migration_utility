# MASTER PLAN — Workspace Migration Utility

> **This is the MASTER plan** — the stable, high-level design of record. Each feature is
> then built from its own detailed sub-plan in this `plans/` directory:
> - `plans/PLAN_1_setup_and_inventory.md` — foundation (config/staging) + `01_Inventory`
> - `plans/PLAN_2_export.md`, `PLAN_3_identity_import.md`, … (created as we go)
>
> The master plan changes rarely; sub-plans carry the per-feature detail and are the
> review gate before each feature's code is written. The `src/` and `notebooks/` trees
> currently contain **stubs only** (docstrings + signatures + TODOs).

---

## 1. Goal recap (what we're building)

A **notebook-based**, **config-via-widgets**, **generic** utility that migrates **all
non-UC workspace assets** from a source Azure Databricks workspace (region 1) to a target
workspace (region 2), designed to be run 100+ times (one workspace at a time).

Key decided constraints (see `CLAUDE.md` for full context):
- **AIR-GAPPED, two-sided model (NO source↔target connectivity).** The export half runs
  **inside the SOURCE workspace** and dumps artifacts to a **source staging location**
  (a UC Volume path, widget-based). The customer ops team then **physically moves** those
  artifacts to a **target staging location** made readable from the target. The import half
  runs **inside the TARGET workspace** and reads from there. There is **no live REST call
  from one workspace to the other, ever.**
- **No terminal / no local Python.** Everything is Databricks notebooks + `%pip`.
- **Auth = each side's run-as SP, no cross-workspace creds.** Export runs as a
  **source workspace-admin SP**; import runs as a **target workspace-admin SP**. Each uses
  only its own **notebook-context token** against its own workspace. **No OAuth M2M, no
  PATs, no cross-workspace SP** — a workspace never authenticates to the other one.
- **Detection-driven identity** so we don't need the "same account?" decision to build.
- **Hive metastore + UC out of scope.** Assets only. (UC Volumes may be used purely as
  staging storage — that is not UC *migration*.)
- **Mirror the house style** of `uc-inventory-migration` (thin notebooks + `src/` package).

---

## 2. Repo layout (scaffold)

Deployed **twice** — the same Git folder is pulled into BOTH workspaces. Source-side
notebooks are run in the source; target-side notebooks in the target. Shared `src/` package
is identical on both sides; each module is used by whichever side needs it.

```
workspace_migration_utility/
├── CLAUDE.md                      # design context
├── plans/                         # PLAN_0_master.md (this) + per-feature sub-plans
├── README.md                      # operator-facing deploy + run guide (later)
├── requirements.txt               # requests, openpyxl, databricks-sdk
├── notebooks/                     # THIN — widgets + orchestration only
│   ├── 00_Main_Source.py          # optional orchestrator for SOURCE-side notebooks
│   ├── 00_Main_Target.py          # optional orchestrator for TARGET-side notebooks
│   ├── 00_Account_Preflight.py    # (target) VERIFY account prereqs, once before workspace #1
│   ├── 01_Inventory.py            # (source) read-only enumerate + classify → report
│   ├── 02_Export.py               # (source) dump assets → SOURCE staging location
│   ├── 03_Transform_Review.py     # (target) apply mappings/excludes → pre/post diff report
│   ├── 04_Import.py               # (target) create in dependency order (dry-run→live)
│   └── 05_Validate.py             # (target) target vs exported-manifest reconciliation
└── src/
    ├── config/
    │   └── config_manager.py      # Config dataclass; from_dbutils()/from_dict(); role + locations + toggles
    ├── auth/
    │   └── token_manager.py       # notebook-context token client for THIS workspace (no cross-WS auth)
    ├── collectors/                # READ from THIS workspace (used on source side)
    │   ├── base_collector.py
    │   └── <asset>_collector.py   # identity, compute, workspace, secrets, jobs, sql, dlt,
    │                              #   dashboards, genie, serving, misc
    ├── importers/                 # WRITE to THIS workspace (used on target side)
    │   ├── base_importer.py
    │   └── <asset>_importer.py
    ├── identity/
    │   ├── classifier.py          # Entra-user / UMI-SP / DB-managed-SP / DB-managed-group
    │   └── identity_map.py        # persist old→new (sp_mapping, group_map, user_map, manual_actions)
    ├── state/
    │   └── state_store.py         # TARGET-side Delta state table: per-asset upsert + persistent identity map (§9)
    ├── transform/
    │   └── transforms.py          # user/domain remap, exclude filters, pause schedules, strip runtime
    ├── reports/
    │   └── html_generator.py      # inventory / diff / import-result / validation HTML
    ├── exporters/
    │   ├── artifact_writer.py     # run-isolated staging dir; JSON/notebook read+write; manifest; checkpoint
    │   └── excel_generator.py     # Excel workbook (summary + per-asset sheets + Migration Plan)
    └── utils/
        ├── logger.py
        ├── retry.py               # HTTP retry/backoff + 429 handling
        └── helpers.py
```

**Why this shape:** same skeleton as `uc-inventory-migration` (collectors + config + reports
+ exporters + utils), plus an `importers/` tree (we also write) and an `identity/` tree (our
core enhancement). The only architecture change vs earlier drafts is that **collectors run on
the source side and importers on the target side of an air-gap** — they never run in the same
notebook, and neither talks to the other workspace.

---

## 3. Staging + handoff (air-gapped)

Two locations, one manual hop between them:

```
SOURCE workspace                         (ops moves files)          TARGET workspace
─────────────────                        ───────────────►          ─────────────────
export notebooks                                                    import notebooks
write to:                                                           read from:
  <source_staging>/wsmig/<src_ws_id>/<run_id>/                        <target_staging>/wsmig/<src_ws_id>/<run_id>/
```

- `<source_staging>` and `<target_staging>` are **widgets** — each a **UC Volume path**
  (`/Volumes/<cat>/<schema>/<vol>`), chosen per workspace. The Volume may be managed OR an
  **external Volume over ADLS** (register the ADLS location as a UC external location → create
  an external Volume on it). Either way the tool writes to a FUSE-mounted `/Volumes/...` path,
  so file I/O is uniform. **Raw `abfss://` paths are not used** (not FUSE-mounted).
- The export side writes a **self-describing bundle** under a run-isolated dir; the customer
  ops team downloads/copies the ENTIRE dir and uploads it to the target location unchanged.
- A **`manifest.json`** at the bundle root lists every artifact + counts + a checksum so the
  import side can verify the bundle arrived complete before doing anything.

**Bundle contents (written by source, read by target):**
```
<staging>/wsmig/<src_ws_id>/<run_id>/
├── manifest.json                 # asset list, counts, checksums, source ws id, tool version
├── execution_export.log
├── config_resolved.json          # effective export config (secrets redacted)
├── inventory.json / .xlsx / .html
├── export/                        # raw pulled asset JSON, one file/dir per asset type
│   ├── identity/{users,groups,service_principals,entitlements}.json
│   ├── compute/{instance_pools,cluster_policies,clusters}.json
│   ├── workspace/{dirs.json, acls.json, notebooks/*, files/*}
│   ├── secrets/{scopes.json, acls.json}
│   ├── jobs.json, sql_*.json, dlt_pipelines.json,
│   ├── dashboards/*.json, genie_spaces.json, serving_endpoints.json, misc/*.json
└── identity_classification.json  # per-identity class (from source-side classifier)
```

**Written by target during import (into the SAME run dir on the target location):**
```
├── identity_map.json             # sp_mapping, group_map, user_map, manual_actions
├── checkpoint.json               # per-asset, per-item done markers (resumable import)
├── execution_import.log
├── transform_diff.html/.xlsx     # pre/post review artifact
├── import_results.json/.html     # created/skipped/failed per asset
├── manual_actions.md             # Genie, secret values, account-admin gaps
└── validation_report.html/.json  # target vs manifest reconciliation
```

> **UC-Volume + openpyxl gotcha (carried forward):** openpyxl needs a seekable disk; writing
> `.xlsx` straight to a FUSE `/Volumes` path corrupts it. Render to local `/tmp`, then
> byte-copy to the staging location. HTML/JSON can be written directly.

---

## 4. Notebook responsibilities (thin) — split by side

| Notebook | Side | Reads | Writes | Core actions |
|---|---|---|---|---|
| `01_Inventory` | **source** | source WS | inventory.{json,xlsx,html} + classification | Run all collectors read-only; classify identities; scoping report. |
| `02_Export` | **source** | source WS | export/*, manifest, config_resolved | Dump each enabled asset → source staging bundle. Idempotent + checkpointed. Writes manifest+checksums. |
| — handoff — | ops | source staging | target staging | Ops downloads the run dir and uploads it to the target location, unchanged. |
| `00_Account_Preflight` | **target** | target account/WS + manifest | preflight report | Verify account identities referenced by the bundle exist/assigned in target. Verify-only. Go/no-go. |
| `03_Transform_Review` | **target** | bundle (verified) | transform_diff.* | Verify manifest/checksums; apply mappings/excludes/strip-runtime on staged copies; pre/post diff for sign-off. |
| `04_Import` | **target** | bundle + identity_map | target WS, import_results.*, identity_map | Create in dependency order. Dry-run default. Idempotent + checkpointed. Builds SP/group id maps, remaps ACLs. |
| `05_Validate` | **target** | target WS + manifest | validation_report.* | Reconcile created target objects against the export manifest; flag gaps + manual actions. |

Every notebook: widgets at top → `Config.from_dbutils()` → bootstrap `src/` onto `sys.path`
→ call into `src/` → write artifacts. Logic lives in `src/`, not the notebook. Each notebook
declares which **side** it runs on (a `role` widget: `source` | `target`) so a mis-run is
caught early.

---

## 5. Widgets (the entire operator interface)

**Common**
| Widget | Default | Notes |
|---|---|---|
| `role` | — | `source` (export/inventory) or `target` (preflight/transform/import/validate); guards mis-runs |
| `run_id` | (auto `YYYYMMDD_HHMMSS`) | shared across a workspace's stages; part of the bundle path |
| `source_workspace_id` | "" | identifies the bundle (`.../wsmig/<src_ws_id>/<run_id>`); on the target side this must match the bundle being imported |

**Source-side (inventory/export)**
| Widget | Default | Notes |
|---|---|---|
| `source_staging_location` | "" | UC Volume path (`/Volumes/…`; managed or ADLS-backed external volume) to WRITE the bundle |
| `max_scim`, `max_workspace_items`, `max_ws_api_calls` | 0 (all) | safety caps carried from the inventory script |
| `verbose` | false | verbose API logging |

**Target-side (transform/import/validate)**
| Widget | Default | Notes |
|---|---|---|
| `target_staging_location` | "" | UC Volume path to READ the bundle from (uploaded by ops) |
| `dry_run` | `true` | `04_Import` only mutates when `false` |
| `account_id` | "" | optional; enables account-level preflight/assignment |

- **No credentials in any widget.** Each side runs as a **Job whose run-as identity is a
  workspace-admin SP** on that workspace; all API calls use that SP's notebook-context token.
  A workspace never authenticates to the other. Workspace URLs are derived from context.

**Per-asset toggles — ALL default `true`** (operator flips to `false` to skip). Set on BOTH
sides; export honours them when dumping, import honours them when creating:
`migrate_identity`, `migrate_compute`, `migrate_workspace`, `migrate_secrets`,
`migrate_jobs`, `migrate_sql`, `migrate_dlt`, `migrate_dashboards`, `migrate_genie`,
`migrate_serving`, `migrate_misc`.
(`misc` = IP access lists + workspace conf + global init scripts + cluster libraries.
**PATs/tokens EXCLUDED** — disabled in the customer's workspace.)

**Transform options (target-side)**
| Widget | Default | Notes |
|---|---|---|
| `pause_job_schedules` | `true` | pause imported job schedules |
| `user_domain_mapping` | "" | `old.com=new.com,...` |
| `user_id_mapping` | "" | `old@a.com=new@b.com,...` |
| `exclude_path_patterns` | "" | regex, comma-separated |
| `exclude_job_name_patterns` | "" | regex, comma-separated |

Markdown at the top of each notebook explains each widget, the `role` it must run with, and
the "set a toggle to false to skip that component" instruction.

---

## 6. Per-asset catalog (Question B — the engineering spec)

For each asset: **list/detail API** (read from source, source-side) → **strip runtime
fields** → **remap references** → **create API** (write to target, target-side) with
**skip-if-exists** check. APIs below are the intended baseline; each is confirmed against docs
during its build slice. (Read = runs in source; Write = runs in target; they are separated by
the file bundle, not a live connection.)

| Asset | List / detail (read, source) | Create (write, target) | Strip (runtime) | Remap | Notes |
|---|---|---|---|---|---|
| Users | `GET scim/v2/Users` | `POST scim/v2/Users` | `id` | — | Entra: ensure-assigned + entitlements; skip create if present |
| Service principals | `GET scim/v2/ServicePrincipals` | `POST scim/v2/ServicePrincipals` | `id` | DB-managed: new `applicationId`→map | UMI/Entra: same appId, no create |
| Groups | `GET scim/v2/Groups` | `POST scim/v2/Groups` + PATCH members | `id`, `members` | member ids via user/sp/group maps | nested-first topo order |
| Entitlements | in SCIM `entitlements` | `PATCH` per identity | — | — | workspace-scoped |
| Instance pools | `GET instance-pools/list` | `POST instance-pools/create` | `instance_pool_id`, stats | — | same-cloud → keep node types |
| Cluster policies | `GET policies/clusters/list` | `POST policies/clusters/create` | `policy_id` | — | |
| Clusters | `GET clusters/list` | `POST clusters/create` | `cluster_id`, state, `*_attributes` runtime | pool/policy id remap | keep node types (same cloud) |
| Workspace dirs/notebooks | `GET workspace/list` + `export` | `POST workspace/mkdirs` + `import` | — | path remap (optional) | SOURCE (default) or DBC |
| Workspace files | `GET workspace/list` (FILE) | `POST workspace/import` / files API | — | — | non-notebook files |
| Workspace ACLs | `GET permissions/...` | `PUT permissions/...` | — | principal id remap | after objects exist |
| Repos | `GET repos` | `POST repos` | `id`, `head_commit_id` | — | git provider creds are manual |
| Secret scopes | `GET secrets/scopes/list` | `POST secrets/scopes/create` | — | — | **values NOT exportable** → manual |
| Secret ACLs | `GET secrets/acls/list` | `POST secrets/acls/put` | — | principal remap | |
| Jobs | `GET jobs/list` + `jobs/get` | `POST jobs/create` | `job_id`, `created_time`, `creator`, run state | cluster/pool/policy/notebook path + run_as SP remap | pause schedules per toggle |
| SQL warehouses | `GET sql/warehouses` | `POST sql/warehouses` | `id`, state, health, sessions | — | keep type (same cloud) |
| SQL queries/alerts/dashboards (legacy) | `GET sql/queries`,`sql/alerts`,`sql/dashboards` | corresponding `POST` | ids, timestamps | warehouse id + owner remap | |
| DLT pipelines | `GET pipelines` + detail | `POST pipelines` | `pipeline_id`, state, `cluster_id` | notebook path + cluster remap | |
| AI/BI dashboards | `GET lakeview/dashboards` + detail | `POST lakeview/dashboards` | ids, timestamps | warehouse id remap | serialized_dashboard payload |
| Genie spaces | `GET genie/spaces` (+ backing dashboard) | **manual** | — | warehouse id resolve | `serialized_space` not exportable → `manual_actions.md` |
| Serving endpoints | `GET serving-endpoints` | `POST serving-endpoints` | state, timestamps, config_version | — | skip `databricks-*` managed |
| Global init scripts | `GET global-init-scripts` | `POST global-init-scripts` | `script_id`, timestamps | — | fetch script body per id |
| Cluster libraries | `GET libraries/all-cluster-statuses` | `POST libraries/install` | status | cluster id remap | applied after clusters exist |
| IP access lists | `GET ip-access-lists` | `POST ip-access-lists` | `list_id` | — | **INCLUDED** per decision |
| Workspace conf | `GET workspace-conf?keys=...` | `PATCH workspace-conf` | — | — | **INCLUDED**; enumerate known keys |
| ~~PATs / tokens~~ | — | — | — | — | **EXCLUDED** — PAT disabled in customer WS |

**Universal caveats surfaced to `manual_actions.md`:** secret *values*, Genie spaces, git
repo credentials, anything needing account-admin when the target SP only has workspace-admin.

### 6a. Asset scope decisions vs the customer's `workspace_inventory_nb.ipynb`

The customer's existing inventory notebook fetches 30 asset types. Reconciled against our
non-UC scope:

**KEEP (workspace assets, in scope):** users, groups, service_principals, workspace_items
(notebooks/files), jobs, clusters, instance_pools, cluster_policies, sql_warehouses,
dlt_pipelines, lakeview_dashboards, genie_spaces, secret_scopes, repos, serving_endpoints,
sql_alerts, sql_queries, global_init_scripts, cluster_libraries.

**REMOVE (UC / account-level, out of non-UC scope):** `uc_registered_models`,
`uc_connections`, `delta_shares`, `delta_recipients`, `delta_providers`, `clean_rooms`,
`mlflow_experiments` (MLflow handled by separate tooling; drop or inventory-only, never migrate).

**INVENTORY-ONLY / manual for v1 (complex modern assets — flag, don't auto-migrate):**
`apps` (Databricks Apps — code + resources), `lakebase_projects` (managed Postgres),
`vector_search_endpoints` (backing indexes/data). Show in inventory; mark migration manual.

**ADD (workspace elements the inventory script MISSES — critical for migration):**
- **Object ACLs / permissions** for notebooks, dirs, jobs, clusters, pools, policies,
  warehouses, pipelines, repos, serving (the script lists objects but not their permissions).
- **Per-identity `entitlements`** (explicit SCIM attribute).
- **Secret scope ACLs** (script gets scope names only).
- **Group membership + nesting** fully expanded (for the identity engine).
- **IP access lists**, **workspace conf** (both INCLUDED per decision).

### 6b. Pagination / limits — MUST verify + test per API (customer instruction)

The inventory script has strong handling for SCIM (`startIndex`/`count`) and cursor APIs
(`get_paginated` with an explicit TRUNCATED warning) and a clever repos union
(`/Repos` + `/Workspace` path_prefix). **But several endpoints use a bare `get()` with NO
pagination** — currently OK only because those APIs return full lists today, which is an
**untested assumption**: `clusters/list`, `instance-pools/list`, `policies/clusters/list`,
`sql/warehouses`, `secrets/scopes/list`, `serving-endpoints`, `all-cluster-statuses`,
`global-init-scripts`.

**Rule for this project:** for EVERY API we adopt (read or write), we explicitly determine
whether it paginates, wire the correct cursor/offset handling, and **test it against a real
workspace with enough objects to cross a page boundary** (or document why it can't). No bare
`get()` is assumed complete without verification. This is a checklist item in every sub-plan.

---

## 7. Identity engine (our core enhancement)

Split across the air-gap: **classification happens source-side** (during inventory/export,
where the full source roster is visible) and is written into the bundle
(`identity_classification.json`); **reconciliation + creation happen target-side** (during
import, where we detect what already exists on the target).

1. **Collect** users/SPs/groups from **source workspace SCIM** + per-identity `entitlements`
   (source-side).
2. **Classify** each (`identity/classifier.py`, source-side). **Primary signal = `externalId`**
   (present on Entra/SCIM-provisioned identities, absent on workspace-local ones); fully
   deterministic if the SP also has account-level read (`workspace − account = local`):
   - Entra/SCIM **user** (has `externalId`) → account-managed, stable email; assign + entitle.
   - **UMI/Entra SP** (has `externalId`/managed `applicationId`) → account-managed, stable
     appId; add to target by same appId.
   - **Databricks-managed SP** (no `externalId`, workspace-local) → recreate → new appId → map.
   - **Databricks-managed group** (no `externalId`, workspace-local) → recreate with
     members/nesting/entitlements.
3. **Detection-driven target reconciliation (target-side):** list target identities; only
   create/assign what's missing. If assignment needs account-admin and the target SP lacks it
   → record a `manual_action` instead of failing.
4. **Persist `identity_map.json`** (`sp_mapping`, `group_map`, `user_map`, `manual_actions`)
   on the target side — consumed by ACL/job remap in `04_Import`.
5. **Order:** users/SPs → nested groups (topological) → parent groups → entitlements.

---

## 8. Cross-cutting mechanics

- **Auth (`auth/token_manager.py`):** ONE client per run, bound to **this** workspace, using
  the **notebook-context token of the run-as SP**. Prefer SDK `WorkspaceClient` ambient auth
  (works on serverless/shared/single-user) with a context-token fallback. **No OAuth M2M, no
  cross-workspace client, no secrets** — a workspace only ever calls its own APIs.
- **Handoff integrity:** export writes `manifest.json` (asset list, counts, checksums, source
  ws id, tool version); import verifies it before acting, so a partial/garbled upload is
  caught rather than silently under-migrated.
- **Idempotency:** every importer checks existence (by name/path/appId) and skips.
- **Checkpointing (`checkpoint.json`, target-side):** per-asset, per-item done markers; a
  re-run of import resumes. Export is also checkpointed on the source side.
- **Dry-run:** importers accept `dry_run`; log intended calls, mutate nothing.
- **Resilience:** collectors/importers never fatal the pipeline; failures recorded in stats +
  report. `utils/retry.py` handles 429/5xx with backoff.
- **Reporting:** reuse UC tool's HTML/Excel style — inventory, transform diff, import results,
  validation. Separate `execution_export.log` (source) and `execution_import.log` (target).

---

## 9. Incremental re-runs & state (UPSERT) — the utility runs repeatedly

The same workspace is migrated **more than once**: an initial run, then re-runs weeks/months
later to carry over source changes (jobs added, pools added, **policies edited**, etc.).
Plain skip-if-exists is NOT enough — it would **silently drop updates** to assets that already
exist on target. So every asset is **UPSERTed**, driven by a persistent state store.

**State store (`state/state_store.py`) — a Delta table on the TARGET** (target has the UC
catalog; this respects the air-gap — the source never needs target state). One row per
migrated object, keyed by **`(source_ws_id, asset_type, natural_key)`**, storing **BOTH** the
source and target identifiers plus change-tracking:
`source_object_id`, `target_object_id`, `last_source_fingerprint`, `last_run_id`,
`first_seen`, `last_seen`, `last_action` (created/updated/skipped/failed/deleted-in-source),
timestamps.

> **Why both ids (worked example):** source cluster-policy "policy-1" (source id 3) is created
> on target and gets **target id 9**; the state row records `source_object_id=3`,
> `target_object_id=9`. On the next run, "policy-1" is edited on source → fingerprint changes →
> the importer looks up the row by `(source_ws_id, cluster_policy, "policy-1")`, reads
> `target_object_id=9`, and calls the **edit API against target id 9** — updating the right
> object instead of creating a duplicate. The natural_key ties the two ids together across
> runs; the stored target id is what the update call needs.

**Natural key + fingerprint (produced source-side, in the bundle):**
- **Natural key** = the stable identity that survives across runs and across workspaces (the
  server id is stripped anyway): e.g. job *name*, cluster-policy *name*, pool *name*, warehouse
  *name*, notebook *path*, scope *name*, group *displayName*, SP *applicationId*.
- **Fingerprint** = a hash of the **normalized importable payload** (after runtime-field strip,
  before target-id remap) — changes iff the asset's migratable content changed on source.

**Import decision per asset (target-side, replaces bare skip-if-exists):**
| State row | On target | Fingerprint | Action |
|---|---|---|---|
| none | no | — | **CREATE**, record state |
| none | yes (pre-existing) | — | **ADOPT**: record state, compare fingerprint → skip or update |
| exists | yes | unchanged | **SKIP** |
| exists | yes | changed | **UPDATE** via the asset's edit API (see below) |
| exists | missing on source now | — | **REPORT as deleted-in-source** (never auto-delete by default; deletion behind an explicit opt-in toggle) |

**Update APIs (added to the per-asset catalog §6, used only on the UPDATE path):**
jobs `POST jobs/reset`, cluster policies `policies/clusters/edit`, instance pools
`instance-pools/edit`, clusters `clusters/edit`, warehouses `sql/warehouses/edit`, DLT
`pipelines/{id}` PUT, dashboards `lakeview/dashboards/{id}` PATCH, serving
`serving-endpoints/{name}/config` PUT, SCIM users/groups/SPs `PATCH`, permissions `PUT`
(already declarative), secret ACLs re-`put`, workspace-conf `PATCH`. Genie stays manual.

**Persistent identity map:** the `sp_mapping` / `group_map` (old→new for Databricks-managed
SPs/groups) is **persisted in the state store**, so a re-run reuses the previously-created
target SP/group instead of creating duplicates. `identity_map.json` in the bundle is the
per-run view; the state table is the durable source of truth across runs.

**Idempotency + checkpoint still apply** (mid-run resume); the state store adds the
**cross-run** dimension (create vs update vs skip vs deleted). Every re-run produces a
**change report** (created / updated / unchanged / deleted-in-source) for operator sign-off.

**Effect on Inventory (Plan 1):** read-only, so state/fingerprint/update-APIs live in Export
(Plan 2) + Import (Plans 3–7). Plan 1's only obligation: **every collector records a stable
`natural_key` per asset** so Export can fingerprint and Import can upsert. (Fingerprinting
itself is Plan 2.)

---

## 10. Build sequence (per-feature sub-plans)

Each feature gets its own detailed sub-plan under `plans/`, reviewed before its code:

1. **Plan 1 — Setup + Inventory (SOURCE side)** (`plans/PLAN_1_setup_and_inventory.md`):
   foundation (`utils/`, `config_manager` with role + staging locations + toggles,
   `auth/token_manager` context-token client, `artifact_writer` staging + manifest,
   `base_collector`) **plus** `01_Inventory` — all collectors read-only, identity
   classification, pagination verified per API, HTML+Excel+JSON reports. Adapts the customer's
   inventory notebook (remove UC/MLflow, add ACLs/entitlements/IP-lists/workspace-conf).
2. **Plan 2 — Export (SOURCE side)** (`02_Export`): dump enabled assets → source staging
   bundle (JSON + notebook SOURCE/DBC) + `manifest.json` + checksums. Per asset: emit the
   stable **natural key** + **content fingerprint** (§9). Checkpointed.
3. **Plan 3 — Identity import + STATE STORE (TARGET side, highest-risk write):** build
   `state/state_store.py` (Delta upsert table + persistent identity map, §9); target-side
   reconciliation → identity_map → identity importer. Proves Databricks-managed SPs/groups +
   entitlements AND the create-vs-update-vs-skip upsert path on re-run.
4. **Plan 4 — Account preflight (TARGET side, verify-only)**, reads the bundle's classification.
5. **Plan 5 — Compute + workspace content + secrets** import + ACLs (target side). Each
   asset upserts via the state store (create/update/skip using natural key + fingerprint).
6. **Plan 6 — Jobs** import (depends on compute/workspace/identity for remap). Upsert-aware.
7. **Plan 7 — SQL / DLT / dashboards / serving / genie / misc** import. Upsert-aware.
8. **Plan 8 — Transform+review, validate (incl. per-run CHANGE report: created/updated/
   unchanged/deleted-in-source), orchestrators, README (incl. ops handoff runbook), Job JSON.**

Each sub-plan: verify APIs (incl. pagination) → implement `src/` → wire the thin notebook →
dry-run/test on a real workspace → doc update.

---

## 10a. `databrickslabs/migrate` review — handling we MUST replicate (import plans)

We dropped the `databrickslabs/migrate` dependency; a review of its code confirmed the
patterns below that a naive "list → strip ids → POST" would miss. These are **feature-parity
requirements** folded into the relevant import sub-plans (3–7). `migrate` covers only
identity/compute/workspace/secrets/jobs — SQL/DLT/dashboards/Genie/serving/IP-lists/
workspace-conf come from the other reference tool + our own work.

**Identity (Plan 3):**
- Group import is **two-pass** (create all groups empty, then PATCH members) so nested/cross
  membership resolves regardless of order.
- Membership + ACL principals remap by **email/name, never source id**; build old-id→email and
  old-groupname→new-id maps.
- User create uses a **whitelist** (`emails,entitlements,displayName,name,userName`);
  entitlements + roles applied as **separate PATCH passes** after create.
- SP referenced as a group member must exist first (create SP, then patch membership).
- Known limitation: a role granted both directly AND via a group — API can't distinguish;
  only the group grant migrates (document it).

**Compute (Plan 5):**
- Pools/policies/clusters matched across workspaces **by name**; build name→new-id maps.
- Clusters: keep only the **create-config whitelist** (strip all runtime fields); **exclude
  ephemeral clusters** (`job-*`, `dlt-execution-*`, `mlflow-model-*`); remap `policy_id`,
  `instance_pool_id`, `driver_instance_pool_id`; when a pool is set, strip
  `node_type_id`/`driver_node_type_id`/`enable_elastic_disk`; **stop the cluster right after
  create**; **re-pin** pinned clusters; creator not preservable → `OriginalCreator` tag.
- Policies: send only `name`+`definition`; apply policy ACLs separately.

**Workspace content (Plan 5):**
- Special paths: skip `/Users`,`/Repos`,`/Projects` roots + Trash; **user home dirs can't be
  mkdir'd** (create the user first); **`/Shared` ACL is immutable** (skip); guard that target
  users exist before uploading content.
- Notebook format SOURCE vs DBC handled consistently; **large-notebook (>10 MB) skip**;
  case-insensitive filename collisions; empty dirs logged separately so they + ACLs migrate.
- ACLs: skip `inherited`, drop the `admins` group, re-resolve `object_id` by path on target,
  `skip_missing_users` tolerance for `RESOURCE_DOES_NOT_EXIST`.

**Repos (Plan 5):** need **Git credentials on target first** (skip if none); only URL-backed
repos recreate; auto-create parent `/Repos/<folder>`; repo contents re-cloned, not exported.

**Secrets (Plan 5):**
- Secret **values unrecoverable via API** → manual (do NOT spin a cluster to read them).
- **Azure Key-Vault-backed scopes** need the `backend_azure_keyvault` create payload —
  `migrate` doesn't handle this; **we must** (Azure→Azure). Databricks-backed scopes differ.
- `users:MANAGE` ACL must be set at scope-create via `initial_manage_principal`, not patched.

**Jobs (Plan 6):**
- **MULTI_TASK jobs need API 2.1 `expand_tasks=true` (paginated)** — 2.0 list drops `tasks`.
- Force-pause `schedule` **and** `continuous` on import.
- Remap `existing_cluster_id`/`new_cluster.policy_id`/`instance_pool_id` across `job_clusters`
  AND `tasks`; fall back to a default job cluster on create failure.
- Duplicate job names: suffix `name:::job_id` during id-mapping, rename back after.
- Jobs without an `IS_OWNER` ACL are malformed → log, don't migrate; owner remap.
- **`run_as` pointing at a workspace-local SP must be remapped** through our SP map (migrate
  does NOT do this — our addition).

**Cross-cutting:** import order pools→policies→…→clusters→jobs (secrets/pools after groups);
checkpoint keys type-consistent (their `str(job_id)` bug); `RESOURCE_ALREADY_EXISTS`/
`FEATURE_DISABLED` ignored; **SCIM pagination is a latent bug in `migrate` — we implement
`startIndex`/`count` (already in Plan 1)**; cluster libraries have **no importer** in
`migrate` → we build install-after-create ourselves.

> Full annotated findings (file/line cites) are in the design conversation. **Effect on Plan 1
> (inventory):** the two items that touch inventory — SCIM pagination and name-based natural
> keys — are already in Plan 1 §5/§7. Everything else lands in Plans 3–7.

---

## 11. Resolved decisions

0. **Repeatable / incremental runs** — RESOLVED: the utility runs the same workspace multiple
   times; every asset is **UPSERTed** via a **TARGET-side Delta state store** keyed by
   `(source_ws_id, asset_type, natural_key)`, storing **BOTH source and target object ids** +
   a content fingerprint (create / update-the-stored-target-id / skip / report-deleted).
   Persistent identity map avoids duplicate SP/group recreation. (§9)
1. **Connectivity / architecture** — RESOLVED: **NO source↔target connectivity.** Air-gapped
   two-sided model: export in source → staging location → **ops moves files** → staging
   location readable by target → import in target. No live cross-workspace calls.
2. **Auth** — RESOLVED: each side runs as its own **workspace-admin run-as SP** using the
   notebook-context token; no OAuth M2M, no PATs, no cross-workspace credentials.
3. **Staging** — RESOLVED: `source_staging_location` + `target_staging_location` widgets, each
   a **UC Volume path** (`/Volumes/…`; managed or ADLS-backed external volume — never raw
   `abfss://`, so file I/O is uniform). Bundle is run-isolated + self-describing (manifest+checksums).
4. **Notebook export format** — notebooks stored/served as `.py` Databricks `SOURCE` files
   (git folder). Wire format for content migration finalized in Plan 2 (default SOURCE).
5. **SQL legacy assets** — INCLUDE legacy queries/alerts/dashboards.
6. **Tokens / IP access lists / workspace conf** — PATs EXCLUDED (disabled); IP access lists +
   workspace conf INCLUDED.
7. **Excel output** — INCLUDE (openpyxl) alongside HTML + JSON.
8. **Apps / Lakebase / Vector Search** — v1: inventory-only, migration flagged manual (§6a).
