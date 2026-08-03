"""Offline tests for every source-side collector + the classifier.

Run: python3 -m tests.test_collectors   (from the repo root; no Databricks/network needed).
"""
from __future__ import annotations

from src.config.config_manager import Config
from tests.fakes import FakeClient


def _cfg():
    return Config.from_dict({"role": "source", "source_workspace_id": "1", "run_id": "r",
                             "source_staging_location": "/Volumes/a/b/c"})


def _run_ok(coll):
    objs = coll.run()
    assert not coll.stats()["errors"], (coll.object_type, coll.stats()["errors"])
    return objs


def test_identity_and_classifier():
    from src.collectors.identity_collector import IdentityCollector
    from src.identity.classifier import classify_all, IdentityClass
    scim = {
        "Users": [{"id": "u1", "userName": "alice@corp.com",
                   "emails": [{"value": "alice@corp.com", "primary": True}],
                   "externalId": "ext", "entitlements": [{"value": "allow-cluster-create"}]}],
        "ServicePrincipals": [
            {"id": "s1", "applicationId": "app-entra", "displayName": "umi", "externalId": "e"},
            {"id": "s2", "applicationId": "app-dbx", "displayName": "dbx"}],
        "Groups": [
            {"id": "g1", "displayName": "acct", "externalId": "eg",
             "members": [{"value": "u1", "display": "alice@corp.com", "$ref": "Users/u1"}]},
            {"id": "g2", "displayName": "eng", "members": [
                {"value": "g1", "display": "acct", "$ref": "Groups/g1"},
                {"value": "app-dbx", "display": "dbx", "$ref": "ServicePrincipals/s2"}]},
            {"id": "g3", "displayName": "admins", "members": []},   # built-in — never recreate
            {"id": "g4", "displayName": "users", "members": []}],   # built-in — never recreate
    }
    # SP OAuth-secrets proxy: s2 has a secret, s1 has none (Plan 1a §6).
    gt = {"api/2.0/accounts/servicePrincipals/s2/credentials/secrets":
          {"secrets": [{"id": "sec1", "status": "ACTIVE"}]},
          "api/2.0/accounts/servicePrincipals/s1/credentials/secrets": {}}
    coll = IdentityCollector(FakeClient(get_table=gt, scim_table=scim), _cfg())
    objs = _run_ok(coll)
    assert len(objs) == 7
    eng = next(o for o in objs if o.get("displayName") == "eng")
    assert eng["has_nested_groups"] and eng["member_count"] == 2
    sps = {o.get("applicationId"): o for o in objs if o.get("identity_type") == "service_principal"}
    assert sps["app-dbx"]["has_secrets"] is True and sps["app-entra"]["has_secrets"] is False
    classify_all(objs)
    byname = {(o.get("userName") or o.get("applicationId") or o.get("displayName")): o["classification"] for o in objs}
    assert byname["alice@corp.com"] == IdentityClass.ENTRA_USER.value
    assert byname["app-entra"] == IdentityClass.UMI_OR_ENTRA_SP.value
    assert byname["app-dbx"] == IdentityClass.DB_MANAGED_SP.value
    assert byname["acct"] == IdentityClass.ACCOUNT_GROUP.value
    assert byname["eng"] == IdentityClass.DB_MANAGED_GROUP.value
    assert byname["admins"] == IdentityClass.BUILTIN_GROUP.value
    assert byname["users"] == IdentityClass.BUILTIN_GROUP.value


def test_azure_umi_sp_classifies_as_account_managed():
    """A real Azure managed identity imported as a Databricks SP must be ASSIGN, not CREATE.

    Verbatim SCIM shape of `ai27_umi` on fvm1 (a UMI created in Azure then imported into the
    workspace) — the first genuinely account-managed SP available to test against. Two details
    the synthetic fixture missed:
      * the workspace SCIM API returns `externalId: null` (an explicit JSON null) for
        Databricks-managed SPs, not an absent key — `_has_external_id` must treat both alike;
      * classification must flow through to `import_action`, since recreating a UMI on the
        target would mint a NEW appId and silently break every ACL that referenced it.
    """
    from src.identity.classifier import classify_all, IdentityClass
    from src.exporters.asset_export import build_all
    objs = [
        {"identity_type": "service_principal", "id": "147404773209245",
         "displayName": "ai27_umi", "applicationId": "5f491556-5401-4d38-b0b7-16ffd932f073",
         "externalId": "8904a5fb-c70c-4d33-b6de-4a4db708a5b4", "active": True,
         "_raw": {"applicationId": "5f491556-5401-4d38-b0b7-16ffd932f073",
                  "displayName": "ai27_umi",
                  "externalId": "8904a5fb-c70c-4d33-b6de-4a4db708a5b4", "active": True}},
        # Databricks-managed: the API sends an explicit null here, not a missing field.
        {"identity_type": "service_principal", "id": "147439412262841",
         "displayName": "wsmig_test_db_sp", "applicationId": "18abea3e-5de8-4f74-b678-de67cf2270a2",
         "externalId": None, "active": True,
         "_raw": {"applicationId": "18abea3e-5de8-4f74-b678-de67cf2270a2",
                  "displayName": "wsmig_test_db_sp", "externalId": None, "active": True}},
    ]
    classify_all(objs)
    by_name = {o["displayName"]: o for o in objs}
    assert by_name["ai27_umi"]["classification"] == IdentityClass.UMI_OR_ENTRA_SP.value
    assert by_name["wsmig_test_db_sp"]["classification"] == IdentityClass.DB_MANAGED_SP.value

    units = {u["natural_key"]: u for u in build_all({"identity": objs})["service_principal"]}
    umi = units["5f491556-5401-4d38-b0b7-16ffd932f073"]
    assert umi["import_action"] == "assign_on_target", \
        "recreating a UMI would mint a new appId and orphan its ACLs"
    assert units["18abea3e-5de8-4f74-b678-de67cf2270a2"]["import_action"] == "create"
    # the stable Azure appId is what the target assigns by, so it must survive the export
    assert umi["payload"]["applicationId"] == "5f491556-5401-4d38-b0b7-16ffd932f073"

    # and the report labels it as Entra-managed rather than blank
    from src.reports.inventory_view import _sp_managed
    assert _sp_managed(by_name["ai27_umi"]["classification"]) == "Entra / UMI"


def test_compute():
    from src.collectors.compute_collector import ComputeCollector
    gt = {
        "api/2.0/instance-pools/list": {"instance_pools": [{"instance_pool_id": "p1", "instance_pool_name": "pool"}]},
        "api/2.0/policies/clusters/list": {"policies": [{"policy_id": "pol", "name": "policy-1"}]},
        "api/2.0/clusters/list": {"clusters": [
            {"cluster_id": "c1", "cluster_name": "analytics", "cluster_source": "UI", "pinned_by_user_name": "x"},
            {"cluster_id": "c2", "cluster_name": "job-1-run-2", "cluster_source": "JOB"}]},
        "api/2.0/permissions/instance-pools/p1": {"access_control_list": []},
        "api/2.0/permissions/cluster-policies/pol": {"access_control_list": []},
        "api/2.0/permissions/clusters/c1": {"access_control_list": []},
    }
    objs = _run_ok(ComputeCollector(FakeClient(get_table=gt), _cfg()))
    clusters = [o for o in objs if o["compute_type"] == "cluster"]
    # Ephemeral job cluster (c2, job-1-run-2 / source JOB) is dropped entirely (Plan 1a §8);
    # only the all-purpose cluster remains.
    assert len(clusters) == 1 and clusters[0]["cluster_name"] == "analytics"
    assert clusters[0]["pinned"] is True
    assert all("ephemeral" not in c for c in clusters)


def test_secrets_akv():
    from src.collectors.secrets_collector import SecretsCollector
    gt = {
        "api/2.0/secrets/scopes/list": {"scopes": [
            {"name": "dbx", "backend_type": "DATABRICKS"},
            {"name": "kv", "backend_type": "AZURE_KEYVAULT", "keyvault_metadata": {"dns_name": "u", "resource_id": "r"}}]},
        "api/2.0/secrets/acls/list": {"items": [{"principal": "admins", "permission": "MANAGE"}]},
        "api/2.0/secrets/list": {"secrets": [{"key": "k1"}]},
    }
    objs = _run_ok(SecretsCollector(FakeClient(get_table=gt), _cfg()))
    kv = next(o for o in objs if o["name"] == "kv")
    assert kv["backend_type"] == "AZURE_KEYVAULT" and kv["keyvault_metadata"]["dns_name"] == "u"
    assert kv["values_migratable"] is False and kv["key_names"] == ["k1"]


def test_jobs_multitask():
    from src.collectors.jobs_collector import JobsCollector
    pag = {"api/2.1/jobs/list": [
        {"job_id": "j1", "settings": {"name": "nightly", "format": "MULTI_TASK",
                                      "tasks": [{"task_key": "t"}]}},
        {"job_id": "j2", "settings": {"name": "bundle-job", "format": "MULTI_TASK",
                                      "deployment": {"kind": "BUNDLE"}}},
        {"job_id": "j3", "settings": {"name": "real-multi", "format": "MULTI_TASK",
                                      "tasks": [{"task_key": "a"}, {"task_key": "b"}]}}]}
    gt = {"api/2.0/permissions/jobs/j1": {"access_control_list": [
              {"all_permissions": [{"permission_level": "IS_OWNER"}]}]},
          "api/2.0/permissions/jobs/j2": {"access_control_list": []},
          "api/2.0/permissions/jobs/j3": {"access_control_list": []}}
    objs = _run_ok(JobsCollector(FakeClient(get_table=gt, paginated_table=pag), _cfg()))
    byname = {o["name"]: o for o in objs}
    # nightly has 1 task → honest job_type SINGLE_TASK even though the API says MULTI_TASK (point 1).
    assert byname["nightly"]["format"] == "MULTI_TASK"
    assert byname["nightly"]["task_count"] == 1 and byname["nightly"]["job_type"] == "SINGLE_TASK"
    assert byname["real-multi"]["task_count"] == 2 and byname["real-multi"]["job_type"] == "MULTI_TASK"
    # DAB detection (Plan 1a §4).
    assert byname["nightly"]["deployed_by_dab"] is False
    assert byname["bundle-job"]["deployed_by_dab"] is True


def test_sql_legacy_names():
    from src.collectors.sql_collector import SqlCollector
    gt = {"api/2.0/sql/warehouses": {"warehouses": [{"id": "w1", "name": "wh", "warehouse_type": "PRO"}]},
          "api/2.0/permissions/sql/warehouses/w1": {"access_control_list": []},
          # legacy SQL ACLs now collected via queries / alerts / dashboards perm types (Plan 1a §1)
          "api/2.0/permissions/queries/q1": {"access_control_list": [
              {"user_name": "a@x.com", "all_permissions": [{"permission_level": "CAN_MANAGE"}]}]},
          "api/2.0/permissions/alerts/a1": {"access_control_list": []},
          "api/2.0/permissions/dashboards/l1": {"access_control_list": []}}
    pag = {"api/2.0/sql/queries": [{"id": "q1", "name": "q"}],
           "api/2.0/sql/alerts": [{"id": "a1", "name": "a"}],
           "api/2.0/sql/dashboards": [{"id": "l1", "name": "ld"}]}
    objs = _run_ok(SqlCollector(FakeClient(get_table=gt, paginated_table=pag), _cfg()))
    assert {o["sql_type"] for o in objs} == {"warehouse", "legacy_query", "legacy_alert", "legacy_dashboard"}
    q = next(o for o in objs if o["sql_type"] == "legacy_query")
    assert q["acl"] and q["acl"][0]["user_name"] == "a@x.com"


def test_dlt_dashboards_genie_serving():
    from src.collectors.dlt_collector import DltCollector
    from src.collectors.dashboards_collector import DashboardsCollector
    from src.collectors.genie_collector import GenieCollector
    from src.collectors.serving_collector import ServingCollector
    gt = {"api/2.0/pipelines/dp1": {"spec": {"name": "bronze"}},
          "api/2.0/lakeview/dashboards/d1": {"display_name": "KPIs", "warehouse_id": "w1", "serialized_dashboard": "{}"},
          "api/2.0/serving-endpoints": {"endpoints": [
              {"name": "my-ep", "id": "e1", "config": {}},
              {"name": "databricks-x"},                                  # platform FM — skipped
              {"name": "mas-abc-endpoint", "id": "e2", "task": "agent/v1/responses"}]},  # Agent Bricks — skipped
          "api/2.0/permissions/pipelines/dp1": {"access_control_list": []},
          "api/2.0/permissions/serving-endpoints/e1": {"access_control_list": []}}
    pag = {"api/2.0/pipelines": [{"pipeline_id": "dp1", "name": "bronze"}],
           "api/2.0/lakeview/dashboards": [{"dashboard_id": "d1", "display_name": "KPIs"}],
           "api/2.0/genie/spaces": [{"space_id": "s1", "title": "Genie", "warehouse_id": "w1"}]}
    c = FakeClient(get_table=gt, paginated_table=pag)
    assert _run_ok(DltCollector(c, _cfg()))[0]["spec"]["name"] == "bronze"
    assert _run_ok(DashboardsCollector(c, _cfg()))[0]["serialized_dashboard"] == "{}"
    g = _run_ok(GenieCollector(c, _cfg()))[0]
    assert g["warehouse_id"] == "w1" and "migratable" not in g  # no migratability flag asserted
    serv = _run_ok(ServingCollector(c, _cfg()))
    # databricks-* (platform FM) AND Agent Bricks agent endpoints (task=agent/*) are excluded.
    assert [o["name"] for o in serv] == ["my-ep"]


def test_misc():
    from src.collectors.misc_collector import MiscCollector
    gt = {"api/2.0/global-init-scripts": {"scripts": [{"script_id": "g1", "name": "gis", "position": 0, "enabled": True}]},
          "api/2.0/global-init-scripts/g1": {"script": "ZWNobw=="},
          "api/2.0/libraries/all-cluster-statuses": {"statuses": [{"cluster_id": "c1", "library_statuses": [{"library": {"pypi": {"package": "requests"}}, "status": "INSTALLED"}]}]},
          "api/2.0/workspace-conf": lambda p: {p["keys"]: "true"}}
    objs = _run_ok(MiscCollector(FakeClient(get_table=gt), _cfg()))
    # IP access lists are EXCLUDED (account-level, not a workspace asset).
    assert {o["misc_type"] for o in objs} == {"global_init_script", "cluster_library", "workspace_conf"}
    assert not any(o["misc_type"] == "ip_access_list" for o in objs)
    assert any(o["misc_type"] == "workspace_conf" and o["key"] == "enableTokensConfig" for o in objs)


def test_workspace_special_paths_and_git_folders():
    from src.collectors.workspace_collector import WorkspaceCollector

    # Two git folders: one under /Repos (legacy), one INSIDE a user folder (modern) — both
    # detected via directory_info.is_git_folder (Plan 1a §3), NOT the /repos list API (empty).
    def ws_list(params):
        p = params.get("path")
        if p == "/":
            return {"objects": [
                {"path": "/Shared", "object_type": "DIRECTORY", "object_id": "1"},
                {"path": "/Repos", "object_type": "DIRECTORY", "object_id": "2"},
                {"path": "/Users/a@x.com", "object_type": "DIRECTORY", "object_id": "3"}]}
        if p == "/Shared":
            return {"objects": [{"path": "/Shared/nb", "object_type": "NOTEBOOK", "language": "PYTHON", "object_id": "10"}]}
        if p == "/Repos":
            return {"objects": [{"path": "/Repos/a@x.com", "object_type": "DIRECTORY", "object_id": "20"}]}
        if p == "/Repos/a@x.com":
            return {"objects": [{"path": "/Repos/a@x.com/legacy-repo", "object_type": "DIRECTORY",
                                 "object_id": "21", "directory_info": {"is_git_folder": True}}]}
        if p == "/Users/a@x.com":
            return {"objects": [
                {"path": "/Users/a@x.com/nb", "object_type": "NOTEBOOK", "language": "PYTHON", "object_id": "30"},
                {"path": "/Users/a@x.com/data.csv", "object_type": "FILE", "object_id": "32"},
                {"path": "/Users/a@x.com/user-repo", "object_type": "DIRECTORY", "object_id": "31",
                 "directory_info": {"is_git_folder": True}}]}
        return {"objects": []}

    gt = {"api/2.0/workspace/list": ws_list,
          "api/2.0/permissions/directories/1": {"access_control_list": []},
          "api/2.0/permissions/directories/3": {"access_control_list": []},
          "api/2.0/permissions/notebooks/10": {"access_control_list": []},
          "api/2.0/permissions/notebooks/30": {"access_control_list": []},
          # FILE objects DO have permissions (point 5 fix).
          "api/2.0/permissions/files/32": {"access_control_list": [
              {"group_name": "eng", "all_permissions": [{"permission_level": "CAN_READ"}]}]},
          # per-git-folder detail (list API returns empty on purpose)
          "api/2.0/repos/21": {"id": "21", "path": "/Repos/a@x.com/legacy-repo",
                               "url": "https://g/legacy", "provider": "gitHub", "branch": "main",
                               "head_commit_id": "abc123"},
          "api/2.0/repos/31": {"id": "31", "path": "/Users/a@x.com/user-repo",
                               "url": "https://g/user", "provider": "gitHub", "branch": "dev",
                               "head_commit_id": "def456"},
          "api/2.0/permissions/repos/21": {"access_control_list": []},
          "api/2.0/permissions/repos/31": {"access_control_list": []}}
    pag = {"api/2.0/repos": []}   # list API empty — must NOT be the source of truth
    objs = _run_ok(WorkspaceCollector(FakeClient(get_table=gt, paginated_table=pag), _cfg()))
    paths = {o["path"] for o in objs}
    # container dirs (/Repos, /Repos/<user>) are NOT emitted as content; content still walked.
    assert "/Repos" not in paths and "/Repos/a@x.com" not in paths
    assert "/Shared/nb" in paths and "/Users/a@x.com/nb" in paths
    # BOTH git folders discovered (legacy + user-folder), with full detail from GET /repos/{id}.
    repos = [o for o in objs if o["object_type"] == "REPO"]
    assert {r["path"] for r in repos} == {"/Repos/a@x.com/legacy-repo", "/Users/a@x.com/user-repo"}
    assert all(r["url"] and r["provider"] and r["branch"] and r["head_commit_id"] for r in repos)
    assert next(o for o in objs if o["path"] == "/Users/a@x.com")["is_user_root"] is True
    # FILE ACLs are now fetched (point 5).
    f = next(o for o in objs if o["path"] == "/Users/a@x.com/data.csv")
    assert f["acl"] and f["acl"][0]["group_name"] == "eng"


def test_apps_and_lakebase_inventory_only():
    from src.collectors.apps_collector import AppsCollector
    from src.collectors.lakebase_collector import LakebaseCollector

    pag = {
        "api/2.0/apps": [{"name": "my-app", "description": "d", "creator": "a@x.com",
                          "url": "https://app", "app_status": {"state": "RUNNING"}}],
        "api/2.0/postgres/projects": [{"name": "lb1", "status": {"display_name": "pg",
                                                                 "pg_version": "15"}}],
    }
    apps = _run_ok(AppsCollector(FakeClient(paginated_table=pag), _cfg()))
    assert len(apps) == 1 and apps[0]["migratable"] is False and apps[0]["_raw"]["name"] == "my-app"
    lb = _run_ok(LakebaseCollector(FakeClient(paginated_table=pag), _cfg()))
    assert len(lb) == 1 and lb[0]["migratable"] is False and lb[0]["pg_version"] == "15"


def test_view_adapter_acls_and_managed_metadata():
    """The report adapter must (a) flatten every ACL grant (incl. dashboards/genie/legacy-sql)
    into a countable row and (b) surface migration-critical metadata as columns:
    Managed By, has_secrets, deployed_by_dab."""
    from src.reports.inventory_view import adapt, build_counts

    acl = [{"user_name": "a@x.com",
            "all_permissions": [{"permission_level": "CAN_MANAGE", "inherited": False}]},
           {"group_name": "eng",
            "all_permissions": [{"permission_level": "CAN_VIEW", "inherited": False}]}]
    one = [{"user_name": "a@x.com", "all_permissions": [{"permission_level": "CAN_VIEW"}]}]
    obt = {
        "identity": [
            {"identity_type": "group", "id": "3", "displayName": "eng", "classification":
             "db_managed_group", "member_count": 5, "has_nested_groups": True,
             "entitlements": ["databricks-sql-access"], "roles": [], "_raw": {"id": "3"}},
            {"identity_type": "service_principal", "id": "2", "applicationId": "app-2",
             "classification": "umi_or_entra_sp", "entitlements": [], "has_secrets": True,
             "_raw": {"id": "2"}},
        ],
        "compute": [{"compute_type": "cluster", "cluster_id": "c1", "cluster_name": "cl",
                     "pinned": True, "acl": acl, "_raw": {"cluster_id": "c1"}}],
        "job": [{"job_id": "j1", "name": "bundle-job", "deployed_by_dab": True,
                 "acl": one, "settings": {"name": "bundle-job"}, "_raw": {"job_id": "j1"}}],
        "lakeview_dashboard": [{"dashboard_id": "d1", "display_name": "KPIs", "acl": one,
                                "_raw": {"dashboard_id": "d1", "display_name": "KPIs"}}],
        "genie_space": [{"space_id": "g1", "title": "Genie", "migratable": False, "acl": one,
                         "_raw": {"space_id": "g1", "title": "Genie"}}],
        "sql": [{"sql_type": "legacy_query", "id": "q1", "name": "q", "acl": one,
                 "_raw": {"id": "q1", "display_name": "q"}}],
        "secret_scope": [{"name": "s", "acls": [{"principal": "eng", "permission": "MANAGE"}],
                          "_raw": {"name": "s"}}],
    }
    data = adapt(obt)
    # ACL rows: cluster 2 + job 1 + dashboard 1 + genie 1 + legacy_query 1 + secret 1 = 7.
    assert len(data["object_permissions"]) == 7
    otypes = {r["object_type"] for r in data["object_permissions"]}
    assert {"cluster", "job", "lakeview_dashboard", "genie_space", "legacy_query",
            "secret_scope"} <= otypes
    # Managed-by + new metadata surfaced as columns.
    assert data["groups"][0]["_managed"] == "Databricks-managed"
    assert data["service_principals"][0]["_managed"] == "Entra / UMI"
    assert data["service_principals"][0]["_has_secrets"] is True
    assert data["jobs"][0]["_deployed_by_dab"] is True
    assert data["clusters"][0]["_pinned"] is True and data["clusters"][0]["_acls"] == 2
    counts = build_counts(data)
    assert counts["object_permissions"] == 7


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"FAIL  {fn.__name__}: {exc}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
