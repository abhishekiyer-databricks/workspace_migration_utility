"""Offline unit tests for the Export engine (Plan 2).

Run: python3 -m tests.test_export   (from the repo root; no Databricks/network needed).
"""
from __future__ import annotations

from src.config.config_manager import Config
from tests.fakes import FakeClient


def _cfg(**over):
    d = {"role": "source", "source_workspace_id": "1", "run_id": "r",
         "source_staging_location": "/Volumes/a/b/c"}
    d.update(over)
    return Config.from_dict(d)


# ─────────────────────────── transforms ────────────────────────────────────

def test_strip_and_fingerprint_stable():
    from src.transform.transforms import strip_runtime, fingerprint, normalize
    cluster = {"cluster_name": "cl", "cluster_id": "c-123", "state": "RUNNING",
               "start_time": 111, "last_activity_time": 222, "spark_version": "13.x",
               "node_type_id": "Standard_DS3_v2", "autoscale": {"min_workers": 1, "max_workers": 4}}
    stripped = strip_runtime("cluster", cluster)
    # runtime fields gone; create-config kept.
    assert "cluster_id" not in stripped and "state" not in stripped
    assert "start_time" not in stripped and "last_activity_time" not in stripped
    assert stripped["cluster_name"] == "cl" and stripped["spark_version"] == "13.x"
    assert stripped["autoscale"] == {"min_workers": 1, "max_workers": 4}
    # fingerprint is deterministic + insensitive to the stripped runtime churn.
    churned = dict(cluster, state="TERMINATED", start_time=999, cluster_id="c-999")
    assert fingerprint(strip_runtime("cluster", cluster)) == fingerprint(strip_runtime("cluster", churned))
    # a real content change flips it.
    changed = dict(cluster, node_type_id="Standard_DS4_v2")
    assert fingerprint(strip_runtime("cluster", cluster)) != fingerprint(strip_runtime("cluster", changed))
    assert fingerprint({}).startswith("sha256:")
    assert normalize({"b": 1, "a": 2}) == '{"a":2,"b":1}'


def test_strip_glob_patterns():
    from src.transform.transforms import strip_runtime
    c = {"cluster_name": "x", "terminated_time": 1, "start_time": 2, "creator_by_user": "u",
         "last_activity_by_user_name": "u2", "keep_me": True}
    s = strip_runtime("cluster", c)
    assert "terminated_time" not in s and "start_time" not in s
    assert "creator_by_user" not in s and "last_activity_by_user_name" not in s
    assert s["keep_me"] is True and s["cluster_name"] == "x"


def test_fingerprint_stable_under_list_reordering():
    """A re-export of an UNCHANGED asset must yield the SAME fingerprint even when the source
    API returns list members in a different order.

    Regression: SCIM returns group `members` in a random order on each GET (verified live on
    fvm1 — 3 GETs, same 9 members, 3 orders). Hashing raw order re-fingerprinted an unchanged
    group every run, so the target's cross-run UPSERT would have "updated" it forever.
    """
    from src.transform.transforms import fingerprint
    g1 = {"displayName": "users",
          "members": [{"value": "1", "display": "a"}, {"value": "2", "display": "b"},
                      {"value": "3", "display": "c"}],
          "entitlements": [{"value": "workspace-access"}, {"value": "allow-cluster-create"}]}
    g2 = {"displayName": "users",
          "members": [{"value": "3", "display": "c"}, {"value": "1", "display": "a"},
                      {"value": "2", "display": "b"}],
          "entitlements": [{"value": "allow-cluster-create"}, {"value": "workspace-access"}]}
    assert fingerprint(g1) == fingerprint(g2)
    # a REAL membership change must still change the fingerprint
    g3 = {**g1, "members": g1["members"][:2]}
    assert fingerprint(g3) != fingerprint(g1)
    # nested reordering (task lists inside a job payload) is handled too
    j1 = {"name": "j", "tasks": [{"task_key": "a"}, {"task_key": "b"}]}
    j2 = {"name": "j", "tasks": [{"task_key": "b"}, {"task_key": "a"}]}
    assert fingerprint(j1) == fingerprint(j2)


def test_dbfs_library_refs_flagged_manual():
    """A library whose artifact lives on DBFS exports as a DANGLING reference (DBFS content is
    out of scope), so it must be `manual` — never a `success` that silently breaks the target."""
    from src.exporters.asset_export import build_all
    misc = [
        {"misc_type": "cluster_library", "cluster_id": "c1", "_natural_key": "c1:jar",
         "library": {"jar": "dbfs:/FileStore/x.jar"}},
        {"misc_type": "cluster_library", "cluster_id": "c1", "_natural_key": "c1:whl",
         "library": {"whl": "/dbfs/FileStore/y.whl"}},
        {"misc_type": "cluster_library", "cluster_id": "c1", "_natural_key": "c1:pypi",
         "library": {"pypi": {"package": "tabulate==0.9.0"}}},
        {"misc_type": "cluster_library", "cluster_id": "c1", "_natural_key": "c1:maven",
         "library": {"maven": {"coordinates": "g:a:1"}}},
        # a UC Volumes / workspace-files artifact DOES migrate → must stay auto
        {"misc_type": "cluster_library", "cluster_id": "c1", "_natural_key": "c1:vol",
         "library": {"jar": "/Volumes/cat/sch/vol/z.jar"}},
    ]
    by = {m["natural_key"]: m for m in build_all({"misc": misc})["cluster_library"]}
    assert by["c1:jar"]["migration_mode"] == "manual" and "DBFS" in by["c1:jar"]["note"]
    assert by["c1:whl"]["migration_mode"] == "manual"
    assert by["c1:pypi"]["migration_mode"] == "auto"
    assert by["c1:maven"]["migration_mode"] == "auto"
    assert by["c1:vol"]["migration_mode"] == "auto"
    # a job task pulling a dbfs library still migrates, but must SAY so
    jobs = [{"name": "j", "job_id": "1", "deployed_by_dab": False,
             "settings": {"name": "j", "tasks": [
                 {"task_key": "t", "libraries": [{"jar": "dbfs:/FileStore/q.jar"}]}]}}]
    ju = build_all({"job": jobs})["job"][0]
    assert ju["migration_mode"] == "auto" and "DBFS" in ju["note"]


def test_dab_owned_pathless_assets_are_not_recreated():
    """DAB can own assets with no workspace path (cluster/pool/warehouse/scope/serving/genie).
    Each must export as `dab` with NO payload, so import can't duplicate a bundle-owned asset."""
    from src.exporters.asset_export import build_all
    units = build_all({
        "compute": [
            {"compute_type": "cluster", "_natural_key": "c", "cluster_id": "1",
             "deployed_by_dab": True, "_raw": {"cluster_name": "c"}},
            {"compute_type": "instance_pool", "_natural_key": "p", "instance_pool_id": "2",
             "deployed_by_dab": True, "_raw": {"instance_pool_name": "p"}},
        ],
        "sql": [{"sql_type": "warehouse", "_natural_key": "w", "id": "3",
                 "deployed_by_dab": True, "_raw": {"name": "w"}}],
        "secret_scope": [{"name": "s", "deployed_by_dab": True, "key_names": ["k"],
                          "backend_type": "DATABRICKS"}],
        "serving_endpoint": [{"name": "e", "deployed_by_dab": True, "migratable": True,
                              "config": {"served_entities": []}}],
        "genie_space": [{"title": "g", "space_id": "4", "deployed_by_dab": True,
                         "serialized_space": "{}", "has_serialized_space": True}],
    })
    for at in ("cluster", "instance_pool", "sql_warehouse", "secret_scope",
               "serving_endpoint", "genie_space"):
        u = units[at][0]
        assert u["export_status"] == "dab", f"{at} should be dab, got {u['export_status']}"
        assert u["payload"] == {}, f"{at} must carry no create payload"
    # the scope's VALUES are still a manual action even though DAB redeploys the scope
    assert units["secret_value"][0]["migration_mode"] == "manual"


def test_identity_import_action_create_vs_assign():
    """export_status=success only means "exported". `import_action` must carry create-vs-assign."""
    from src.exporters.asset_export import build_all
    ids = [
        {"identity_type": "user", "userName": "a@x.com", "id": "1",
         "classification": "entra_user", "_raw": {"userName": "a@x.com"}},
        {"identity_type": "user", "userName": "b@x.com", "id": "2",
         "classification": "needs_review", "_raw": {"userName": "b@x.com"}},
        {"identity_type": "service_principal", "applicationId": "app1", "id": "3",
         "classification": "db_managed_sp", "_raw": {"applicationId": "app1"}},
        {"identity_type": "service_principal", "applicationId": "app2", "id": "4",
         "classification": "umi_or_entra_sp", "_raw": {"applicationId": "app2"}},
    ]
    u = build_all({"identity": ids})
    users = {x["natural_key"]: x for x in u["user"]}
    sps = {x["natural_key"]: x for x in u["service_principal"]}
    # everything was exported fine...
    assert all(x["export_status"] == "success" for x in list(users.values()) + list(sps.values()))
    # ...but the target action differs
    assert users["a@x.com"]["import_action"] == "assign_on_target"
    assert users["b@x.com"]["import_action"] == "review_required"
    assert sps["app1"]["import_action"] == "create"
    assert sps["app2"]["import_action"] == "assign_on_target"


# ─────────────────────────── asset_export ──────────────────────────────────

def _sample_inventory():
    return {
        "identity": [
            {"identity_type": "user", "id": "u1", "userName": "alice@corp.com",
             "classification": "entra_user", "_raw": {"id": "u1", "userName": "alice@corp.com"}},
            {"identity_type": "service_principal", "id": "s1", "applicationId": "app-dbx",
             "classification": "db_managed_sp", "has_secrets": True, "_raw": {"id": "s1"}},
            {"identity_type": "group", "id": "g1", "displayName": "eng",
             "classification": "db_managed_group", "_raw": {"id": "g1", "displayName": "eng"}},
            {"identity_type": "group", "id": "g2", "displayName": "admins",
             "classification": "builtin_group", "_raw": {"id": "g2", "displayName": "admins"}},
        ],
        "compute": [
            {"compute_type": "cluster", "cluster_id": "c1", "_natural_key": "cl",
             "acl": [], "_raw": {"cluster_name": "cl", "cluster_id": "c1", "state": "RUNNING",
                                 "node_type_id": "n1"}},
            {"compute_type": "cluster_policy", "policy_id": "p1", "_natural_key": "pol",
             "_raw": {"name": "pol", "policy_id": "p1", "definition": "{}"}},
        ],
        "workspace_object": [
            {"object_type": "DIRECTORY", "path": "/Shared", "object_id": "1"},
            {"object_type": "NOTEBOOK", "path": "/Users/alice@corp.com/nb", "object_id": "10",
             "language": "PYTHON"},
            {"object_type": "FILE", "path": "/Users/bob@corp.com/data.csv", "object_id": "12"},
            {"object_type": "REPO", "path": "/Repos/x/r", "repo_id": "20",
             "_raw": {"path": "/Repos/x/r", "url": "https://g/r", "provider": "gitHub",
                      "branch": "main", "head_commit_id": "abc"}},
        ],
        "secret_scope": [
            {"name": "kv", "backend_type": "AZURE_KEYVAULT",
             "keyvault_metadata": {"dns_name": "d"}, "key_names": ["k1", "k2"],
             "acls": [{"principal": "eng", "permission": "READ"}], "_raw": {"name": "kv"}},
        ],
        "job": [
            {"job_id": "j1", "name": "nightly", "deployed_by_dab": False,
             "settings": {"name": "nightly", "tasks": [{"task_key": "t"}]}, "acl": [], "_raw": {}},
            {"job_id": "j2", "name": "bundle-job", "deployed_by_dab": True,
             "settings": {"name": "bundle-job", "deployment": {"kind": "BUNDLE"}}, "_raw": {}},
        ],
        "sql": [
            {"sql_type": "warehouse", "id": "w1", "_natural_key": "wh",
             "_raw": {"name": "wh", "id": "w1", "state": "RUNNING", "warehouse_type": "PRO"}},
            {"sql_type": "legacy_query", "id": "q1", "_natural_key": "q",
             "_raw": {"display_name": "q", "id": "q1", "query": "select 1"}},
        ],
        "dlt_pipeline": [
            {"pipeline_id": "dp1", "name": "bronze", "deployed_by_dab": False,
             "spec": {"name": "bronze", "target": "db"}, "acl": []},
        ],
        "lakeview_dashboard": [
            {"dashboard_id": "d1", "display_name": "KPIs", "deployed_by_dab": False,
             "warehouse_id": "w1", "serialized_dashboard": "{}", "acl": []},
        ],
        "genie_space": [
            {"space_id": "gs1", "title": "Genie", "warehouse_id": "w1", "acl": [],
             "serialized_space": '{"version":2}'},
            {"space_id": "gs2", "title": "NoSer", "warehouse_id": "w1", "acl": []},  # no payload
        ],
        "serving_endpoint": [
            {"name": "ext-ep", "migratable": True, "migration_note": "external",
             "config": {"served_entities": [{"external_model": {"name": "gpt"}}]}, "acl": []},
            {"name": "uc-ep", "migratable": False, "migration_note": "UC model",
             "config": {"served_entities": [{"entity_name": "cat.sch.model"}]}, "acl": []},
        ],
        "misc": [
            {"misc_type": "global_init_script", "script_id": "gi1", "_natural_key": "gis",
             "name": "gis", "position": 1, "enabled": True, "script_b64": "ZWNobw=="},
            {"misc_type": "workspace_conf", "key": "enableTokensConfig", "value": "true"},
        ],
        "app": [{"name": "my-app"}],
        "lakebase_project": [{"name": "lb1"}],
    }


def test_build_all_asset_types_and_modes():
    from src.exporters.asset_export import build_all
    ubt = build_all(_sample_inventory())
    # every fine-grained asset_type present.
    assert set(ubt) >= {"user", "service_principal", "group", "cluster", "cluster_policy",
                        "directory", "notebook", "workspace_file", "repo", "secret_scope",
                        "secret_value", "job", "sql_warehouse", "legacy_query", "dlt_pipeline",
                        "lakeview_dashboard", "genie_space", "serving_endpoint",
                        "global_init_script", "workspace_conf", "app", "lakebase_project"}
    # SP with secrets → note; db-managed group → auto.
    sp = ubt["service_principal"][0]
    assert sp["migration_mode"] == "auto" and "secret" in sp["note"].lower()
    groups = {g["natural_key"]: g for g in ubt["group"]}
    assert groups["eng"]["migration_mode"] == "auto"
    # A BUILT-IN group is never recreated (it already exists on target) → `covered`, but its
    # MEMBERSHIP is exported as its own unit so source admins actually become target admins.
    assert groups["admins"]["migration_mode"] == "covered"
    memberships = {m["natural_key"]: m for m in ubt["group_membership"]}
    assert memberships["admins"]["migration_mode"] == "auto"
    assert "members" in memberships["admins"]["payload"]
    # ...and only for built-ins: a db-managed group carries its members in the group unit itself.
    assert "eng" not in memberships
    # notebook/file are content mode with owner captured.
    nb = ubt["notebook"][0]
    assert nb["migration_mode"] == "content" and nb["content_ref"] is None
    assert nb["owner"] == "alice@corp.com"
    assert ubt["workspace_file"][0]["owner"] == "bob@corp.com"
    # DAB job → dab, no payload; normal job → auto with settings payload.
    jobs = {j["natural_key"]: j for j in ubt["job"]}
    assert jobs["bundle-job"]["migration_mode"] == "dab" and jobs["bundle-job"]["payload"] == {}
    assert jobs["nightly"]["migration_mode"] == "auto" and jobs["nightly"]["payload"]["tasks"]
    # secret_value units: one per key, manual.
    sv = ubt["secret_value"]
    assert {u["natural_key"] for u in sv} == {"kv/k1", "kv/k2"}
    assert all(u["migration_mode"] == "manual" and u["migratable"] is False for u in sv)
    # serving: external migratable auto, UC-backed manual.
    serv = {s["natural_key"]: s for s in ubt["serving_endpoint"]}
    assert serv["ext-ep"]["migration_mode"] == "auto" and serv["ext-ep"]["migratable"] is True
    assert serv["uc-ep"]["migration_mode"] == "manual" and serv["uc-ep"]["migratable"] is False
    # genie: auto when serialized_space present, manual when absent.
    genie = {g["natural_key"]: g for g in ubt["genie_space"]}
    assert genie["Genie"]["migration_mode"] == "auto"
    assert genie["Genie"]["payload"]["serialized_space"] == '{"version":2}'
    assert genie["NoSer"]["migration_mode"] == "manual" and genie["NoSer"]["migratable"] is False
    # apps/lakebase manual.
    assert ubt["app"][0]["migration_mode"] == "manual"
    # warehouse payload stripped of runtime state.
    assert "state" not in ubt["sql_warehouse"][0]["payload"]


# ─────────────────────────── acl_writer ────────────────────────────────────

def test_workspace_other_object_types_dedup_and_not_dropped():
    """The walk returns DASHBOARD/ALERT/MLFLOW_EXPERIMENT objects. Dashboards/alerts that match a
    NATIVE unit (by ASSET ID) are `covered` (counted once, not re-exported); unmatched ones stay
    manual; MLflow is out of scope. Nothing is silently dropped (1:1 reconciliation).

    Matching is by id, not path, and this fixture reflects the live API shapes that forced it:
      * the ALERT twin's native record has `parent_path: None` (fvm1: null even on a detail GET),
        so a path-keyed match left it `manual` — a real dashboard/alert reported as needing manual
        work when it was in fact fully exported;
      * `/a/orphan.lvdash.json` sits in the SAME DIRECTORY as the matched dashboard. The old code
        also keyed on the native record's `parent_path` (a directory), so this unrelated dashboard
        was wrongly marked `covered` — the dangerous direction, since `covered` asserts "already
        exported" and would hide a genuine export gap.
    """
    from src.exporters.asset_export import build_all
    obt = {
        "workspace_object": [
            {"object_type": "NOTEBOOK", "path": "/a/nb", "object_id": "1", "language": "PYTHON"},
            # a DASHBOARD's resource_id IS the Lakeview dashboard_id
            {"object_type": "DASHBOARD", "path": "/a/kpis.lvdash.json", "object_id": "2",
             "resource_id": "d1"},                                                        # twin
            {"object_type": "DASHBOARD", "path": "/a/orphan.lvdash.json", "object_id": "5",
             "resource_id": "d-unknown"},                                     # no native match
            # an ALERT's object_id is the Alerts V2 id (its resource_id is an unrelated uuid)
            {"object_type": "ALERT", "path": "/a/al.dbalert.json", "object_id": "a1",
             "resource_id": "3247336e-uuid-not-the-alert-id"},                            # twin
            {"object_type": "MLFLOW_EXPERIMENT", "path": "/a/exp", "object_id": "4"},
        ],
        "lakeview_dashboard": [
            {"dashboard_id": "d1", "display_name": "KPIs", "path": "/a/kpis.lvdash.json",
             "parent_path": "/a", "warehouse_id": "w", "serialized_dashboard": "{}",
             "deployed_by_dab": False}],
        # live shape: Alerts V2 exposes NO workspace path at all
        "sql": [{"sql_type": "alert", "id": "a1", "_natural_key": "al", "parent_path": None,
                 "_raw": {"parent_path": None}, "deployed_by_dab": False}],
    }
    ubt = build_all(obt)
    covered = {u["natural_key"]: u for u in ubt["lakeview_dashboard_file"]}
    assert covered["/a/kpis.lvdash.json"]["export_status"] == "covered"
    # same folder as the matched dashboard, but a different asset → must NOT be claimed covered
    assert covered["/a/orphan.lvdash.json"]["export_status"] == "manual"
    # pathless alert still dedupes, via object_id
    assert ubt["alert_v2_file"][0]["export_status"] == "covered"
    # MLflow out of scope, never fetched.
    assert ubt["mlflow_experiment"][0]["migratable"] is False
    assert ubt["mlflow_experiment"][0]["content_ref"] is None
    # nothing dropped: 1 nb + 2 dashboard-file + 1 alert-file + 1 mlflow + native(1 dash +1 alert)
    total = sum(len(v) for v in ubt.values())
    assert total == 7


def test_collect_acls_keys_and_counts():
    from src.exporters.acl_writer import collect_acls, acl_counts
    obt = {
        "compute": [{"compute_type": "cluster", "cluster_id": "c1", "_natural_key": "cl",
                     "acl": [{"user_name": "a@x.com",
                              "all_permissions": [{"permission_level": "CAN_MANAGE"}]},
                             {"group_name": "eng",
                              "all_permissions": [{"permission_level": "CAN_ATTACH_TO",
                                                   "inherited": True}]}]}],
        "secret_scope": [{"name": "kv", "acls": [{"principal": "eng", "permission": "READ"}]}],
        "job": [{"name": "j", "job_id": "j1", "acl": []}],   # empty → not emitted
    }
    acls = collect_acls(obt)
    keys = {(e["asset_type"], e["natural_key"]) for e in acls}
    assert ("cluster", "cl") in keys and ("secret_scope", "kv") in keys
    assert ("job", "j") not in keys   # empty ACL not emitted
    cl = next(e for e in acls if e["asset_type"] == "cluster")
    assert cl["perm_object_type"] == "clusters"
    principals = {(g["principal"], g["principal_type"]) for g in cl["grants"]}
    assert ("a@x.com", "user") in principals and ("eng", "group") in principals
    counts = acl_counts(acls)
    assert counts[("cluster", "cl")] == 2 and counts[("secret_scope", "kv")] == 1


# ─────────────────────────── parallel ──────────────────────────────────────

def test_parallel_map_failsoft_and_complete():
    from src.exporters.parallel import parallel_map, Locked
    def fn(x):
        if x == 3:
            raise ValueError("boom")
        return x * 2
    results = {item: (res, err) for item, res, err in parallel_map(range(6), fn, max_workers=4)}
    assert results[2] == (4, None)
    assert results[3][0] is None and isinstance(results[3][1], ValueError)
    assert len(results) == 6
    # Locked guards shared state.
    box = Locked({"n": 0})
    for item, _r, _e in parallel_map(range(10), lambda i: i, max_workers=1):
        with box as s:
            s["n"] += 1
    assert box.value["n"] == 10


# ─────────────────────────── content_fetcher ───────────────────────────────

def test_content_fetcher_success(tmp_path=None):
    import tempfile, os
    from src.exporters.artifact_writer import ArtifactWriter
    from src.exporters.content_fetcher import ContentFetcher

    tmp = tempfile.mkdtemp()
    cfg = _cfg(source_staging_location=tmp)
    aw = ArtifactWriter(cfg)
    aw.ensure_output_path()

    nb_py = b"# Databricks notebook source\nprint(1)\n"
    nb_sql = b"-- Databricks notebook source\nSELECT 1\n"
    csv = b"col1,col2\n1,2\n"
    dl = {"api/2.0/workspace/export": lambda p: (
        nb_py if p.get("path", "").endswith("py_nb") else
        nb_sql if p.get("path", "").endswith("sql_nb") else csv)}
    fetcher = ContentFetcher(FakeClient(download_table=dl), aw)

    # notebook (PYTHON) → SOURCE .py, direct_download route
    r1 = fetcher.fetch({"asset_type": "notebook", "natural_key": "/Users/a/py_nb",
                        "payload": {"language": "PYTHON"}})
    assert r1.status == "success" and r1.content_route == "direct_download"
    assert r1.content_ref.endswith(".py")
    assert open(os.path.join(aw.root, r1.content_ref), "rb").read() == nb_py

    # notebook (SQL) → .sql
    r2 = fetcher.fetch({"asset_type": "notebook", "natural_key": "/Users/a/sql_nb",
                        "payload": {"language": "SQL"}})
    assert r2.status == "success" and r2.content_ref.endswith(".sql")

    # workspace_file → verbatim .bin
    rf = fetcher.fetch({"asset_type": "workspace_file", "natural_key": "/Users/a/x.csv",
                        "payload": {}})
    assert rf.status == "success" and rf.content_ref.endswith(".bin")
    assert open(os.path.join(aw.root, rf.content_ref), "rb").read() == csv


def test_content_fetcher_big_notebook_skipped_no_bytes():
    """A >10MB notebook (MAX_NOTEBOOK_SIZE_EXCEEDED) → skipped_oversize, and NO bytes written
    (decision: it can't be recreated as a notebook via any API)."""
    import tempfile, os
    from src.exporters.artifact_writer import ArtifactWriter
    from src.exporters.content_fetcher import ContentFetcher
    from src.auth.token_manager import DownloadHTTPError

    tmp = tempfile.mkdtemp()
    cfg = _cfg(source_staging_location=tmp)
    aw = ArtifactWriter(cfg)
    aw.ensure_output_path()

    dl = {"api/2.0/workspace/export": DownloadHTTPError(400, "MAX_NOTEBOOK_SIZE_EXCEEDED")}
    r = ContentFetcher(FakeClient(download_table=dl), aw).fetch(
        {"asset_type": "notebook", "natural_key": "/Users/a/huge", "payload": {"language": "PYTHON"}})
    assert r.status == "skipped_oversize" and r.content_ref is None
    assert r.oversize.get("type") == "notebook" and "10 MB" in r.oversize["reason"]
    # nothing was written into content/
    content_dir = os.path.join(aw.root, "export/workspace/content")
    assert not os.path.isdir(content_dir) or not os.listdir(content_dir)


def test_content_fetcher_big_file_oversize_and_failure():
    import tempfile
    from src.exporters.artifact_writer import ArtifactWriter
    from src.exporters.content_fetcher import ContentFetcher, FILE_CAP
    from src.auth.token_manager import DownloadHTTPError, OversizeError

    tmp = tempfile.mkdtemp()
    cfg = _cfg(source_staging_location=tmp)
    aw = ArtifactWriter(cfg)
    aw.ensure_output_path()

    # file over 500MB → OversizeError from download_bytes cap → skipped_oversize
    dl = {"api/2.0/workspace/export": OversizeError(FILE_CAP + 1, "too big")}
    r = ContentFetcher(FakeClient(download_table=dl), aw).fetch(
        {"asset_type": "workspace_file", "natural_key": "/Users/a/giant.parquet", "payload": {}})
    assert r.status == "skipped_oversize" and r.oversize["type"] == "file"

    # a genuine non-size error → failure (files don't get the notebook-oversize benefit of doubt)
    dl3 = {"api/2.0/workspace/export": DownloadHTTPError(403, "forbidden")}
    r3 = ContentFetcher(FakeClient(download_table=dl3), aw).fetch(
        {"asset_type": "workspace_file", "natural_key": "/Users/a/forbidden", "payload": {}})
    assert r3.status == "failure" and "forbidden" in r3.note


def test_mangle_path_collision():
    from src.exporters.content_fetcher import mangle_path
    taken = set()
    a = mangle_path("/Users/a/NB", "notebook", "PYTHON", taken)
    b = mangle_path("/Users/a/nb", "notebook", "PYTHON", taken)   # case-insensitive collision
    assert a != b and a.lower() != b.lower()
    f = mangle_path("/Shared/data.csv", "file", "", taken)
    assert f.endswith(".bin") and f.startswith("Shared__data.csv")


# ─────────────────────────── bundle_state ──────────────────────────────────

def test_bundle_state_resolution():
    import tempfile, os, json
    from src.exporters import bundle_state as bs

    tmp = tempfile.mkdtemp()
    cfg = _cfg(source_staging_location=tmp, run_id="20260101_000000")
    root = bs.wsmig_root(cfg)
    os.makedirs(root)

    # no pointer, no bundle → export resolution fails loudly.
    try:
        bs.resolve_export_run_id(cfg, "", False)
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass

    # explicit widget always wins.
    assert bs.resolve_export_run_id(cfg, "R99", False) == ("R99", "widget")

    # pointer resolves.
    bs.write_latest_pointer(cfg, "20260101_120000", {"job": 3})
    assert bs.resolve_export_run_id(cfg, "", False) == ("20260101_120000", "pointer")

    # an incomplete bundle (checkpoint, no manifest) is resumed ahead of the pointer.
    inc = os.path.join(root, "20260102_000000")
    os.makedirs(inc)
    with open(os.path.join(inc, "checkpoint.json"), "w") as f:
        json.dump({}, f)
    assert bs.resolve_export_run_id(cfg, "", False) == ("20260102_000000", "resume")
    # force_full skips resume → falls back to pointer.
    assert bs.resolve_export_run_id(cfg, "", True)[1] == "pointer"

    # a complete bundle (manifest present) is NOT resumed.
    with open(os.path.join(inc, "manifest.json"), "w") as f:
        json.dump({}, f)
    assert bs.find_latest_incomplete_run(cfg) is None

    # inventory resolution: blank + incomplete → resume; else fresh.
    inc2 = os.path.join(root, "20260103_000000")
    os.makedirs(inc2)
    with open(os.path.join(inc2, "checkpoint.json"), "w") as f:
        json.dump({}, f)
    assert bs.resolve_inventory_run_id(cfg, "", False) == ("20260103_000000", "resume")
    assert bs.resolve_inventory_run_id(cfg, "", True) == (cfg.run_id, "fresh")
    assert bs.resolve_inventory_run_id(cfg, "X", False) == ("X", "widget")


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
