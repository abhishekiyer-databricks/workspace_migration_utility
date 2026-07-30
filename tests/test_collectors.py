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
    coll = IdentityCollector(FakeClient(scim_table=scim), _cfg())
    objs = _run_ok(coll)
    assert len(objs) == 7
    eng = next(o for o in objs if o.get("displayName") == "eng")
    assert eng["has_nested_groups"] and eng["member_count"] == 2
    classify_all(objs)
    byname = {(o.get("userName") or o.get("applicationId") or o.get("displayName")): o["classification"] for o in objs}
    assert byname["alice@corp.com"] == IdentityClass.ENTRA_USER.value
    assert byname["app-entra"] == IdentityClass.UMI_OR_ENTRA_SP.value
    assert byname["app-dbx"] == IdentityClass.DB_MANAGED_SP.value
    assert byname["acct"] == IdentityClass.ACCOUNT_GROUP.value
    assert byname["eng"] == IdentityClass.DB_MANAGED_GROUP.value
    assert byname["admins"] == IdentityClass.BUILTIN_GROUP.value
    assert byname["users"] == IdentityClass.BUILTIN_GROUP.value


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
    assert [c["ephemeral"] for c in clusters].count(True) == 1
    assert any(c["pinned"] for c in clusters)


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
    pag = {"api/2.1/jobs/list": [{"job_id": "j1",
            "settings": {"name": "nightly", "format": "MULTI_TASK", "tasks": [{"task_key": "t"}]}}]}
    gt = {"api/2.0/permissions/jobs/j1": {"access_control_list": [
        {"all_permissions": [{"permission_level": "IS_OWNER"}]}]}}
    objs = _run_ok(JobsCollector(FakeClient(get_table=gt, paginated_table=pag), _cfg()))
    assert objs[0]["format"] == "MULTI_TASK" and objs[0]["settings"]["tasks"] and objs[0]["has_owner_acl"]


def test_sql_legacy_names():
    from src.collectors.sql_collector import SqlCollector
    gt = {"api/2.0/sql/warehouses": {"warehouses": [{"id": "w1", "name": "wh", "warehouse_type": "PRO"}]},
          "api/2.0/permissions/sql/warehouses/w1": {"access_control_list": []}}
    pag = {"api/2.0/sql/queries": [{"id": "q1", "name": "q"}],
           "api/2.0/sql/alerts": [{"id": "a1", "name": "a"}],
           "api/2.0/sql/dashboards": [{"id": "l1", "name": "ld"}]}
    objs = _run_ok(SqlCollector(FakeClient(get_table=gt, paginated_table=pag), _cfg()))
    assert {o["sql_type"] for o in objs} == {"warehouse", "legacy_query", "legacy_alert", "legacy_dashboard"}


def test_dlt_dashboards_genie_serving():
    from src.collectors.dlt_collector import DltCollector
    from src.collectors.dashboards_collector import DashboardsCollector
    from src.collectors.genie_collector import GenieCollector
    from src.collectors.serving_collector import ServingCollector
    gt = {"api/2.0/pipelines/dp1": {"spec": {"name": "bronze"}},
          "api/2.0/lakeview/dashboards/d1": {"display_name": "KPIs", "warehouse_id": "w1", "serialized_dashboard": "{}"},
          "api/2.0/serving-endpoints": {"endpoints": [{"name": "my-ep", "id": "e1", "config": {}}, {"name": "databricks-x"}]},
          "api/2.0/permissions/pipelines/dp1": {"access_control_list": []},
          "api/2.0/permissions/serving-endpoints/e1": {"access_control_list": []}}
    pag = {"api/2.0/pipelines": [{"pipeline_id": "dp1", "name": "bronze"}],
           "api/2.0/lakeview/dashboards": [{"dashboard_id": "d1", "display_name": "KPIs"}],
           "api/2.0/genie/spaces": [{"space_id": "s1", "title": "Genie", "warehouse_id": "w1"}]}
    c = FakeClient(get_table=gt, paginated_table=pag)
    assert _run_ok(DltCollector(c, _cfg()))[0]["spec"]["name"] == "bronze"
    assert _run_ok(DashboardsCollector(c, _cfg()))[0]["serialized_dashboard"] == "{}"
    g = _run_ok(GenieCollector(c, _cfg()))[0]
    assert g["migratable"] is False and g["warehouse_id"] == "w1"
    serv = _run_ok(ServingCollector(c, _cfg()))
    assert [o["name"] for o in serv] == ["my-ep"]


def test_misc():
    from src.collectors.misc_collector import MiscCollector
    gt = {"api/2.0/global-init-scripts": {"scripts": [{"script_id": "g1", "name": "gis", "position": 0, "enabled": True}]},
          "api/2.0/global-init-scripts/g1": {"script": "ZWNobw=="},
          "api/2.0/libraries/all-cluster-statuses": {"statuses": [{"cluster_id": "c1", "library_statuses": [{"library": {"pypi": {"package": "requests"}}, "status": "INSTALLED"}]}]},
          "api/2.0/ip-access-lists": {"ip_access_lists": [{"list_id": "i1", "label": "corp", "list_type": "ALLOW", "ip_addresses": ["1.2.3.4"], "enabled": True}]},
          "api/2.0/workspace-conf": lambda p: {p["keys"]: "true"}}
    objs = _run_ok(MiscCollector(FakeClient(get_table=gt), _cfg()))
    assert {o["misc_type"] for o in objs} == {"global_init_script", "cluster_library", "ip_access_list", "workspace_conf"}
    assert any(o["misc_type"] == "workspace_conf" and o["key"] == "enableTokensConfig" for o in objs)


def test_workspace_special_paths():
    from src.collectors.workspace_collector import WorkspaceCollector

    def ws_list(params):
        p = params.get("path")
        if p == "/":
            return {"objects": [
                {"path": "/Shared", "object_type": "DIRECTORY", "object_id": "1"},
                {"path": "/Repos", "object_type": "DIRECTORY", "object_id": "2"},
                {"path": "/Users/a@x.com", "object_type": "DIRECTORY", "object_id": "3"}]}
        if p == "/Shared":
            return {"objects": [{"path": "/Shared/nb", "object_type": "NOTEBOOK", "language": "PYTHON", "object_id": "10"}]}
        return {"objects": []}

    gt = {"api/2.0/workspace/list": ws_list,
          "api/2.0/permissions/directories/1": {"access_control_list": []},
          "api/2.0/permissions/directories/3": {"access_control_list": []},
          "api/2.0/permissions/notebooks/10": {"access_control_list": []},
          "api/2.0/permissions/repos/r1": {"access_control_list": []}}
    pag = {"api/2.0/repos": [{"id": "r1", "path": "/Repos/t/repo", "url": "https://g/x", "branch": "main"}]}
    objs = _run_ok(WorkspaceCollector(FakeClient(get_table=gt, paginated_table=pag), _cfg()))
    paths = {o["path"] for o in objs}
    assert "/Repos" not in paths and "/Shared" in paths and "/Shared/nb" in paths
    assert "/Repos/t/repo" in paths
    assert next(o for o in objs if o["path"] == "/Users/a@x.com")["is_user_root"] is True


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
    """The report adapter must (a) flatten every ACL grant into a countable row and
    (b) surface Entra-vs-Databricks-managed + ephemeral/owner metadata as columns."""
    from src.reports.inventory_view import adapt, build_counts

    acl = [{"user_name": "a@x.com",
            "all_permissions": [{"permission_level": "CAN_MANAGE", "inherited": False}]},
           {"group_name": "eng",
            "all_permissions": [{"permission_level": "CAN_VIEW", "inherited": False}]}]
    obt = {
        "identity": [
            {"identity_type": "group", "id": "3", "displayName": "eng", "classification":
             "db_managed_group", "member_count": 5, "has_nested_groups": True,
             "entitlements": ["databricks-sql-access"], "roles": [], "_raw": {"id": "3"}},
            {"identity_type": "service_principal", "id": "2", "applicationId": "app-2",
             "classification": "umi_or_entra_sp", "entitlements": [], "_raw": {"id": "2"}},
        ],
        "compute": [{"compute_type": "cluster", "cluster_id": "c1", "cluster_name": "cl",
                     "ephemeral": False, "pinned": True, "acl": acl, "_raw": {"cluster_id": "c1"}}],
        "secret_scope": [{"name": "s", "acls": [{"principal": "eng", "permission": "MANAGE"}],
                          "_raw": {"name": "s"}}],
    }
    data = adapt(obt)
    # ACL rows: 2 (cluster) + 1 (secret scope) = 3 countable grants.
    assert len(data["object_permissions"]) == 3
    assert {r["principal"] for r in data["object_permissions"]} == {"a@x.com", "eng"}
    # Managed-by surfaced.
    assert data["groups"][0]["_managed"] == "Databricks-managed"
    assert data["service_principals"][0]["_managed"] == "Entra / UMI"
    # Cluster metadata surfaced.
    assert data["clusters"][0]["_pinned"] is True and data["clusters"][0]["_acls"] == 2
    counts = build_counts(data)
    assert counts["object_permissions"] == 3


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
