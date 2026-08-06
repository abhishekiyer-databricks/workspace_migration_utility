"""Offline tests for phases 2–5: compute, workspace, secrets, jobs (Plan 3 §6, §6c, §7d).

Each test targets a specific trap the plan calls out — the ones where a naive implementation looks
correct, passes a smoke test, and then fails in the customer's workspace:

  compute   — ephemeral clusters excluded; STOPPED after create; re-pinned; pool/node-type conflict;
              pool+policy remapped by name, and a dangling reference dropped with a warning
  workspace — user home dirs can't be mkdir'd; notebooks land as NOTEBOOKS not files; .bundle/
              content never uploaded; missing bytes are manual, not an empty file
  secrets   — AKV needs an AAD token (distinguished from a vault refusal); MANAGE set at create;
              values always manual; no edit API
  jobs      — compute remapped in job_clusters AND tasks; schedule AND continuous paused; run_as
              remapped; an unresolvable notebook_path is created_with_warning, not a silent bomb
"""
from __future__ import annotations

import base64
import json
import os
import tempfile

from src.config.config_manager import Config
from src.exporters.artifact_writer import ArtifactWriter
from src.importers.compute_importer import ComputeImporter, is_ephemeral_cluster
from src.importers.jobs_importer import JobsImporter
from src.importers.secrets_importer import SecretsImporter
from src.importers.workspace_importer import WorkspaceImporter, is_user_home
from src.state.state_store import StateStore
from tests.test_state_store import FakeBackend


def _same_path(actual: str, wanted: str) -> bool:
    """Whether `actual` is the API path `wanted`, ignoring the `api/<version>/` prefix."""
    import re
    stripped = re.sub(r"^api/\d+\.\d+/", "", actual)
    return stripped == wanted.lstrip("/") or actual == wanted


class RecordingClient:
    """Records every call; answers GETs from a table; mints ids on POST."""

    def __init__(self, get_table=None, paginated=None, fail_paths=None, status_paths=None):
        self.get_table = get_table or {}
        self.paginated = paginated or {}
        self.fail_paths = set(fail_paths or ())
        self.status_paths = set(status_paths or ())   # workspace paths that "exist"
        self.calls: list[tuple] = []
        self._n = 0

    @property
    def base_url(self):
        return "https://target.example.net"

    def get(self, path, params=None):
        self.calls.append(("GET", path, params))
        if path == "api/2.0/workspace/get-status":
            p = (params or {}).get("path")
            if p in self.status_paths:
                return {"path": p, "object_type": "DIRECTORY"}
            raise RuntimeError("RESOURCE_DOES_NOT_EXIST")
        return self.get_table.get(path, {})

    def get_paginated(self, path, result_key, token_key="next_page_token", params=None,
                      max_pages=100000):
        self.calls.append(("GET_PAGINATED", path, params))
        return self.paginated.get(path, [])

    def get_scim(self, resource, max_items=0, count=500):
        return self.get_table.get(f"scim:{resource}", [])

    def post(self, path, body):
        self.calls.append(("POST", path, body))
        if path in self.fail_paths:
            raise RuntimeError("INVALID_PARAMETER_VALUE: rejected")
        self._n += 1
        return {"instance_pool_id": f"pool-{self._n}", "policy_id": f"pol-{self._n}",
                "cluster_id": f"clu-{self._n}", "job_id": f"job-{self._n}", "id": f"id-{self._n}"}

    def put(self, path, body):
        self.calls.append(("PUT", path, body))
        if path in self.fail_paths:
            raise RuntimeError("INVALID_PARAMETER_VALUE: rejected")
        return {}

    def patch(self, path, body):
        self.calls.append(("PATCH", path, body))
        if path in self.fail_paths:
            raise RuntimeError("INVALID_PARAMETER_VALUE: rejected")
        return {}

    def posts_to(self, path):
        """Calls to EXACTLY this path (modulo the api version prefix).

        Deliberately not a substring or suffix match: `clusters/create` also matches
        `policies/clusters/create`, which silently mixed cluster and policy bodies together and made
        a passing remap look like a failure.
        """
        return [c for c in self.calls if c[0] == "POST" and _same_path(c[1], path)]

    def bodies_to(self, path):
        return [c[2] for c in self.posts_to(path)]


def _make(importer_cls, units, client, dry_run=False, context=None, staging_files=None):
    d = tempfile.mkdtemp()
    cfg = Config.from_dict({"role": "target", "source_workspace_id": "111", "run_id": "r1",
                            "target_staging_location": d, "dry_run": dry_run,
                            "imports": {"state_catalog": "c", "state_schema": "s"}})
    aw = ArtifactWriter(cfg)
    aw.ensure_output_path()
    for rel, data in (staging_files or {}).items():
        aw.write_bytes(rel, data)
    st = StateStore(FakeBackend(), cfg)
    st.ensure_table()
    st.load()
    by_type: dict = {}
    for u in units:
        by_type.setdefault(u["asset_type"], []).append(u)
    imp = importer_cls(client, cfg, aw, state=st, units_by_type=by_type,
                       context=context if context is not None else {})
    return imp, st


def _unit(asset_type, key, payload=None, **over):
    u = {"asset_type": asset_type, "natural_key": key, "source_id": f"src-{key}",
         "fingerprint": f"sha256:{key}", "import_action": "create", "export_status": "success",
         "payload": payload or {}, "note": ""}
    u.update(over)
    return u


# ═══════════════════════════════ COMPUTE ═══════════════════════════════════

def test_ephemeral_clusters_are_never_migrated():
    """`job-*` / `dlt-execution-*` clusters die with their run; recreating them litters the target."""
    assert is_ephemeral_cluster("job-123-run-456")
    assert is_ephemeral_cluster("dlt-execution-abc")
    assert is_ephemeral_cluster("mlflow-model-x")
    assert not is_ephemeral_cluster("etl-shared-cluster")

    client = RecordingClient()
    imp, _st = _make(ComputeImporter, [
        _unit("cluster", "job-999-run-1", {"cluster_name": "job-999-run-1"}),
        _unit("cluster", "real-etl", {"cluster_name": "real-etl", "spark_version": "14.3.x"}),
    ], client)
    res = imp.run()
    assert res.total == 1, "the ephemeral cluster must not even be reported as work"
    assert [b["cluster_name"] for b in client.bodies_to("clusters/create")] == ["real-etl"]


def test_a_new_cluster_is_stopped_immediately_after_create():
    """create STARTS the cluster — migrating 30 would otherwise burn the customer's DBUs."""
    client = RecordingClient()
    imp, _st = _make(ComputeImporter, [
        _unit("cluster", "etl", {"cluster_name": "etl", "spark_version": "14.3.x"})], client)
    res = imp.run()
    assert client.posts_to("clusters/delete"), "the cluster was left RUNNING after create"
    assert "stopped immediately" in res.units[0]["note"]


def test_a_pinned_cluster_is_repinned():
    """Unpinned, a terminated cluster disappears from the UI once it is cleaned up."""
    client = RecordingClient()
    imp, _st = _make(ComputeImporter, [
        _unit("cluster", "etl", {"cluster_name": "etl", "pinned": True})], client)
    imp.run()
    assert client.posts_to("clusters/pin"), "a pinned source cluster was not re-pinned"


def test_pool_and_policy_are_remapped_by_name_to_target_ids():
    """Source ids mean nothing on target; the natural key is the only stable link."""
    client = RecordingClient()
    imp, _st = _make(ComputeImporter, [
        _unit("instance_pool", "shared-pool", {"instance_pool_name": "shared-pool"},
              source_id="SRC-POOL"),
        _unit("cluster_policy", "std-policy", {"name": "std-policy", "definition": "{}"},
              source_id="SRC-POL"),
        _unit("cluster", "etl", {"cluster_name": "etl", "instance_pool_id": "SRC-POOL",
                                 "policy_id": "SRC-POL"}),
    ], client)
    res = imp.run()
    assert res.created == 3
    body = client.bodies_to("clusters/create")[0]
    assert body["instance_pool_id"] == "pool-1", "the pool id was not remapped to the target id"
    assert body["policy_id"] == "pol-2", "the policy id was not remapped to the target id"


def test_node_types_are_stripped_when_a_pool_is_set():
    """`node_type_id` alongside `instance_pool_id` is rejected — the pool dictates the node type."""
    client = RecordingClient()
    imp, _st = _make(ComputeImporter, [
        _unit("instance_pool", "p", {"instance_pool_name": "p"}, source_id="SP"),
        _unit("cluster", "etl", {"cluster_name": "etl", "instance_pool_id": "SP",
                                 "node_type_id": "Standard_DS3_v2",
                                 "driver_node_type_id": "Standard_DS3_v2",
                                 "enable_elastic_disk": True}),
    ], client)
    imp.run()
    body = client.bodies_to("clusters/create")[0]
    for field in ("node_type_id", "driver_node_type_id", "enable_elastic_disk"):
        assert field not in body, f"{field} must not be sent with instance_pool_id"


def test_a_dangling_pool_reference_is_dropped_with_a_warning_not_a_failure():
    """A working cluster + an explicit warning beats an opaque create failure."""
    client = RecordingClient()
    imp, _st = _make(ComputeImporter, [
        _unit("cluster", "etl", {"cluster_name": "etl", "instance_pool_id": "GONE"})], client)
    res = imp.run()
    body = client.bodies_to("clusters/create")[0]
    assert "instance_pool_id" not in body
    assert res.warned == 1, "a cluster missing its pool must be reported degraded, not clean"
    assert "DROPPED" in res.units[0]["note"]


def test_the_source_creator_is_preserved_as_a_tag():
    """The API attributes the cluster to the caller, so the creator can only survive as a tag."""
    client = RecordingClient()
    imp, _st = _make(ComputeImporter, [
        _unit("cluster", "etl", {"cluster_name": "etl",
                                 "creator_user_name": "someone@corp.com"})], client)
    imp.run()
    body = client.bodies_to("clusters/create")[0]
    assert body["custom_tags"]["OriginalCreator"] == "someone@corp.com"
    assert "creator_user_name" not in body, "creator_user_name is not a create field"


def test_instance_pool_update_sends_the_full_config_with_the_id():
    """instance-pools/edit is NOT a partial update — omitting a field resets it."""
    client = RecordingClient()
    imp, st = _make(ComputeImporter, [
        _unit("instance_pool", "p", {"instance_pool_name": "p", "min_idle_instances": 5},
              fingerprint="sha256:v2")], client)
    st.record("instance_pool", "p", action="created", fingerprint="sha256:v1",
              target_object_id="pool-existing")
    imp.existing_keys = lambda: {"p": "pool-existing"}
    imp.run()
    body = client.bodies_to("instance-pools/edit")[0]
    assert body["instance_pool_id"] == "pool-existing"
    assert body["min_idle_instances"] == 5, "the full config must be sent, not just the id"


# ═══════════════════════════════ WORKSPACE ══════════════════════════════════

def test_a_user_home_directory_is_reported_as_a_prerequisite_not_mkdird():
    """`/Users/<email>` appears only when the USER is provisioned — that's why identity is phase 1."""
    assert is_user_home("/Users/a@b.com")
    assert not is_user_home("/Users/a@b.com/sub")
    client = RecordingClient()
    imp, st = _make(WorkspaceImporter, [
        _unit("directory", "/Users/missing@corp.com", {"path": "/Users/missing@corp.com"})], client)
    res = imp.run()
    assert client.posts_to("workspace/mkdirs") == [], "a home directory must never be mkdir'd"
    assert res.failed == 1
    row = st.row("directory", "/Users/missing@corp.com")
    assert row["failure_category"] == "prerequisite_missing"
    assert "provisioned" in row["last_error"]


def test_workspace_roots_are_skipped_not_created():
    client = RecordingClient()
    imp, _st = _make(WorkspaceImporter, [
        _unit("directory", "/Shared", {"path": "/Shared"}),
        _unit("directory", "/Repos", {"path": "/Repos"}),
    ], client)
    res = imp.run()
    assert client.posts_to("workspace/mkdirs") == []
    assert res.created == 2 and "exists by construction" in res.units[0]["note"]


def test_directories_are_created_top_down():
    client = RecordingClient()
    imp, _st = _make(WorkspaceImporter, [
        _unit("directory", "/Shared/a/b/c", {"path": "/Shared/a/b/c"}),
        _unit("directory", "/Shared/a", {"path": "/Shared/a"}),
        _unit("directory", "/Shared/a/b", {"path": "/Shared/a/b"}),
    ], client)
    paths = [safe_get(u) for u in imp.load()]
    assert paths == ["/Shared/a", "/Shared/a/b", "/Shared/a/b/c"]


def safe_get(unit):
    return unit["natural_key"]


def test_a_notebook_is_imported_as_a_NOTEBOOK_with_its_language():
    """Without format=SOURCE + language a `.py` lands as an opaque FILE and nothing runs."""
    client = RecordingClient()
    imp, _st = _make(WorkspaceImporter, [
        _unit("notebook", "/Shared/etl", {"path": "/Shared/etl", "language": "SQL"},
              content_ref="export/workspace/content/etl.sql")], client,
        staging_files={"export/workspace/content/etl.sql": b"SELECT 1"})
    res = imp.run()
    body = client.bodies_to("workspace/import")[0]
    assert body["format"] == "SOURCE"
    assert body["language"] == "SQL"
    assert body["object_type"] == "NOTEBOOK"
    assert base64.b64decode(body["content"]) == b"SELECT 1"
    assert res.created == 1


def test_a_workspace_file_is_imported_verbatim_as_AUTO():
    client = RecordingClient()
    imp, _st = _make(WorkspaceImporter, [
        _unit("workspace_file", "/Shared/data.csv", {"path": "/Shared/data.csv"},
              content_ref="export/workspace/content/data.bin")], client,
        staging_files={"export/workspace/content/data.bin": b"a,b\n1,2\n"})
    imp.run()
    body = client.bodies_to("workspace/import")[0]
    assert body["format"] == "AUTO", "a workspace file must not be interpreted as a notebook"
    assert "language" not in body


def test_bundle_content_is_never_uploaded():
    """Importing bundle STATE points the customer's next `bundle deploy` at SOURCE object ids."""
    client = RecordingClient()
    imp, _st = _make(WorkspaceImporter, [
        _unit("notebook", "/Shared/.bundle/b/files/nb", {"path": "/Shared/.bundle/b/files/nb"},
              import_action="dab_redeploy", migration_mode="content",
              content_ref="export/workspace/content/nb.py")], client,
        staging_files={"export/workspace/content/nb.py": b"x=1"})
    res = imp.run()
    assert client.posts_to("workspace/import") == [], "bundle content must never be uploaded"
    assert res.skipped == 1


def test_a_unit_with_no_exported_bytes_is_a_manual_copy_not_an_empty_file():
    """Uploading an empty file would look successful while losing the content."""
    client = RecordingClient()
    imp, st = _make(WorkspaceImporter, [
        _unit("notebook", "/Shared/huge", {"path": "/Shared/huge"}, content_ref="")], client)
    res = imp.run()
    assert client.posts_to("workspace/import") == []
    assert res.failed == 1
    assert "not in the bundle" in st.row("notebook", "/Shared/huge")["last_error"]


def test_repos_are_recorded_manual_and_never_created():
    """D9: repos are out of scope for import, but must stay a countable, reconcilable line."""
    client = RecordingClient()
    imp, st = _make(WorkspaceImporter, [
        _unit("repo", "/Repos/me/app", {"url": "https://github.com/o/app", "branch": "main"},
              import_action="manual", note="git repos are OUT OF SCOPE for import")], client)
    res = imp.run()
    assert client.posts_to("api/2.0/repos") == []
    assert res.manual == 1
    assert st.row("repo", "/Repos/me/app")["last_action"] == "manual"


# ═══════════════════════════════ SECRETS ════════════════════════════════════

def test_a_databricks_scope_is_created_with_manage_at_create_time():
    """`users:MANAGE` cannot be patched later — getting it wrong means delete + recreate."""
    client = RecordingClient()
    imp, _st = _make(SecretsImporter, [
        _unit("secret_scope", "app-secrets",
              {"name": "app-secrets", "backend_type": "DATABRICKS", "key_names": ["k1", "k2"]})],
        client, context={"secret_acls": {"app-secrets": [{"principal": "users",
                                                          "permission": "MANAGE"}]}})
    res = imp.run()
    body = client.bodies_to("secrets/scopes/create")[0]
    assert body["initial_manage_principal"] == "users"
    assert "2 secret VALUE(s) are NOT migratable" in res.units[0]["note"]


def test_a_named_manage_principal_is_deferred_to_an_acl_put_not_sent_at_create():
    """`initial_manage_principal` accepts ONLY `users` — a named principal must not be sent there.

    Regression (live 2026-08-06): the source scopes were MANAGEd by specific users, so the importer
    put those names in `initial_manage_principal` and the API rejected every one —
    `400 BAD_REQUEST Cannot specify <user> as initial_manage_principal` — failing 3 of 4 scopes.
    Verified against the real API that even the CALLER's own username is refused; `users` is the
    only accepted literal. The named grant is therefore applied afterwards via `secrets/acls/put`,
    which (unlike `users:MANAGE`) genuinely can be patched later.
    """
    # the MANAGE holder is read from the BUNDLE (export/acls.json), not from cross-phase context:
    # the ACL phase runs last, so the scope phase has to read it from the bundle directly.
    acls = json.dumps([{"asset_type": "secret_scope", "natural_key": "app-secrets",
                        "grants": [{"principal": "alice@corp.com", "permission_level": "MANAGE"}]}])
    client = RecordingClient()
    imp, _st = _make(SecretsImporter, [
        _unit("secret_scope", "app-secrets",
              {"name": "app-secrets", "backend_type": "DATABRICKS", "key_names": ["k1"]})],
        client, staging_files={"export/acls.json": acls.encode()})
    res = imp.run()

    body = client.bodies_to("secrets/scopes/create")[0]
    assert body["initial_manage_principal"] == "users", \
        f"only `users` may be sent at create, got {body['initial_manage_principal']!r}"
    assert "alice@corp.com" not in str(body), "a named principal must never reach the create body"

    # …and it must still END UP with MANAGE, via the ACL API
    acl_bodies = client.bodies_to("secrets/acls/put")
    assert any(b.get("principal") == "alice@corp.com" and b.get("permission") == "MANAGE"
               and b.get("scope") == "app-secrets" for b in acl_bodies), \
        f"expected a MANAGE acls/put for alice@corp.com, got {acl_bodies}"
    assert res.units[0]["target_id"] == "app-secrets"


def test_an_akv_scope_without_an_aad_token_fails_with_the_right_remediation():
    """A Databricks token CANNOT make this call — and the message must say which fix applies."""
    client = RecordingClient()
    imp, st = _make(SecretsImporter, [
        _unit("secret_scope", "kv-scope",
              {"name": "kv-scope", "backend_type": "AZURE_KEYVAULT",
               "keyvault_metadata": {"dns_name": "https://v.vault.azure.net/",
                                     "resource_id": "/subscriptions/x/v"}})], client)
    res = imp.run()
    assert client.posts_to("secrets/scopes/create") == [], \
        "an AKV scope must not be attempted with a Databricks token"
    assert res.failed == 1
    err = st.row("secret_scope", "kv-scope")["last_error"]
    assert "userAADToken" in err and "Entra SP" in err
    assert st.row("secret_scope", "kv-scope")["failure_category"] == "prerequisite_missing"


def test_an_akv_scope_with_an_aad_token_links_to_the_source_vault(monkeypatch):
    """With a token it links to the SAME vault, and says the cross-region dependency out loud."""
    posted = {}

    class FakeResp:
        status_code = 200
        text = "{}"

        @staticmethod
        def json():
            return {}

    def fake_post(url, headers=None, json=None, timeout=None):
        posted["url"] = url
        posted["auth"] = headers.get("Authorization")
        posted["body"] = json
        return FakeResp()

    import requests
    monkeypatch.setattr(requests, "post", fake_post)

    client = RecordingClient()
    imp, _st = _make(SecretsImporter, [
        _unit("secret_scope", "kv-scope",
              {"name": "kv-scope", "backend_type": "AZURE_KEYVAULT",
               "keyvault_metadata": {"dns_name": "https://v.vault.azure.net/",
                                     "resource_id": "/subscriptions/x/v"}})],
        client, context={"aad_token": "AAD-TOKEN-XYZ"})
    res = imp.run()
    assert posted["auth"] == "Bearer AAD-TOKEN-XYZ", "the AAD token must be used for THIS call"
    assert posted["body"]["scope_backend_type"] == "AZURE_KEYVAULT"
    assert posted["body"]["backend_azure_keyvault"]["dns_name"] == "https://v.vault.azure.net/"
    assert res.created == 1
    assert "CROSS-REGION" in res.units[0]["note"]


def test_a_secret_scope_has_no_edit_api_so_a_change_is_reported_not_applied():
    """Recreating a scope would DELETE every value in it — worse than an honest report."""
    client = RecordingClient()
    imp, st = _make(SecretsImporter, [
        _unit("secret_scope", "s", {"name": "s", "backend_type": "DATABRICKS",
                                    "key_names": ["a", "b"]}, fingerprint="sha256:v2")], client)
    st.record("secret_scope", "s", action="created", fingerprint="sha256:v1", target_object_id="s")
    imp.existing_keys = lambda: {"s": "s"}
    res = imp.run()
    assert client.posts_to("secrets/scopes/create") == []
    assert res.warned == 1
    assert "no edit API exists" in res.units[0]["note"]


# ═══════════════════════════════ JOBS ═══════════════════════════════════════

def _job_unit(name, settings, **over):
    return _unit("job", name, settings, **over)


def test_compute_is_remapped_in_BOTH_job_clusters_and_tasks():
    """Missing either leaves the job pointing at a SOURCE cluster id, failing only at run time."""
    client = RecordingClient()
    imp, _st = _make(JobsImporter, [
        _job_unit("etl", {
            "name": "etl",
            "job_clusters": [{"job_cluster_key": "jc",
                              "new_cluster": {"policy_id": "SRC-POL", "spark_version": "14.3.x"}}],
            "tasks": [{"task_key": "t1", "existing_cluster_id": "SRC-CLU"},
                      {"task_key": "t2",
                       "new_cluster": {"instance_pool_id": "SRC-POOL",
                                       "node_type_id": "Standard_DS3_v2"}}],
        })], client, context={
            "cluster_target_ids": {"etl-cluster": "TGT-CLU"},
            "cluster_policy_target_ids": {"std": "TGT-POL"},
            "instance_pool_target_ids": {"pool": "TGT-POOL"}})
    # source_id → natural_key comes from the bundle's own units
    imp.units_by_type["cluster"] = [_unit("cluster", "etl-cluster", source_id="SRC-CLU")]
    imp.units_by_type["cluster_policy"] = [_unit("cluster_policy", "std", source_id="SRC-POL")]
    imp.units_by_type["instance_pool"] = [_unit("instance_pool", "pool", source_id="SRC-POOL")]

    imp.run()
    body = client.bodies_to("jobs/create")[0]
    assert body["tasks"][0]["existing_cluster_id"] == "TGT-CLU", "task cluster not remapped"
    assert body["job_clusters"][0]["new_cluster"]["policy_id"] == "TGT-POL", \
        "job_clusters policy not remapped"
    assert body["tasks"][1]["new_cluster"]["instance_pool_id"] == "TGT-POOL"
    assert "node_type_id" not in body["tasks"][1]["new_cluster"], \
        "node type must be dropped when a pool is set"


def test_schedule_AND_continuous_are_both_paused():
    """Pausing only `schedule` lets a CONTINUOUS job run against half-migrated data immediately."""
    client = RecordingClient()
    imp, _st = _make(JobsImporter, [
        _job_unit("j", {"name": "j",
                        "schedule": {"quartz_cron_expression": "0 0 1 * * ?",
                                     "pause_status": "UNPAUSED"},
                        "continuous": {"pause_status": "UNPAUSED"},
                        "trigger": {"pause_status": "UNPAUSED"}})], client)
    imp.run()
    body = client.bodies_to("jobs/create")[0]
    assert body["schedule"]["pause_status"] == "PAUSED"
    assert body["continuous"]["pause_status"] == "PAUSED", "a continuous job was left RUNNING"
    assert body["trigger"]["pause_status"] == "PAUSED"


def test_run_as_service_principal_is_remapped_through_the_sp_map():
    """The reference tool does NOT do this; an unmapped run_as names an appId absent on target."""
    client = RecordingClient()
    imp, _st = _make(JobsImporter, [
        _job_unit("j", {"name": "j", "run_as": {"service_principal_name": "old-app"}})], client)
    imp.identity_map = {"sp_mapping": {"old-app": "new-app"}}
    imp.run()
    body = client.bodies_to("jobs/create")[0]
    assert body["run_as"]["service_principal_name"] == "new-app"


def test_an_unmapped_run_as_is_warned_about_rather_than_silently_left():
    client = RecordingClient()
    imp, _st = _make(JobsImporter, [
        _job_unit("j", {"name": "j", "run_as": {"service_principal_name": "unknown-app"}})], client)
    res = imp.run()
    assert res.warned == 1
    assert "not in the identity map" in res.units[0]["note"]


def test_an_unresolvable_notebook_path_is_created_with_a_warning():
    """THE time-bomb check (D14): the Jobs API accepts a bad path and the job fails at FIRST RUN, so
    a create-failure check alone would never catch it."""
    client = RecordingClient()
    imp, st = _make(JobsImporter, [
        _job_unit("j", {"name": "j", "tasks": [
            {"task_key": "t1",
             "notebook_task": {"notebook_path": "/Repos/me/gitfolder/nb"}}]})], client)
    res = imp.run()
    assert client.posts_to("jobs/create"), "the job must still be created — the path may be intended"
    assert res.warned == 1
    note = res.units[0]["note"]
    assert "FAIL AT FIRST RUN" in note and "Git folder" in note
    assert st.row("job", "j")["last_action"] == "created_with_warning"


def test_a_resolvable_notebook_path_produces_no_warning():
    client = RecordingClient(status_paths={"/Shared/etl"})
    imp, _st = _make(JobsImporter, [
        _job_unit("j", {"name": "j", "tasks": [
            {"task_key": "t1", "notebook_task": {"notebook_path": "/Shared/etl"}}]})], client,
        context={"workspace_paths": {"/Shared/etl"}})
    res = imp.run()
    assert res.created == 1 and res.warned == 0


def test_external_storage_paths_are_not_flagged():
    """A dbfs:/ or /Volumes/ path is not workspace content, so it must not be reported missing."""
    client = RecordingClient()
    imp, _st = _make(JobsImporter, [
        _job_unit("j", {"name": "j", "tasks": [
            {"task_key": "t", "spark_python_task": {"python_file": "dbfs:/scripts/x.py"}}]})],
        client)
    res = imp.run()
    assert res.created == 1 and res.warned == 0


def test_jobs_existence_check_is_paginated_and_expands_tasks():
    """A truncated jobs list duplicates every job past the first page; without expand_tasks the
    response drops `tasks` entirely."""
    client = RecordingClient(paginated={"api/2.1/jobs/list": [
        {"job_id": "1", "settings": {"name": "existing-job"}}]})
    imp, _st = _make(JobsImporter, [], client)
    found = imp.existing_keys()
    assert found == {"existing-job": "1"}
    call = [c for c in client.calls if c[0] == "GET_PAGINATED"][0]
    assert call[2]["expand_tasks"] == "true"


def test_job_update_uses_reset_which_replaces_settings_wholesale():
    client = RecordingClient()
    imp, st = _make(JobsImporter, [
        _job_unit("j", {"name": "j", "max_concurrent_runs": 4}, fingerprint="sha256:v2")], client)
    st.record("job", "j", action="created", fingerprint="sha256:v1", target_object_id="job-existing")
    imp.existing_keys = lambda: {"j": "job-existing"}
    imp.run()
    body = client.bodies_to("jobs/reset")[0]
    assert body["job_id"] == "job-existing"
    assert body["new_settings"]["max_concurrent_runs"] == 4


def test_server_side_echoes_are_stripped_from_the_job_body():
    client = RecordingClient()
    imp, _st = _make(JobsImporter, [
        _job_unit("j", {"name": "j", "created_time": 123, "creator_user_name": "a@b.com",
                        "job_id": "SOURCE-ID"})], client)
    imp.run()
    body = client.bodies_to("jobs/create")[0]
    for field in ("created_time", "creator_user_name", "job_id"):
        assert field not in body, f"{field} is not a create field"
