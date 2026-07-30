# Plan 1a — Inventory feedback refinements (SOURCE side)

> Follow-up to Plan 1 after the customer reviewed the first inventory HTML/Excel. Nine feedback
> points; each item below states the decision, the verified facts behind it, and the change.
> API facts here were verified live against `fvm2` on 2026-07-30 (see memory
> `inventory-api-facts-verified.md`). No UC scope change.

## Guiding principle (carried from prior feedback)
Inventory is the reconciliation baseline for the final result report — every migratable unit is
a countable, visible line item carrying its migration-critical metadata. **Nothing in the
inventory should be null when tested** → create resources / use rich data so every card populates.

---

## The 9 items

### 1. ACL grants — add missing columns + close collection gaps (thorough audit done)
Audit result (which ACL-capable cards had the `_acls` count column):

| Card | ACL collected? | Column shown? | Action |
|---|---|---|---|
| jobs, clusters, pools, policies, dlt, serving, secret_scopes, notebooks, files, repos | yes | yes | none |
| **sql_warehouses** | **yes** (fetch works — `sql/warehouses` returns 200) | **no** | **add column** |
| **sql_queries / sql_alerts / sql_dashboards** (legacy) | **no** | no | **collect ACL (`queries`/`alerts`/`dashboards`) + add column** |
| **lakeview_dashboards** | **no** | no | **collect ACL (`dashboards`) + add column** |
| **genie_spaces** | **no** | no | **collect ACL (`genie`) + add column** |
| users / groups / service_principals | n/a | n/a | **no change — see note** |

**SP/user/group note (answers "can't see ACLs for SPNs"):** identities are ACL *principals*, not
ACL *targets* — there is no `/permissions/servicePrincipals/{id}` object-ACL (verified: returns
400/404). An identity's "access" = its **entitlements/roles** (already shown as columns) **plus**
its appearances as a principal in the **Object Permissions (ACLs)** sheet. So no object-ACL column
on identity cards; instead we keep entitlements visible and the ACL sheet lets you filter by an
SP/group principal. Verified permissions object-type strings: `dashboards`, `queries`, `alerts`,
`genie`, `warehouses`, `jobs`, `pipelines`.

All newly-collected ACLs also feed `_flatten_acls` → the **Object Permissions (ACLs)** sheet.

### 2. Key Vault column — no change (works). AKV-backed scope shows vault DNS; DBX-backed shows —.

### 3. Git repos incl. user-folder git folders — FIXED (is_git_folder detection)
Git folders live under `/Repos/<user>/...` AND inside user folders (`/Users/<email>/...`).
**Root cause found:** the `/api/2.0/repos` list API is unreliable — verified live on fvm2 it
returns EMPTY even with git folders present (SDK `repos.list()` also 0). So the old "union
`/repos` with/without path_prefix" (reference-script approach) MISSES repos. **Fix:** the
WorkspaceCollector walk now flags any DIRECTORY with `directory_info.is_git_folder==true` (or
type REPO) as a git folder — the reliable inline signal — records its id, does not descend into
it, and descends `/Repos` + `/Repos/<user>` as pure containers without emitting them as content.
`_repos()` fetches per-id detail via `GET /api/2.0/repos/{id}` (works even when the list is
empty) → path/url/provider/branch/head_commit_id, list API kept as dedup fallback. **Verified
live on fvm2:** a `/Repos` repo AND a user-folder git folder both appear with full detail; no
container dirs leak.

### 4. DAB (Databricks Asset Bundle) detection on jobs + pipelines — add (crucial)
Both expose `deployment.kind == "BUNDLE"` when bundle-deployed (jobs: `settings.deployment`;
pipelines: `spec.deployment`). Already in `_raw`. Action: **add `deployed_by_dab` bool + a
"Deployed by DAB" column on Jobs and DLT Pipelines.** Purpose: DAB-deployed ones get redeployed
to the target via their bundle (DevOps integrity), not migrated by this tool → the result report
flags them accordingly.

### 5. ACL count ↔ ACL sheet — no change (confirmed: per-object count = grants on that object;
detail rows live in the Object Permissions sheet for target replication).

### 6. SP OAuth secrets — add `has_secrets` boolean flag
Workspace-admin can enumerate SP OAuth secrets via
`GET /api/2.0/accounts/servicePrincipals/{SCIM_ID}/credentials/secrets` (workspace proxy; uses the
SCIM **id**, returns metadata only — never values). Action: **for each service principal, set
`has_secrets = (len(secrets) > 0)` and show a "Has Secrets" column.** Client secrets can't be
migrated → this flags SPs needing manual secret recreation on target. (One extra API call per SP;
best-effort, never fails the collector.)

### 7. SQL Queries "State" column — remove
`lifecycle_state` = ACTIVE/TRASHED, meaningless for migration (queries migrate as-is). Action:
**drop the State column from the sql_queries card** (keep the rest).

### 8. Job/ephemeral clusters — exclude from inventory entirely
Job/DLT/model clusters (`job-*`, `dlt-execution-*`, `mlflow-model-*`, or source JOB/PIPELINE/MODELS)
are ephemeral and never migrated. Action: **ComputeCollector drops ephemeral clusters entirely
(don't emit them)**; **remove the "Ephemeral" column** (all remaining are all-purpose). Keep Pinned.

### 9. Auto-Migratable — apps & lakebase stay False; Genie pending
Apps & Lakebase keep `Auto-Migratable = No` (correct). **Genie: automation exists — customer will
provide the process.** Leave Genie's flag/handling unchanged until that process is supplied
(tracked as a follow-up, not implemented in 1a).

---

## Testing (nothing null)
- **Offline:** rich synthetic dataset exercising every card + new columns (DAB jobs/pipelines,
  SP has_secrets, ACLs on warehouses/legacy-sql/dashboards/genie, user-folder repo). Extend the
  existing offline suites; all must pass.
- **Live (fvm2):** fvm2 is nearly empty, so **create resources** first (cluster policy, instance
  pool, secret scope + secret, IP access list, global init script, a repo, a git folder in a user
  dir; a small all-purpose cluster if needed). Jobs (6), serving (22), SPs (5, some with real
  secrets) already exist. Run the real InventoryRunner; confirm no unexpected nulls, new columns
  populate, ACL sheet includes the new sources, DAB + has_secrets resolve on real objects.

## Out of scope for 1a
Genie automation (item 9, pending customer process); any target-side/import work.
