"""
fixtures_fvm1 — populate the fvm1 (source) workspace with a COMPLETE test fixture set covering
every inventory asset type + edge cases, so the export utility can be tested end-to-end.

Idempotent-ish: uses stable `wsmig_test*` names / paths; re-running skips or overwrites where the
API allows. Writes ONLY to fvm1 (source). Run phases individually:
    python3 -m tests.fixtures_fvm1 <phase>
where phase ∈ identity compute workspace secrets uc sql jobs dlt dashboards genie misc dab all

Everything is namespaced so it's easy to find/remove:
  • names prefixed `wsmig_test_`
  • workspace paths under /Shared/wsmig_test/ and /Users/<me>/wsmig_test/
  • UC under catalog_23s8a1_pbswg8.wsmig_test
"""
from __future__ import annotations

import base64
import json
import sys
import time

from databricks.sdk import WorkspaceClient

PROFILE = "fvm1"
CATALOG = "catalog_23s8a1_pbswg8"
SCHEMA = "wsmig_test"
ME = "abhishek.iyer@databricks.com"
TEST_USERS = ["aman.bansal@databricks.com", "vivek.ravichandran@databricks.com",
              "sanket.kelkar@databricks.com", "idris.chakera@databricks.com"]
SHARED = "/Shared/wsmig_test"
USERDIR = f"/Users/{ME}/wsmig_test"

w = WorkspaceClient(profile=PROFILE)


def log(msg):
    print(f"  {msg}", flush=True)


# ─────────────────────────── identity ──────────────────────────────────────

def phase_identity():
    from databricks.sdk.service import iam
    print("== identity ==")
    # 1. add test users (account users → assign to workspace)
    for email in TEST_USERS:
        try:
            w.users.create(user_name=email)
            log(f"user created: {email}")
        except Exception as e:
            log(f"user {email}: {str(e)[:70]}")

    # 2. Databricks-managed groups (workspace-local, no externalId) + entitlements
    def mk_group(name, entitlements=None, members=None):
        try:
            g = w.groups.create(
                display_name=name,
                entitlements=[iam.ComplexValue(value=e) for e in (entitlements or [])],
                members=[iam.ComplexValue(value=m) for m in (members or [])],
            )
            log(f"group created: {name} (id={g.id})")
            return g.id
        except Exception as e:
            log(f"group {name}: {str(e)[:70]}")
            # fetch existing id
            for g in w.groups.list(filter=f'displayName eq "{name}"'):
                return g.id
            return None

    # resolve user ids for membership
    uid = {}
    for email in [ME] + TEST_USERS:
        for u in w.users.list(filter=f'userName eq "{email}"'):
            uid[email] = u.id
    # child group with entitlements + a couple users
    child = mk_group("wsmig_test_child_grp",
                     entitlements=["databricks-sql-access"],
                     members=[uid[e] for e in TEST_USERS[:2] if e in uid])
    # parent group with the child nested + cluster-create entitlement + me
    mk_group("wsmig_test_parent_grp",
             entitlements=["allow-cluster-create", "workspace-access"],
             members=([child] if child else []) + ([uid[ME]] if ME in uid else []))
    # a plain group, no entitlements
    mk_group("wsmig_test_plain_grp", members=[uid[TEST_USERS[2]]] if TEST_USERS[2] in uid else [])

    # 3. Databricks-managed SPN (workspace-local; no externalId → DB-managed)
    try:
        sp = w.service_principals.create(display_name="wsmig_test_db_sp",
                                         entitlements=[iam.ComplexValue(value="allow-cluster-create")])
        log(f"db-managed SPN created: wsmig_test_db_sp (appId={sp.application_id})")
    except Exception as e:
        log(f"db SPN: {str(e)[:70]}")

    # 4. An "Entra-style" SPN.
    #    CAVEAT (verified live 2026-08-03): the WORKSPACE SCIM API silently DROPS `externalId`
    #    on create — the created SP comes back with only {displayName, applicationId, active}.
    #    Only real Entra/SCIM provisioning (account level) sets externalId. So this fixture
    #    cannot manufacture a true account-managed SP, and the collector correctly classifies it
    #    as db_managed_sp. The umi_or_entra_sp path is instead covered live by the workspace's
    #    REAL provisioned SPs (e.g. the fe-vending-machine SPs, which DO carry an externalId)
    #    plus the offline test test_identity_import_action_create_vs_assign.
    import uuid
    try:
        sp2 = w.service_principals.create(
            display_name="wsmig_test_entra_sp",
            application_id=str(uuid.uuid4()),
            external_id="entra-ext-" + uuid.uuid4().hex[:8],
        )
        got_ext = bool(getattr(sp2, "external_id", None))
        log(f"entra-style SPN created: wsmig_test_entra_sp (appId={sp2.application_id}; "
            f"externalId persisted={got_ext} — workspace SCIM drops it, so this classifies as "
            f"db_managed_sp)")
    except Exception as e:
        log(f"entra SPN: {str(e)[:90]}")


# ─────────────────────────── compute ───────────────────────────────────────

def phase_compute():
    from databricks.sdk.service import compute
    print("== compute ==")
    node = "Standard_DS3_v2"
    # instance pool
    pool_id = None
    try:
        p = w.instance_pools.create(instance_pool_name="wsmig_test_pool", node_type_id=node,
                                    min_idle_instances=0, max_capacity=2)
        pool_id = p.instance_pool_id
        log(f"instance pool: wsmig_test_pool ({pool_id})")
    except Exception as e:
        log(f"pool: {str(e)[:70]}")
    # cluster policy
    try:
        pol = w.cluster_policies.create(
            name="wsmig_test_policy",
            definition=json.dumps({"node_type_id": {"type": "allowlist", "values": [node]},
                                   "spark_version": {"type": "regex", "pattern": ".*"}}))
        log(f"cluster policy: wsmig_test_policy ({pol.policy_id})")
    except Exception as e:
        log(f"policy: {str(e)[:70]}")
    # all-purpose cluster (don't start it — just create the config)
    try:
        spark_v = "16.4.x-scala2.12"
        c = w.clusters.create(cluster_name="wsmig_test_cluster", spark_version=spark_v,
                              node_type_id=node, num_workers=1,
                              autotermination_minutes=10).result if False else None
    except Exception:
        pass
    # use the REST create (no wait) so we don't block on a running cluster
    try:
        resp = w.api_client.do("POST", "/api/2.0/clusters/create", body={
            "cluster_name": "wsmig_test_cluster", "spark_version": "16.4.x-scala2.12",
            "node_type_id": node, "num_workers": 1, "autotermination_minutes": 10})
        cid = resp.get("cluster_id")
        log(f"cluster: wsmig_test_cluster ({cid})")
        # give it an ACL grant so acls.json is exercised
        if cid:
            grp = next((g for g in w.groups.list(filter='displayName eq "wsmig_test_parent_grp"')), None)
            if grp:
                w.api_client.do("PATCH", f"/api/2.0/permissions/clusters/{cid}", body={
                    "access_control_list": [{"group_name": "wsmig_test_parent_grp",
                                             "permission_level": "CAN_RESTART"}]})
                log("  + cluster ACL grant added")
    except Exception as e:
        log(f"cluster: {str(e)[:90]}")


# ─────────────────────────── workspace content ─────────────────────────────

def phase_workspace():
    from databricks.sdk.service import workspace
    print("== workspace content ==")
    for d in (SHARED, USERDIR, f"{SHARED}/sub"):
        try:
            w.workspace.mkdirs(d)
        except Exception as e:
            log(f"mkdir {d}: {str(e)[:50]}")
    # notebooks in each language → tests all SOURCE extensions
    nbs = {
        "PYTHON": ("py_nb", "# Databricks notebook source\nprint('hi from python')\n"),
        "SQL": ("sql_nb", "-- Databricks notebook source\nSELECT 1 AS x\n"),
        "SCALA": ("scala_nb", "// Databricks notebook source\nprintln(\"hi scala\")\n"),
        "R": ("r_nb", "# Databricks notebook source\nprint('hi from R')\n"),
    }
    for lang, (name, src) in nbs.items():
        for base_dir in (SHARED, USERDIR):
            path = f"{base_dir}/{name}"
            try:
                w.workspace.import_(path=path, language=getattr(workspace.Language, lang),
                                    format=workspace.ImportFormat.SOURCE,
                                    content=base64.b64encode(src.encode()).decode(),
                                    overwrite=True)
            except Exception as e:
                log(f"nb {path}: {str(e)[:60]}")
    log("notebooks (py/sql/scala/r) x (Shared+Users) created")

    # workspace files (non-notebook)
    files = {f"{SHARED}/config.json": b'{"key":"value"}\n',
             f"{USERDIR}/data.csv": b"a,b,c\n1,2,3\n",
             f"{SHARED}/script.sh": b"#!/bin/bash\necho hi\n"}
    for path, content in files.items():
        try:
            w.workspace.upload(path=path, content=content,
                               format=workspace.ImportFormat.RAW, overwrite=True)
        except Exception as e:
            log(f"file {path}: {str(e)[:60]}")
    log("workspace files created")

    # >10MB FILE (edge case: file streaming works)
    big_path = f"{USERDIR}/wsmig_test_big_file.csv"
    try:
        w.api_client.do("POST", f"/api/2.0/workspace-files/import-file{big_path}",
                        query={"overwrite": "true"},
                        data=b"col1,col2\n" + b"x" * (11 * 1024 * 1024),
                        headers={"Content-Type": "application/octet-stream"})
        log("11MB file created (edge case)")
    except Exception as e:
        log(f"big file: {str(e)[:90]}")

    # >10MB NOTEBOOK edge case — PROVE it cannot be created as a notebook
    big_nb = "# Databricks notebook source\n" + "# " + "y" * 80 + "\n" * 1
    big_nb_content = "# Databricks notebook source\n" + ("# filler " + "y" * 80 + "\n") * 150000
    try:
        w.workspace.import_(path=f"{USERDIR}/wsmig_test_big_nb",
                            language=workspace.Language.PYTHON,
                            format=workspace.ImportFormat.SOURCE,
                            content=base64.b64encode(big_nb_content.encode()).decode(),
                            overwrite=True)
        log("!! big notebook import unexpectedly SUCCEEDED (size=%d)" % len(big_nb_content))
    except Exception as e:
        log(f">10MB notebook import correctly REJECTED: {str(e)[:80]}")

    # object ACL on a notebook
    try:
        st = w.workspace.get_status(f"{SHARED}/py_nb")
        w.api_client.do("PATCH", f"/api/2.0/permissions/notebooks/{st.object_id}", body={
            "access_control_list": [{"group_name": "wsmig_test_child_grp",
                                     "permission_level": "CAN_READ"}]})
        log("notebook ACL grant added")
    except Exception as e:
        log(f"nb acl: {str(e)[:60]}")

    # a Repo (git folder) — public repo, no creds needed to register
    try:
        w.repos.create(url="https://github.com/databricks/databricks-sdk-py",
                       provider="gitHub", path=f"/Repos/{ME}/wsmig_test_repo")
        log("repo created")
    except Exception as e:
        log(f"repo: {str(e)[:80]}")


# ─────────────────────────── secrets ───────────────────────────────────────

def phase_secrets():
    print("== secrets ==")
    scope = "wsmig_test_scope"
    try:
        w.secrets.create_scope(scope=scope)
        log(f"secret scope: {scope}")
    except Exception as e:
        log(f"scope: {str(e)[:60]}")
    for k, v in [("api_key", "secret-value-1"), ("db_pass", "secret-value-2")]:
        try:
            w.secrets.put_secret(scope=scope, key=k, string_value=v)
        except Exception as e:
            log(f"secret {k}: {str(e)[:50]}")
    log("secret keys added (values non-exportable)")
    # ACL on the scope
    try:
        w.secrets.put_acl(scope=scope, principal="wsmig_test_child_grp",
                          permission=__import__("databricks.sdk.service.workspace",
                                                fromlist=["AclPermission"]).AclPermission.READ)
        log("secret scope ACL added")
    except Exception as e:
        log(f"secret acl: {str(e)[:60]}")


# ─────────────────────────── UC tables ─────────────────────────────────────

def phase_uc():
    print("== uc tables (for genie/dashboard refs) ==")
    wh = _warehouse_id()
    stmts = [
        f"CREATE TABLE IF NOT EXISTS {CATALOG}.{SCHEMA}.trips (zip STRING, trips INT, avg_dist DOUBLE)",
        f"INSERT INTO {CATALOG}.{SCHEMA}.trips VALUES ('94103', 120, 3.4), ('94107', 88, 2.1)",
        f"CREATE TABLE IF NOT EXISTS {CATALOG}.{SCHEMA}.zones (zip STRING, borough STRING)",
        f"INSERT INTO {CATALOG}.{SCHEMA}.zones VALUES ('94103','SF'), ('94107','SF')",
    ]
    for s in stmts:
        try:
            r = w.statement_execution.execute_statement(warehouse_id=wh, statement=s, wait_timeout="30s")
            log(f"sql ok: {s[:55]}... [{r.status.state}]")
        except Exception as e:
            log(f"sql err: {str(e)[:70]}")


def _warehouse_id():
    for wh in w.warehouses.list():
        return wh.id
    return None


# ─────────────────────────── SQL (queries + all alert types + legacy dash) ─

def phase_sql():
    print("== sql (queries, legacy alert, alerts v2, legacy dashboard) ==")
    wh = _warehouse_id()
    # 1. Query via the current /api/2.0/sql/queries API (collector tags this legacy_query).
    qid = None
    try:
        from databricks.sdk.service.sql import CreateQueryRequestQuery
        q = w.queries.create(query=CreateQueryRequestQuery(
            display_name="wsmig_test_query", warehouse_id=wh,
            query_text=f"SELECT * FROM {CATALOG}.{SCHEMA}.trips"))
        qid = q.id
        log(f"query: wsmig_test_query ({qid})")
    except Exception as e:
        log(f"query: {str(e)[:90]}")

    # 2. Alerts V2 (/api/2.0/alerts) — the current alert surface.
    try:
        from databricks.sdk.service.sql import (AlertV2, AlertV2Evaluation, AlertV2OperandColumn,
                                                AlertV2Operand, AlertV2OperandValue,
                                                ComparisonOperator, CronSchedule)
        ev = AlertV2Evaluation(
            comparison_operator=ComparisonOperator.GREATER_THAN,
            source=AlertV2OperandColumn(name="c"),
            threshold=AlertV2Operand(value=AlertV2OperandValue(double_value=0)))
        av2 = AlertV2(display_name="wsmig_test_alert_v2", warehouse_id=wh,
                      query_text=f"SELECT count(*) AS c FROM {CATALOG}.{SCHEMA}.trips",
                      evaluation=ev,
                      schedule=CronSchedule(quartz_cron_schedule="0 0 9 * * ?",
                                            timezone_id="UTC"))
        r = w.alerts_v2.create_alert(alert=av2)
        log(f"alert_v2: wsmig_test_alert_v2 ({getattr(r,'id',None)})")
    except Exception as e:
        log(f"alert_v2: {str(e)[:140]}")

    # 3. Legacy alert (/api/2.0/sql/alerts family via alerts_legacy) — needs a legacy query.
    try:
        from databricks.sdk.service.sql import AlertOptions
        lq = w.queries_legacy.create(name="wsmig_test_legacy_q", query="SELECT 1 AS v",
                                     data_source_id=_legacy_data_source_id(wh))
        la = w.alerts_legacy.create(name="wsmig_test_legacy_alert",
                                    query_id=lq.id,
                                    options=AlertOptions(column="v", op=">", value="0"))
        log(f"legacy_alert: wsmig_test_legacy_alert ({la.id})")
    except Exception as e:
        log(f"legacy_alert: {str(e)[:110]}")

    # 4. Legacy dashboard (redash /api/2.0/preview/sql/dashboards; `dashboard_filters_enabled`
    #    is required by the RPC).
    try:
        r = w.api_client.do("POST", "/api/2.0/preview/sql/dashboards",
                            body={"name": "wsmig_test_legacy_dashboard",
                                  "dashboard_filters_enabled": False,
                                  "is_draft": False})
        log(f"legacy_dashboard: wsmig_test_legacy_dashboard ({r.get('id')})")
    except Exception as e:
        log(f"legacy_dashboard: {str(e)[:110]}")


def _legacy_data_source_id(warehouse_id):
    """Legacy query needs a data_source_id (the redash id of a warehouse), not the warehouse id."""
    try:
        for ds in w.api_client.do("GET", "/api/2.0/preview/sql/data_sources") or []:
            if ds.get("warehouse_id") == warehouse_id:
                return ds.get("id")
        # fallback: first data source
        dss = w.api_client.do("GET", "/api/2.0/preview/sql/data_sources") or []
        return dss[0]["id"] if dss else None
    except Exception:
        return None


# ─────────────────────────── Genie space ───────────────────────────────────

def phase_genie():
    print("== genie space ==")
    wh = _warehouse_id()
    serialized = json.dumps({
        "version": 2,
        "data_sources": {"tables": [
            {"identifier": f"{CATALOG}.{SCHEMA}.trips"},
            {"identifier": f"{CATALOG}.{SCHEMA}.zones"}]},
    })
    try:
        r = w.genie.create_space(warehouse_id=wh, serialized_space=serialized,
                                 title="wsmig_test_genie", description="test genie space")
        log(f"genie space: wsmig_test_genie ({getattr(r,'space_id',None)})")
    except Exception as e:
        log(f"genie: {str(e)[:150]}")


# ─────────────────────────── Lakeview (AI/BI) dashboard ────────────────────

def phase_dashboards():
    print("== lakeview (AI/BI) dashboard ==")
    wh = _warehouse_id()
    serialized = json.dumps({
        "datasets": [{"name": "ds1", "displayName": "trips",
                      "queryLines": [f"SELECT * FROM {CATALOG}.{SCHEMA}.trips"]}],
        "pages": [{"name": "p1", "displayName": "Page 1",
                   "layout": [{"position": {"x": 0, "y": 0, "width": 6, "height": 6},
                               "widget": {"name": "w1",
                                          "queries": [{"name": "q1", "query": {
                                              "datasetName": "ds1", "fields": [
                                                  {"name": "zip", "expression": "`zip`"}],
                                              "disaggregated": True}}],
                                          "spec": {"version": 1, "widgetType": "table",
                                                   "encodings": {}}}}]}],
    })
    try:
        from databricks.sdk.service.dashboards import Dashboard
        d = w.lakeview.create(dashboard=Dashboard(
            display_name="wsmig_test_dashboard", warehouse_id=wh,
            serialized_dashboard=serialized))
        log(f"lakeview dashboard: wsmig_test_dashboard ({d.dashboard_id})")
    except Exception as e:
        log(f"lakeview: {str(e)[:150]}")


# ─────────────────────────── jobs (plain, non-DAB) ─────────────────────────

def phase_jobs():
    print("== jobs (multi-task, single-task, scheduled) ==")
    from databricks.sdk.service import jobs
    nb = f"{SHARED}/py_nb"
    # single-task job
    try:
        j = w.jobs.create(name="wsmig_test_single_job",
                          tasks=[jobs.Task(task_key="t1",
                                           notebook_task=jobs.NotebookTask(notebook_path=nb),
                                           new_cluster=_job_cluster())])
        log(f"single-task job: wsmig_test_single_job ({j.job_id})")
    except Exception as e:
        log(f"single job: {str(e)[:90]}")
    # multi-task job with a schedule
    try:
        j = w.jobs.create(
            name="wsmig_test_multi_job",
            tasks=[jobs.Task(task_key="a", notebook_task=jobs.NotebookTask(notebook_path=nb),
                             new_cluster=_job_cluster()),
                   jobs.Task(task_key="b", depends_on=[jobs.TaskDependency(task_key="a")],
                             notebook_task=jobs.NotebookTask(notebook_path=nb),
                             new_cluster=_job_cluster())],
            schedule=jobs.CronSchedule(quartz_cron_expression="0 0 12 * * ?",
                                       timezone_id="UTC",
                                       pause_status=jobs.PauseStatus.PAUSED))
        log(f"multi-task scheduled job: wsmig_test_multi_job ({j.job_id})")
        # ACL grant on the job
        w.api_client.do("PATCH", f"/api/2.0/permissions/jobs/{j.job_id}", body={
            "access_control_list": [{"group_name": "wsmig_test_plain_grp",
                                     "permission_level": "CAN_VIEW"}]})
        log("  + job ACL grant added")
    except Exception as e:
        log(f"multi job: {str(e)[:90]}")


def _job_cluster():
    from databricks.sdk.service import compute
    return compute.ClusterSpec(spark_version="16.4.x-scala2.12", node_type_id="Standard_DS3_v2",
                               num_workers=1)


# ─────────────────────────── DLT pipeline (plain, non-DAB) ─────────────────

def phase_dlt():
    print("== dlt pipeline ==")
    from databricks.sdk.service import pipelines
    # a DLT pipeline needs a notebook with a dlt definition
    from databricks.sdk.service import workspace as wssvc
    dlt_src = ("# Databricks notebook source\n"
               "import dlt\n"
               "@dlt.table\n"
               "def wsmig_test_bronze():\n"
               f"    return spark.read.table('{CATALOG}.{SCHEMA}.trips')\n")
    dlt_nb = f"{SHARED}/dlt_nb"
    try:
        w.workspace.import_(path=dlt_nb, language=wssvc.Language.PYTHON,
                            format=wssvc.ImportFormat.SOURCE,
                            content=base64.b64encode(dlt_src.encode()).decode(), overwrite=True)
    except Exception as e:
        log(f"dlt nb: {str(e)[:60]}")
    try:
        p = w.pipelines.create(
            name="wsmig_test_pipeline",
            libraries=[pipelines.PipelineLibrary(notebook=pipelines.NotebookLibrary(path=dlt_nb))],
            catalog=CATALOG, target=SCHEMA, development=True, serverless=True)
        log(f"dlt pipeline: wsmig_test_pipeline ({p.pipeline_id})")
    except Exception as e:
        log(f"dlt pipeline: {str(e)[:120]}")


# ─────────────────────────── misc (GIS, cluster lib, ws conf) ──────────────

def phase_misc():
    print("== misc (global init script, workspace conf) ==")
    # global init script
    try:
        gis = w.global_init_scripts.create(
            name="wsmig_test_gis",
            script=base64.b64encode(b"#!/bin/bash\necho wsmig-test\n").decode(),
            enabled=False)
        log(f"global init script: wsmig_test_gis ({gis.script_id})")
    except Exception as e:
        log(f"gis: {str(e)[:90]}")
    # workspace conf (set a documented key)
    try:
        w.api_client.do("PATCH", "/api/2.0/workspace-conf",
                        body={"enableExportNotebook": "true"})
        log("workspace conf set (enableExportNotebook)")
    except Exception as e:
        log(f"ws conf: {str(e)[:70]}")
    log("note: cluster libraries require a RUNNING cluster to install — skipped (would need cluster start)")


# ─────────────────────────── serving (external model endpoint) ─────────────

def phase_serving():
    """External-model serving endpoint = the only auto-migratable serving kind.

    Created via RAW REST (the SDK's EndpointCoreConfigInput errors with "missing name").
    Uses a dummy api key — the endpoint only has to EXIST for export to capture it.
    """
    print("== serving (external model endpoint) ==")
    try:
        r = w.api_client.do("POST", "/api/2.0/serving-endpoints", body={
            "name": "wsmig_test_ext_endpoint",
            "config": {"served_entities": [{
                "name": "openai_gpt",
                "external_model": {
                    "name": "gpt-4o-mini", "provider": "openai", "task": "llm/v1/chat",
                    "openai_config": {"openai_api_key_plaintext": "sk-dummy-not-a-real-key"},
                },
            }]},
        })
        log(f"serving endpoint: wsmig_test_ext_endpoint ({r.get('id')})")
    except Exception as e:
        log(f"serving: {str(e)[:160]}")


# ─────────────────────────── AKV-backed secret scope ───────────────────────

AKV_RG = "wsmig-test-rg"
AKV_LOCATION = "eastus2"
AKV_TENANT = "bf465dc7-3bc8-4944-b018-092572b5c20d"
# The AzureDatabricks first-party application id — the resource an AAD token must target
# so the workspace can verify Key Vault control when registering an AKV-backed scope.
AZURE_DATABRICKS_APP_ID = "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d"


def phase_akv():
    """AKV-backed secret scope.

    Registering one needs an **Azure AD** bearer token for the AzureDatabricks resource — a
    Databricks OAuth token carries no AAD identity and the RPC fails with
    "must have userAADToken defined!". So we mint the AAD token with the az CLI and POST
    secrets/scopes ourselves rather than going through the SDK client.
    """
    import subprocess

    print("== akv-backed secret scope ==")
    vault = f"wsmigtestkv{ME.split('@')[0].replace('.', '')[:8]}"

    def az(*args):
        return subprocess.run(["az", *args], capture_output=True, text=True)

    # The db_fe management group denies any resource without an `owner` tag, so tag both.
    tags = [f"owner={ME}", "purpose=wsmig-export-test"]
    r = az("group", "create", "-n", AKV_RG, "-l", AKV_LOCATION, "--tags", *tags, "-o", "json")
    if r.returncode:
        log(f"rg: {r.stderr[:120]}")
        return
    r = az("keyvault", "create", "-n", vault, "-g", AKV_RG, "-l", AKV_LOCATION,
           "--enable-rbac-authorization", "false", "--tags", *tags, "-o", "json")
    if r.returncode:
        # may already exist from a prior run — fall back to reading it
        r2 = az("keyvault", "show", "-n", vault, "-g", AKV_RG, "-o", "json")
        if r2.returncode:
            log(f"vault create: {(r.stderr or '')[:300]}")
            return
        r = r2
    vinfo = json.loads(r.stdout)
    vault_id = vinfo["id"]
    vault_uri = vinfo["properties"]["vaultUri"]
    log(f"key vault: {vault} ({vault_uri})")

    # a secret in the vault so the scope has content to enumerate
    az("keyvault", "secret", "set", "--vault-name", vault, "-n", "wsmig-akv-key",
       "--value", "akv-secret-value", "-o", "none")

    # AAD token for the AzureDatabricks resource (NOT the Databricks OAuth token)
    r = az("account", "get-access-token", "--resource", AZURE_DATABRICKS_APP_ID,
           "--tenant", AKV_TENANT, "-o", "json")
    if r.returncode:
        log(f"aad token: {r.stderr[:160]}")
        return
    aad = json.loads(r.stdout)["accessToken"]

    import requests
    resp = requests.post(
        f"{_host()}/api/2.0/secrets/scopes/create",
        headers={"Authorization": f"Bearer {aad}"},
        json={"scope": "wsmig_test_akv_scope",
              "scope_backend_type": "AZURE_KEYVAULT",
              "backend_azure_keyvault": {"resource_id": vault_id, "dns_name": vault_uri},
              "initial_manage_principal": "users"},
        timeout=60)
    if resp.status_code == 200:
        log("AKV-backed secret scope created: wsmig_test_akv_scope")
    else:
        log(f"akv scope: HTTP {resp.status_code} {resp.text[:250]}")


def _host():
    import configparser
    c = configparser.ConfigParser()
    c.read(__import__("os").path.expanduser("~/.databrickscfg"))
    return dict(c[PROFILE])["host"].rstrip("/")


# ─────────────────────────── DAB bundles ───────────────────────────────────

def phase_dab():
    """Deploy REAL Databricks Asset Bundles so the export's DAB detection is exercised.

    Two bundles — one landing in /Users/<me>/.bundle/, one in /Shared/.bundle/ — each with a
    job + a pipeline + a dashboard, so DAB-deployed twins of those asset types exist alongside
    the manually-created ones.

    `databricks bundle deploy` shells out to terraform; the account's PGP signing key is
    expired, so we point the CLI at a pre-downloaded terraform binary instead of letting it
    fetch+verify one.
    """
    import os
    import subprocess
    import tempfile
    print("== dab bundles ==")
    tf = "/tmp/tfbin/terraform"
    if not os.path.isfile(tf):
        log(f"terraform binary missing at {tf} — run the tf download first; skipping DAB")
        return
    env = dict(os.environ, DATABRICKS_TF_EXEC_PATH=tf, DATABRICKS_TF_VERSION="1.9.8")
    wh = _warehouse_id()

    dash_serialized = json.dumps({
        "datasets": [{"name": "ds1", "displayName": "trips",
                      "queryLines": [f"SELECT * FROM {CATALOG}.{SCHEMA}.trips"]}],
        "pages": [{"name": "p1", "displayName": "Page 1",
                   "layout": [{"position": {"x": 0, "y": 0, "width": 6, "height": 6},
                               "widget": {"name": "w1",
                                          "queries": [{"name": "q1", "query": {
                                              "datasetName": "ds1",
                                              "fields": [{"name": "zip", "expression": "`zip`"}],
                                              "disaggregated": True}}],
                                          "spec": {"version": 1, "widgetType": "table",
                                                   "encodings": {}}}}]}],
    })

    for tag, root_path in (("shared", "/Shared/.bundle/wsmig_test_shared"),
                           ("user", f"/Users/{ME}/.bundle/wsmig_test_user")):
        d = tempfile.mkdtemp(prefix=f"wsmig_dab_{tag}_")
        # bundle sources
        with open(f"{d}/dab_nb.py", "w") as f:
            f.write("# Databricks notebook source\nprint('dab notebook')\n")
        with open(f"{d}/dab_dlt.py", "w") as f:
            f.write("# Databricks notebook source\nimport dlt\n@dlt.table\n"
                    f"def wsmig_dab_{tag}_bronze():\n"
                    f"    return spark.read.table('{CATALOG}.{SCHEMA}.trips')\n")
        with open(f"{d}/dab_dash.lvdash.json", "w") as f:
            f.write(dash_serialized)
        bundle = f"""
bundle:
  name: wsmig_test_{tag}

workspace:
  root_path: {root_path}

resources:
  jobs:
    wsmig_dab_{tag}_job:
      name: wsmig_dab_{tag}_job
      tasks:
        - task_key: t1
          notebook_task:
            notebook_path: ./dab_nb.py
          new_cluster:
            spark_version: 16.4.x-scala2.12
            node_type_id: Standard_DS3_v2
            num_workers: 1
  pipelines:
    wsmig_dab_{tag}_pipeline:
      name: wsmig_dab_{tag}_pipeline
      catalog: {CATALOG}
      target: {SCHEMA}
      serverless: true
      libraries:
        - notebook:
            path: ./dab_dlt.py
  dashboards:
    wsmig_dab_{tag}_dashboard:
      display_name: wsmig_dab_{tag}_dashboard
      warehouse_id: {wh}
      file_path: ./dab_dash.lvdash.json
"""
        with open(f"{d}/databricks.yml", "w") as f:
            f.write(bundle)
        r = subprocess.run(["databricks", "bundle", "deploy", "-p", PROFILE],
                           cwd=d, capture_output=True, text=True, env=env)
        if r.returncode:
            log(f"dab {tag} deploy FAILED: {(r.stderr or r.stdout)[-400:]}")
        else:
            log(f"dab {tag} deployed → {root_path}")


# ─────────────────────────── cluster libraries ─────────────────────────────

def phase_libraries():
    """Install all three library kinds on a RUNNING cluster.

    Libraries can only be installed on a running cluster, so this starts it (and leaves it to
    autoterminate). Covers the migration-relevant distinction:
      • pypi / maven  → re-resolve from their repos on target        → auto-migratable
      • jar on dbfs:/ → the FILE is never exported (DBFS out of scope) → must be flagged manual
    """
    print("== cluster libraries ==")
    cid = None
    for c in w.clusters.list():
        if c.cluster_name == "wsmig_test_cluster":
            cid = c.cluster_id
    if not cid:
        log("wsmig_test_cluster not found — run the compute phase first")
        return
    log(f"starting cluster {cid} (libraries need it RUNNING)…")
    try:
        w.clusters.start_and_wait(cluster_id=cid, timeout=__import__("datetime").timedelta(minutes=15))
    except Exception as e:
        if "already" not in str(e).lower() and "unexpected state" not in str(e).lower():
            log(f"cluster start: {str(e)[:110]}")

    # a dummy jar on DBFS so the dangling-reference case is real
    try:
        w.api_client.do("POST", "/api/2.0/dbfs/put",
                        body={"path": "/FileStore/wsmig_test/wsmig_dummy.jar",
                              "contents": base64.b64encode(b"PK\x03\x04dummy-jar-bytes").decode(),
                              "overwrite": True})
        log("dbfs jar staged: dbfs:/FileStore/wsmig_test/wsmig_dummy.jar")
    except Exception as e:
        log(f"dbfs put: {str(e)[:90]}")

    try:
        w.api_client.do("POST", "/api/2.0/libraries/install", body={
            "cluster_id": cid,
            "libraries": [{"pypi": {"package": "tabulate==0.9.0"}},
                          {"maven": {"coordinates": "com.google.code.gson:gson:2.10.1"}},
                          {"jar": "dbfs:/FileStore/wsmig_test/wsmig_dummy.jar"}]})
        log("libraries installed: pypi + maven + dbfs jar")
    except Exception as e:
        log(f"library install: {str(e)[:110]}")
    time.sleep(20)
    try:
        st = w.api_client.do("GET", "/api/2.0/libraries/cluster-status",
                             query={"cluster_id": cid})
        for ls in st.get("library_statuses", []):
            log(f"  {json.dumps(ls['library'])[:60]} → {ls['status']}")
    except Exception as e:
        log(f"status: {str(e)[:80]}")


# ─────────────────────────── oversize workspace files ──────────────────────

def phase_bigfiles():
    """Large workspace files, for the oversize/size-tier reporting path.

    Notes on the real caps (verified live):
      • a >10 MB NOTEBOOK cannot be created at all — the import API rejects it, and uploading
        a >10 MB `.py` with the notebook header fails the same way. So the oversize-NOTEBOOK row
        is only reachable offline (tests/test_export) or by lowering the cap.
      • workspace FILES cap at 500 MB. 60/120 MB files upload fine and export fine; they exist so
        tests/live_fvm1_oversize.py can trip a lowered cap and show the real report rows.
    """
    print("== oversize workspace files ==")
    for mb, name in ((60, "wsmig_test_60mb.bin"), (120, "wsmig_test_120mb.bin")):
        path = f"{USERDIR}/{name}"
        try:
            w.api_client.do("POST", f"/api/2.0/workspace-files/import-file{path}",
                            query={"overwrite": "true"}, data=b"x" * (mb * 1024 * 1024),
                            headers={"Content-Type": "application/octet-stream"})
            log(f"{mb}MB file created: {name}")
        except Exception as e:
            log(f"{mb}MB file: {str(e)[:90]}")
    # prove the >10MB notebook really is impossible (documents the limit rather than hiding it)
    big = "# Databricks notebook source\n" + ("# filler " + "z" * 80 + "\n") * 140000
    try:
        w.api_client.do("POST", f"/api/2.0/workspace-files/import-file{USERDIR}/wsmig_big_nb.py",
                        query={"overwrite": "true"}, data=big.encode(),
                        headers={"Content-Type": "application/octet-stream"})
        log("!! >10MB .py unexpectedly accepted")
    except Exception as e:
        log(f">10MB notebook-source correctly REJECTED: {str(e)[:80]}")


# ─────────────────────────── DAB: pathless assets + genie ──────────────────

def phase_dab_pathless():
    """Deploy DAB-managed assets that have NO workspace path, plus a DAB Genie space.

    These are the cases path-based `.bundle/` detection CANNOT see (a cluster/pool/warehouse/
    scope has no workspace path at all), so they exercise the bundle-state-file detection in
    src/collectors/dab_registry.py. `genie_spaces` became a bundle resource type in CLI v1.10.0.

    Needs a CLI >= 1.10.0 — set WSMIG_CLI to point at one if the default `databricks` is older.
    """
    import os
    import subprocess
    import tempfile
    print("== dab pathless assets + genie space ==")
    cli = os.environ.get("WSMIG_CLI", "databricks")
    ver = subprocess.run([cli, "--version"], capture_output=True, text=True).stdout.strip()
    log(f"using CLI: {ver}")
    tf = "/tmp/tfbin/terraform"
    env = dict(os.environ, DATABRICKS_TF_EXEC_PATH=tf, DATABRICKS_TF_VERSION="1.9.8")
    wh = _warehouse_id()

    d = tempfile.mkdtemp(prefix="wsmig_dab_pathless_")
    with open(f"{d}/dab_genie.geniespace.json", "w") as f:
        json.dump({"version": 2, "data_sources": {
            "tables": [{"identifier": f"{CATALOG}.{SCHEMA}.trips"}]}}, f)
    with open(f"{d}/databricks.yml", "w") as f:
        f.write(f"""
bundle:
  name: wsmig_test_pathless

workspace:
  root_path: /Shared/.bundle/wsmig_test_pathless

resources:
  clusters:
    wsmig_dab_cluster:
      cluster_name: wsmig_dab_cluster
      spark_version: 16.4.x-scala2.12
      node_type_id: Standard_DS3_v2
      num_workers: 1
      autotermination_minutes: 10
  instance_pools:
    wsmig_dab_pool:
      instance_pool_name: wsmig_dab_pool
      node_type_id: Standard_DS3_v2
      min_idle_instances: 0
  sql_warehouses:
    wsmig_dab_wh:
      name: wsmig_dab_wh
      cluster_size: 2X-Small
      max_num_clusters: 1
  secret_scopes:
    wsmig_dab_scope:
      name: wsmig_dab_scope
  genie_spaces:
    wsmig_dab_genie:
      title: wsmig_dab_genie
      description: DAB-deployed genie space
      warehouse_id: {wh}
      file_path: ./dab_genie.geniespace.json
""")
    r = subprocess.run([cli, "bundle", "deploy", "-p", PROFILE],
                       cwd=d, capture_output=True, text=True, env=env)
    if r.returncode:
        log(f"deploy FAILED: {(r.stderr or r.stdout)[-500:]}")
    else:
        log("deployed: cluster + pool + warehouse + secret scope + genie space (all DAB-owned)")


if __name__ == "__main__":
    phase = sys.argv[1] if len(sys.argv) > 1 else "all"
    phases = {"identity": phase_identity, "compute": phase_compute, "workspace": phase_workspace,
              "secrets": phase_secrets, "akv": phase_akv, "uc": phase_uc, "sql": phase_sql,
              "genie": phase_genie, "dashboards": phase_dashboards, "jobs": phase_jobs,
              "dlt": phase_dlt, "serving": phase_serving, "dab": phase_dab,
              "dab_pathless": phase_dab_pathless, "libraries": phase_libraries,
              "bigfiles": phase_bigfiles, "misc": phase_misc}
    if phase == "all":
        for fn in phases.values():
            fn()
    elif phase in phases:
        phases[phase]()
    else:
        print("unknown phase:", phase, "| known:", list(phases) + ["all"])
