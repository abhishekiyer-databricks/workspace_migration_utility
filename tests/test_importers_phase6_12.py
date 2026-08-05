"""Offline tests for phases 6–12: sql, dlt, dashboards, genie, serving, misc, ACLs.

Focused on the decisions that are easy to get wrong and expensive when wrong:

  sql       — warehouses first; warehouse_id remapped everywhere; legacy dashboards NEVER attempted
  dlt       — the UC dependency named UP FRONT rather than as an opaque API error
  dashboards/genie — serialized payload carried VERBATIM, warehouse remapped, UC caveat stated
  serving   — external-model only; a UC-backed endpoint is `not_supported`, not a failure loop
  misc      — libraries DEFERRED rather than force-starting clusters (never spend DBUs silently);
              unknown workspace-conf keys refused rather than blanket-written
  acls      — what goes in the declarative PUT body and what deliberately doesn't; `skipped_no_object`
              is its own status with the specific case; parity proven by a post-apply diff
"""
from __future__ import annotations

import json
import tempfile

from src.config.config_manager import Config
from src.exporters.artifact_writer import ArtifactWriter
from src.importers.acl_importer import AclImporter
from src.importers.dashboards_importer import DashboardsImporter
from src.importers.dlt_importer import DltImporter
from src.importers.genie_importer import GenieImporter
from src.importers.misc_importer import MiscImporter
from src.importers.serving_importer import ServingImporter
from src.importers.sql_importer import SqlImporter
from src.state.state_store import StateStore
from tests.test_importers_phase2_5 import RecordingClient, _same_path
from tests.test_state_store import FakeBackend


def _make(cls, units, client, *, context=None, dry_run=False, acls=None, config_over=None):
    d = tempfile.mkdtemp()
    conf = {"role": "target", "source_workspace_id": "111", "run_id": "r1",
            "target_staging_location": d, "dry_run": dry_run,
            "imports": {"state_catalog": "c", "state_schema": "s"}}
    if config_over:
        conf["imports"].update(config_over.pop("imports", {}))
        conf.update(config_over)
    cfg = Config.from_dict(conf)
    aw = ArtifactWriter(cfg)
    aw.ensure_output_path()
    if acls is not None:
        aw.write_json("export/acls.json", acls)
    st = StateStore(FakeBackend(), cfg)
    st.ensure_table()
    st.load()
    by_type: dict = {}
    for u in units:
        by_type.setdefault(u["asset_type"], []).append(u)
    return cls(client, cfg, aw, state=st, units_by_type=by_type,
               context=context if context is not None else {}), st, aw


def _unit(asset_type, key, payload=None, **over):
    u = {"asset_type": asset_type, "natural_key": key, "source_id": f"src-{key}",
         "fingerprint": f"sha256:{key}", "import_action": "create", "export_status": "success",
         "payload": payload or {}, "note": ""}
    u.update(over)
    return u


# ═══════════════════════════════ SQL ════════════════════════════════════════

def test_warehouses_are_created_before_anything_that_references_them():
    client = RecordingClient()
    imp, _st, _aw = _make(SqlImporter, [
        _unit("legacy_query", "q1", {"display_name": "q1", "query_text": "select 1"}),
        _unit("sql_warehouse", "wh", {"name": "wh", "cluster_size": "Small"}),
    ], client)
    order = [u["asset_type"] for u in imp.load()]
    assert order.index("sql_warehouse") < order.index("legacy_query")


def test_query_warehouse_id_is_remapped_to_the_target_warehouse():
    client = RecordingClient()
    imp, _st, _aw = _make(SqlImporter, [
        _unit("sql_warehouse", "wh", {"name": "wh", "cluster_size": "Small"}, source_id="SRC-WH"),
        _unit("legacy_query", "q1", {"display_name": "q1", "query_text": "select 1",
                                     "warehouse_id": "SRC-WH"}),
    ], client)
    imp.run()
    body = client.bodies_to("sql/queries")[0]["query"]
    assert body["warehouse_id"] == "id-1", "the query still points at the SOURCE warehouse"
    assert "parent_path" not in body


def test_an_unmappable_warehouse_falls_back_to_an_existing_one_and_says_so():
    """A query attached to a working warehouse can be re-pointed; one with a dead id just errors."""
    client = RecordingClient(get_table={"api/2.0/sql/warehouses": {
        "warehouses": [{"id": "TGT-EXISTING", "name": "target-only-wh"}]}})
    imp, _st, _aw = _make(SqlImporter, [
        _unit("legacy_query", "q1", {"display_name": "q1", "warehouse_id": "GONE"})], client)
    res = imp.run()
    body = client.bodies_to("sql/queries")[0]["query"]
    assert body["warehouse_id"] == "TGT-EXISTING"
    assert any("runnable" in w for w in res.warnings)


def test_alert_query_id_is_remapped():
    client = RecordingClient()
    imp, _st, _aw = _make(SqlImporter, [
        _unit("legacy_query", "q1", {"display_name": "q1"}, source_id="SRC-Q"),
        _unit("legacy_alert", "a1", {"name": "a1", "query_id": "SRC-Q"}),
    ], client)
    imp.run()
    body = client.bodies_to("sql/alerts")[0]
    assert body["query_id"] == "id-1", "an alert holding a SOURCE query id is inert on target"


def test_alert_v2_is_posted_flat_not_wrapped():
    """Verified against the SDK's create_alert — /api/2.0/alerts takes the body FLAT."""
    client = RecordingClient()
    imp, _st, _aw = _make(SqlImporter, [
        _unit("alert_v2", "a2", {"display_name": "a2", "query_text": "select 1"})], client)
    imp.run()
    body = client.bodies_to("alerts")[0]
    assert "alert" not in body and body["display_name"] == "a2"


def test_legacy_dashboards_are_never_attempted():
    """The create endpoint is gone on modern workspaces; attempting it would be permanent red."""
    client = RecordingClient()
    imp, st, _aw = _make(SqlImporter, [
        _unit("legacy_dashboard", "old-dash", {}, import_action="manual",
              note="legacy SQL dashboard creation is not supported by the API")], client)
    res = imp.run()
    assert client.posts_to("preview/sql/dashboards") == []
    assert not [c for c in client.calls if "dashboards" in str(c[1]) and c[0] == "POST"]
    assert res.manual == 1
    assert st.row("legacy_dashboard", "old-dash")["last_action"] == "manual"


def test_warehouse_update_uses_the_id_edit_path():
    client = RecordingClient()
    imp, st, _aw = _make(SqlImporter, [
        _unit("sql_warehouse", "wh", {"name": "wh", "cluster_size": "Large"},
              fingerprint="sha256:v2")], client)
    st.record("sql_warehouse", "wh", action="created", fingerprint="sha256:v1",
              target_object_id="WH-1")
    imp.existing_keys = lambda: {"wh": "WH-1"}
    imp.run()
    assert client.posts_to("sql/warehouses/WH-1/edit"), "warehouses edit via /{id}/edit"


def test_creator_name_is_not_sent_on_a_warehouse():
    client = RecordingClient()
    imp, _st, _aw = _make(SqlImporter, [
        _unit("sql_warehouse", "wh", {"name": "wh", "creator_name": "someone@corp.com"})], client)
    imp.run()
    assert "creator_name" not in client.bodies_to("sql/warehouses")[0]


# ═══════════════════════════════ DLT ════════════════════════════════════════

def test_a_uc_pipeline_names_the_uc_dependency_up_front():
    """"the UC migration hasn't run" and "our payload is wrong" need different responses, and the
    raw API error does not distinguish them."""
    client = RecordingClient()
    imp, _st, _aw = _make(DltImporter, [
        _unit("dlt_pipeline", "p1", {"name": "p1", "catalog": "prod_cat", "schema": "bronze",
                                     "libraries": []})], client)
    res = imp.run()
    assert res.warned == 1
    note = res.units[0]["note"]
    assert "OUT OF SCOPE" in note and "prod_cat.bronze" in note


def test_pipeline_source_notebook_missing_on_target_is_flagged():
    client = RecordingClient()
    imp, _st, _aw = _make(DltImporter, [
        _unit("dlt_pipeline", "p1", {"name": "p1",
                                     "libraries": [{"notebook": {"path": "/Repos/x/nb"}}]})], client)
    res = imp.run()
    assert res.warned == 1
    assert "first update will FAIL" in res.units[0]["note"]


def test_pipeline_cluster_policy_is_remapped_and_server_fields_stripped():
    client = RecordingClient()
    imp, _st, _aw = _make(DltImporter, [
        _unit("cluster_policy", "pol", {}, source_id="SRC-POL"),
        _unit("dlt_pipeline", "p1", {"name": "p1", "id": "SOURCE-PIPELINE-ID",
                                     "state": "IDLE", "pipeline_type": "WORKSPACE",
                                     "clusters": [{"label": "default", "policy_id": "SRC-POL"}]}),
    ], client, context={"cluster_policy_target_ids": {"pol": "TGT-POL"}})
    imp.run()
    body = client.bodies_to("pipelines")[0]
    assert body["clusters"][0]["policy_id"] == "TGT-POL"
    for field in ("id", "state", "pipeline_type"):
        assert field not in body, f"{field} is not a create field for a pipeline spec"


# ═══════════════════ DASHBOARDS + GENIE ═════════════════════════════════════

def test_lakeview_serialized_dashboard_is_carried_verbatim_with_the_uc_caveat():
    client = RecordingClient()
    serialized = json.dumps({"pages": [{"name": "p1"}]})
    imp, _st, _aw = _make(DashboardsImporter, [
        _unit("sql_warehouse", "wh", {}, source_id="SRC-WH"),
        _unit("lakeview_dashboard", "sales", {"display_name": "sales",
                                              "serialized_dashboard": serialized,
                                              "warehouse_id": "SRC-WH"}),
    ], client, context={"sql_warehouse_target_ids": {"wh": "TGT-WH"}})
    res = imp.run()
    body = client.bodies_to("lakeview/dashboards")[0]
    assert body["serialized_dashboard"] == serialized, "the definition must not be rewritten"
    assert body["warehouse_id"] == "TGT-WH"
    assert "Unity Catalog" in res.units[0]["note"], \
        "the most common cause of a broken-but-successful import must be stated"


def test_genie_space_is_auto_migratable_with_serialized_space():
    """Supersedes the old "un-exportable protobuf" belief — verified live 2026-08-01."""
    client = RecordingClient()
    serialized = json.dumps({"tables": ["cat.sch.t"]})
    imp, _st, _aw = _make(GenieImporter, [
        _unit("sql_warehouse", "wh", {}, source_id="SRC-WH"),
        _unit("genie_space", "analytics", {"title": "analytics", "description": "d",
                                           "serialized_space": serialized,
                                           "warehouse_id": "SRC-WH"}),
    ], client, context={"sql_warehouse_target_ids": {"wh": "TGT-WH"}})
    res = imp.run()
    body = client.bodies_to("genie/spaces")[0]
    assert body["serialized_space"] == serialized
    assert body["warehouse_id"] == "TGT-WH"
    assert res.created == 1
    assert "Unity Catalog" in res.units[0]["note"]


# ═══════════════════════════════ SERVING ════════════════════════════════════

def test_platform_managed_endpoints_are_never_touched():
    client = RecordingClient()
    imp, _st, _aw = _make(ServingImporter, [
        _unit("serving_endpoint", "databricks-meta-llama-3", {}),
        _unit("serving_endpoint", "my-openai-proxy",
              {"served_entities": [{"name": "e", "external_model": {"provider": "openai"}}]}),
    ], client)
    res = imp.run()
    assert res.total == 1, "a databricks-* endpoint is platform-managed and not ours to migrate"
    assert client.bodies_to("serving-endpoints")[0]["name"] == "my-openai-proxy"


def test_an_external_model_endpoint_is_created_with_the_api_key_caveat():
    client = RecordingClient()
    imp, _st, _aw = _make(ServingImporter, [
        _unit("serving_endpoint", "gpt-proxy",
              {"served_entities": [{"name": "gpt", "external_model": {"provider": "openai"}}],
               "config_version": 7, "state": {"ready": "READY"}})], client)
    res = imp.run()
    body = client.bodies_to("serving-endpoints")[0]
    assert body["name"] == "gpt-proxy"
    for field in ("config_version", "state"):
        assert field not in body["config"], f"{field} is not a create field"
    assert "API key is NOT migratable" in res.units[0]["note"]


def test_a_uc_backed_endpoint_is_not_supported_rather_than_a_failure_loop():
    client = RecordingClient()
    imp, st, _aw = _make(ServingImporter, [
        _unit("serving_endpoint", "prod-model",
              {"served_entities": [{"name": "m", "entity_name": "cat.sch.my_model"}]})], client)
    res = imp.run()
    assert client.posts_to("serving-endpoints") == []
    assert res.failed == 1
    row = st.row("serving_endpoint", "prod-model")
    assert row["failure_category"] == "not_supported"
    assert "OUT OF SCOPE" in row["last_error"]


# ═══════════════════════════════ MISC ═══════════════════════════════════════

def test_a_cluster_library_is_deferred_rather_than_starting_a_cluster():
    """D6: force-starting clusters would silently spend the customer's money."""
    client = RecordingClient(get_table={"api/2.0/clusters/get": {"state": "TERMINATED"}})
    imp, st, _aw = _make(MiscImporter, [
        _unit("cluster_library", "SRC-CLU:requests",
              {"cluster_id": "SRC-CLU", "library": {"pypi": {"package": "requests"}}})], client,
        context={"cluster_target_ids": {"etl": "TGT-CLU"}})
    imp.units_by_type["cluster"] = [_unit("cluster", "etl", source_id="SRC-CLU")]
    res = imp.run()
    assert client.posts_to("libraries/install") == [], "a stopped cluster must not be started"
    assert res.failed == 1
    err = st.row("cluster_library", "SRC-CLU:requests")["last_error"]
    assert "library_force_start_clusters=true" in err
    assert "does not consume DBUs" in err


def test_a_library_installs_when_the_cluster_is_already_running():
    client = RecordingClient(get_table={"api/2.0/clusters/get": {"state": "RUNNING"}})
    imp, _st, _aw = _make(MiscImporter, [
        _unit("cluster_library", "SRC-CLU:requests",
              {"cluster_id": "SRC-CLU", "library": {"pypi": {"package": "requests"}}})], client,
        context={"cluster_target_ids": {"etl": "TGT-CLU"}})
    imp.units_by_type["cluster"] = [_unit("cluster", "etl", source_id="SRC-CLU")]
    res = imp.run()
    body = client.bodies_to("libraries/install")[0]
    assert body == {"cluster_id": "TGT-CLU", "libraries": [{"pypi": {"package": "requests"}}]}
    assert res.created == 1


def test_a_library_with_force_start_installs_on_a_stopped_cluster():
    client = RecordingClient(get_table={"api/2.0/clusters/get": {"state": "TERMINATED"}})
    imp, _st, _aw = _make(MiscImporter, [
        _unit("cluster_library", "SRC-CLU:requests",
              {"cluster_id": "SRC-CLU", "library": {"pypi": {"package": "requests"}}})], client,
        context={"cluster_target_ids": {"etl": "TGT-CLU"}},
        config_over={"imports": {"library_force_start_clusters": True}})
    imp.units_by_type["cluster"] = [_unit("cluster", "etl", source_id="SRC-CLU")]
    res = imp.run()
    assert client.posts_to("libraries/install"), "the opt-in must actually install"
    assert res.created == 1


def test_an_unknown_workspace_conf_key_is_refused_not_blanket_written():
    """A conf key can change the workspace's SECURITY posture — writing an unreviewed one silently
    is not something the tool should do."""
    client = RecordingClient()
    imp, st, _aw = _make(MiscImporter, [
        _unit("workspace_conf", "someExperimentalFlag",
              {"key": "someExperimentalFlag", "value": "true"})], client)
    res = imp.run()
    assert client.calls == [] or not [c for c in client.calls
                                      if c[0] == "PATCH" and "workspace-conf" in c[1]]
    assert res.failed == 1
    assert "documented key set" in st.row("workspace_conf", "someExperimentalFlag")["last_error"]


def test_a_known_conf_key_is_applied_one_key_per_call():
    """One key per call, so a single rejected key can't take the others down with it."""
    client = RecordingClient()
    imp, _st, _aw = _make(MiscImporter, [
        _unit("workspace_conf", "enableTokensConfig",
              {"key": "enableTokensConfig", "value": "false"}),
        _unit("workspace_conf", "enableWebTerminal",
              {"key": "enableWebTerminal", "value": "true"}),
    ], client)
    res = imp.run()
    patches = [c for c in client.calls if c[0] == "PATCH" and "workspace-conf" in c[1]]
    assert len(patches) == 2, "conf keys must be applied individually"
    assert patches[0][2] == {"enableTokensConfig": "false"}
    assert res.created == 2


def test_a_global_init_script_preserves_its_enabled_state_and_body():
    """An init script runs on EVERY cluster launch: silently enabling one changes the workspace."""
    client = RecordingClient()
    imp, _st, _aw = _make(MiscImporter, [
        _unit("global_init_script", "bootstrap",
              {"name": "bootstrap", "script_b64": "ZWNobyBoaQ==", "position": 2,
               "enabled": False})], client)
    res = imp.run()
    body = client.bodies_to("global-init-scripts")[0]
    assert body["script"] == "ZWNobyBoaQ==" and body["enabled"] is False and body["position"] == 2
    assert "disabled as on source" in res.units[0]["note"]


# ═══════════════════════════════ ACLs ═══════════════════════════════════════

def _acl(asset_type, natural_key, perm_type, grants, source_id="src-1"):
    return {"asset_type": asset_type, "natural_key": natural_key, "source_id": source_id,
            "perm_object_type": perm_type, "grants": grants}


def _grant(principal, level, ptype="group", inherited=False):
    return {"principal": principal, "principal_type": ptype, "permission_level": level,
            "inherited": inherited}


def test_explicit_grants_are_sent_verbatim_with_principals_remapped():
    client = RecordingClient()
    imp, _st, _aw = _make(AclImporter, [], client,
                          acls=[_acl("cluster", "etl", "clusters",
                                     [_grant("data-eng", "CAN_MANAGE"),
                                      _grant("a@b.com", "CAN_ATTACH_TO", "user"),
                                      _grant("old-app", "CAN_RESTART", "service_principal")])],
                          context={"cluster_target_ids": {"etl": "TGT-CLU"}})
    imp.identity_map = {"sp_mapping": {"old-app": "new-app"}, "user_map": {}}
    res = imp.run()
    body = client.calls
    put = [c for c in body if c[0] == "PUT"][0]
    acl = put[2]["access_control_list"]
    assert {"group_name": "data-eng", "permission_level": "CAN_MANAGE"} in acl
    assert {"user_name": "a@b.com", "permission_level": "CAN_ATTACH_TO"} in acl
    assert {"service_principal_name": "new-app", "permission_level": "CAN_RESTART"} in acl, \
        "a recreated SP's grant must go through the id map"
    assert res.created == 1


def test_inherited_echoes_and_the_admins_grant_are_not_sent():
    """Sending either FAILS or creates a divergence the source didn't have — omitting preserves
    parity rather than breaking it."""
    client = RecordingClient()
    imp, _st, _aw = _make(AclImporter, [], client,
                          acls=[_acl("cluster", "etl", "clusters", [
                              _grant("data-eng", "CAN_MANAGE"),
                              _grant("inherited-group", "CAN_VIEW", inherited=True),
                              _grant("admins", "CAN_MANAGE")])],
                          context={"cluster_target_ids": {"etl": "TGT-CLU"}})
    imp.run()
    acl = [c for c in client.calls if c[0] == "PUT"][0][2]["access_control_list"]
    principals = {list(e.keys())[0] and e.get("group_name") for e in acl}
    assert "data-eng" in principals
    assert "inherited-group" not in principals, "an inherited echo must not be sent"
    assert "admins" not in principals, "the built-in admins grant must not be sent"


def test_the_shared_root_acl_is_not_attempted():
    client = RecordingClient()
    imp, st, _aw = _make(AclImporter, [], client,
                         acls=[_acl("directory", "/Shared", "directories",
                                    [_grant("users", "CAN_MANAGE")])])
    res = imp.run()
    assert [c for c in client.calls if c[0] == "PUT"] == []
    assert res.skipped_no_object == 1
    row = st.row("acl", "directories:/Shared")
    assert row["last_action"] == "skipped_no_object"
    assert row["failure_category"] == "not_supported"


def test_a_bundle_object_grant_is_skipped_no_object_with_the_dab_case():
    """§6b-i: skipped_no_object is its OWN action — filing it as failed would make every
    bundle-using workspace show permanent red."""
    client = RecordingClient()
    imp, st, _aw = _make(AclImporter, [], client,
                         acls=[_acl("directory", "/Shared/.bundle/my_bundle", "directories",
                                    [_grant("data-eng", "CAN_MANAGE")])])
    res = imp.run()
    assert res.failed == 0, "a DAB object's grant must NOT be a failure"
    assert res.skipped_no_object == 1
    row = st.row("acl", "directories:/Shared/.bundle/my_bundle")
    assert row["last_action"] == "skipped_no_object"
    assert row["failure_category"] == "dab_redeploy"
    assert "bundle deploy" in row["last_error"]


def test_a_repo_grant_records_the_out_of_scope_case():
    client = RecordingClient()
    imp, st, _aw = _make(AclImporter, [], client,
                         acls=[_acl("repo", "/Repos/me/app", "repos",
                                    [_grant("data-eng", "CAN_MANAGE")])])
    imp.run()
    row = st.row("acl", "repos:/Repos/me/app")
    assert row["failure_category"] == "repo_out_of_scope"


def test_a_grant_on_a_unit_that_failed_earlier_records_the_dynamic_case():
    """One of the two DYNAMIC cases that make a path-based rule inadequate (D17)."""
    client = RecordingClient()
    imp, st, _aw = _make(AclImporter, [], client,
                         acls=[_acl("cluster", "broken-cluster", "clusters",
                                    [_grant("data-eng", "CAN_MANAGE")])])
    st.record("cluster", "broken-cluster", action="failed", error="quota exceeded")
    imp.run()
    row = st.row("acl", "clusters:broken-cluster")
    assert row["failure_category"] == "unit_failed_earlier"
    assert "fix that failure and retry" in row["last_error"]


def test_acl_rows_are_per_object_and_retryable():
    """Without their own state rows, skipped grants would be INVISIBLE to retry_mode — the units
    most likely to need a second pass."""
    client = RecordingClient()
    imp, st, _aw = _make(AclImporter, [], client,
                         acls=[_acl("directory", "/Shared/.bundle/b", "directories",
                                    [_grant("g1", "CAN_MANAGE"), _grant("g2", "CAN_READ")])])
    imp.run()
    st.flush()
    st.load(force=True)
    rows = [k for k in st._cache if k[0] == "acl"]
    assert len(rows) == 1, "one row per OBJECT, not per grant"
    assert ("acl", "directories:/Shared/.bundle/b") in st.retry_keys("skipped_only")
    assert ("acl", "directories:/Shared/.bundle/b") not in (st.retry_keys("failed_only") or set())


def test_the_acl_fingerprint_moves_when_a_grant_changes():
    """An ACL changed on source must be replayed; an unchanged one must not."""
    client = RecordingClient()
    imp_a, _st, _aw = _make(AclImporter, [], client,
                            acls=[_acl("cluster", "etl", "clusters",
                                       [_grant("g1", "CAN_MANAGE")])])
    fp_a = imp_a.load()[0]["fingerprint"]
    imp_b, _st2, _aw2 = _make(AclImporter, [], RecordingClient(),
                              acls=[_acl("cluster", "etl", "clusters",
                                         [_grant("g1", "CAN_MANAGE"),
                                          _grant("g2", "CAN_VIEW")])])
    fp_b = imp_b.load()[0]["fingerprint"]
    assert fp_a != fp_b, "an added grant must move the ACL fingerprint"
    # and inherited echoes must NOT affect it (they aren't part of the object's own ACL)
    imp_c, _st3, _aw3 = _make(AclImporter, [], RecordingClient(),
                              acls=[_acl("cluster", "etl", "clusters",
                                         [_grant("g1", "CAN_MANAGE"),
                                          _grant("x", "CAN_VIEW", inherited=True)])])
    assert imp_c.load()[0]["fingerprint"] == fp_a, \
        "an inherited echo must not move the fingerprint"


def test_the_parity_report_proves_a_match_and_detects_a_broken_grant():
    """§6b: parity is PROVEN by a post-apply diff, not asserted."""
    # target returns exactly what we applied → match
    client = RecordingClient(get_table={
        "api/2.0/permissions/clusters/TGT-CLU": {"access_control_list": [
            {"group_name": "data-eng", "all_permissions": [
                {"permission_level": "CAN_MANAGE", "inherited": False}]}]}})
    imp, _st, aw = _make(AclImporter, [], client,
                         acls=[_acl("cluster", "etl", "clusters",
                                    [_grant("data-eng", "CAN_MANAGE")])],
                         context={"cluster_target_ids": {"etl": "TGT-CLU"}})
    imp.run()
    report = aw.read_json("acl_parity_report.json")
    assert report["counts"]["match"] == 1
    assert report["objects"][0]["verdict"] == "match"

    # now break it: the target is missing the grant → missing_on_target, NOT a silent pass
    client2 = RecordingClient(get_table={
        "api/2.0/permissions/clusters/TGT-CLU": {"access_control_list": []}})
    imp2, _st2, aw2 = _make(AclImporter, [], client2,
                            acls=[_acl("cluster", "etl", "clusters",
                                       [_grant("data-eng", "CAN_MANAGE")])],
                            context={"cluster_target_ids": {"etl": "TGT-CLU"}})
    imp2.run()
    report2 = aw2.read_json("acl_parity_report.json")
    assert report2["counts"]["missing_on_target"] == 1
    assert report2["objects"][0]["missing_on_target"] == [["data-eng", "CAN_MANAGE"]]


def test_an_unchanged_acl_SKIPS_on_a_re_run():
    """§6b-i: re-runs replay only genuinely-changed ACLs. Re-PUTting every object every run is
    correct-but-wasteful — one API call per object per run, the slowest thing in the tool."""
    client = RecordingClient()
    imp, st, _aw = _make(AclImporter, [], client,
                         acls=[_acl("cluster", "etl", "clusters",
                                    [_grant("data-eng", "CAN_MANAGE")])],
                         context={"cluster_target_ids": {"etl": "TGT-CLU"}})
    imp.run()
    assert len([c for c in client.calls if c[0] == "PUT"]) == 1
    st.flush()

    # A second run over the same state + unchanged grants.
    client2 = RecordingClient()
    imp2 = AclImporter(client2, imp.config, imp.staging, state=st, units_by_type={},
                       context={"cluster_target_ids": {"etl": "TGT-CLU"}})
    imp2.config.imports.force_full_import = True   # bypass the checkpoint, exercise the STATE path
    res2 = imp2.run()
    assert [c for c in client2.calls if c[0] == "PUT"] == [], \
        "an unchanged ACL must not be re-applied"
    assert res2.skipped == 1


def test_the_parity_report_still_verifies_on_a_run_where_every_acl_SKIPPED():
    """REGRESSION (found live): verifying only what THIS run applied made the report empty on every
    run after the first — the parity evidence vanished exactly when a re-run should confirm it.
    Parity is a claim about the TARGET's current state, not about this run's activity.
    """
    target_acl = {"access_control_list": [
        {"group_name": "data-eng", "all_permissions": [
            {"permission_level": "CAN_MANAGE", "inherited": False}]}]}
    client = RecordingClient(get_table={"api/2.0/permissions/clusters/TGT-CLU": target_acl})
    imp, st, _aw = _make(AclImporter, [], client,
                         acls=[_acl("cluster", "etl", "clusters",
                                    [_grant("data-eng", "CAN_MANAGE")])],
                         context={"cluster_target_ids": {"etl": "TGT-CLU"}})
    imp.run()
    st.flush()

    client2 = RecordingClient(get_table={"api/2.0/permissions/clusters/TGT-CLU": target_acl})
    imp2 = AclImporter(client2, imp.config, imp.staging, state=st, units_by_type={},
                       context={"cluster_target_ids": {"etl": "TGT-CLU"}})
    imp2.config.imports.force_full_import = True
    res2 = imp2.run()
    assert res2.skipped == 1, "the ACL should have skipped"
    report = imp2.staging.read_json("acl_parity_report.json")
    assert report["objects_checked"] == 1, \
        "the parity report must still verify an ACL applied by an EARLIER run"
    assert report["counts"]["match"] == 1


def test_secret_scope_acls_use_their_own_api_and_are_excluded_from_the_diff():
    """A scope's ACL is a different endpoint, one call PER PRINCIPAL, and ADDITIVE rather than
    declarative — so it must not be sent to `PUT permissions` nor diffed as if it were absolute."""
    client = RecordingClient()
    imp, _st, aw = _make(AclImporter, [], client,
                         acls=[_acl("secret_scope", "app-secrets", "secret-scope",
                                    [_grant("data-eng", "READ"),
                                     _grant("users", "MANAGE")])],
                         context={"secret_scope_target_ids": {"app-secrets": "app-secrets"}})
    res = imp.run()
    assert [c for c in client.calls if c[0] == "PUT"] == [], \
        "a secret-scope ACL must NOT go to PUT permissions (that path 404s)"
    puts = client.posts_to("secrets/acls/put")
    assert len(puts) == 1, "one call per principal, and users:MANAGE is set at scope-create"
    assert puts[0][2] == {"scope": "app-secrets", "principal": "data-eng", "permission": "READ"}
    assert res.created == 1
    report = aw.read_json("acl_parity_report.json") or {}
    assert report.get("objects_checked") == 0, \
        "an additive scope ACL must not be diffed as if it were declarative"


def test_parity_drops_inherited_on_BOTH_sides_so_like_compares_with_like():
    """The target recomputes inheritance from its own tree, so comparing raw GETs would report
    differences that aren't real."""
    client = RecordingClient(get_table={
        "api/2.0/permissions/clusters/TGT-CLU": {"access_control_list": [
            {"group_name": "data-eng", "all_permissions": [
                {"permission_level": "CAN_MANAGE", "inherited": False}]},
            {"group_name": "some-ancestor-group", "all_permissions": [
                {"permission_level": "CAN_VIEW", "inherited": True}]}]}})
    imp, _st, aw = _make(AclImporter, [], client,
                         acls=[_acl("cluster", "etl", "clusters",
                                    [_grant("data-eng", "CAN_MANAGE")])],
                         context={"cluster_target_ids": {"etl": "TGT-CLU"}})
    imp.run()
    report = aw.read_json("acl_parity_report.json")
    assert report["counts"]["match"] == 1, \
        "an inherited grant on the target must not count as `extra_on_target`"


def test_an_object_with_only_inherited_grants_needs_no_put():
    client = RecordingClient()
    imp, _st, _aw = _make(AclImporter, [], client,
                          acls=[_acl("cluster", "etl", "clusters",
                                     [_grant("x", "CAN_VIEW", inherited=True)])],
                          context={"cluster_target_ids": {"etl": "TGT-CLU"}})
    res = imp.run()
    assert [c for c in client.calls if c[0] == "PUT"] == []
    assert res.created == 1
    assert "already matches by construction" in res.units[0]["note"]


def test_workspace_content_acls_resolve_by_path_not_by_source_id():
    """A source object_id is meaningless on target; get-status returns the target's own id."""
    client = RecordingClient(status_paths={"/Shared/etl"})
    client.get_table["api/2.0/workspace/get-status"] = {}
    imp, _st, _aw = _make(AclImporter, [], client,
                          acls=[_acl("notebook", "/Shared/etl", "notebooks",
                                     [_grant("data-eng", "CAN_READ")], source_id="SRC-999")])
    # get-status must return an object_id for the path
    def fake_get(path, params=None):
        client.calls.append(("GET", path, params))
        if path == "api/2.0/workspace/get-status":
            return {"path": "/Shared/etl", "object_id": "TARGET-12345"}
        return {}
    imp.client.get = fake_get
    imp.run()
    put = [c for c in client.calls if c[0] == "PUT"]
    assert put and "TARGET-12345" in put[0][1], \
        "the ACL must be applied to the TARGET object id resolved by path"
