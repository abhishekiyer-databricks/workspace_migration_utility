"""Offline tests for the identity importer — phase 1 (Plan 3 §6, §7.1).

Identity is the highest-risk phase, and its risks are specific:
  • creating an account-managed SP instead of assigning it mints a NEW applicationId and ORPHANS
    every ACL that referenced it — so create-vs-assign is tested per classification;
  • a recreated DB-managed SP's old→new appId map cannot be rebuilt from the target, so the map must
    be written per identity, including for identities we deliberately do NOT create;
  • groups must be two-pass, or nested membership resolves only by luck of ordering;
  • built-in group MEMBERSHIP must migrate even though the group itself must not be created —
    otherwise a source admin silently isn't an admin on target.
"""
from __future__ import annotations

import tempfile

from src.config.config_manager import Config
from src.exporters.artifact_writer import ArtifactWriter
from src.importers.identity_importer import IdentityImporter
from src.state.state_store import StateStore
from tests.test_state_store import FakeBackend


class FakeScimClient:
    """Records every SCIM call and mints ids, so ordering and payloads can be asserted."""

    def __init__(self, users=None, sps=None, groups=None):
        self.scim = {"Users": list(users or []), "ServicePrincipals": list(sps or []),
                     "Groups": list(groups or [])}
        self.calls: list[tuple] = []
        self._n = 0
        self.fail_paths: set = set()

    def get_scim(self, resource, max_items=0, count=500):
        return list(self.scim.get(resource, []))

    def get(self, path, params=None):
        self.calls.append(("GET", path, params))
        return {}

    def post(self, path, body):
        self.calls.append(("POST", path, body))
        if path in self.fail_paths:
            raise RuntimeError("INVALID_PARAMETER_VALUE: nope")
        self._n += 1
        out = {"id": f"scim-{self._n}"}
        if path.endswith("/ServicePrincipals"):
            # The target mints a BRAND-NEW applicationId — the whole reason the map must be durable.
            out["applicationId"] = f"new-app-uuid-{self._n}"
        return out

    def patch(self, path, body):
        self.calls.append(("PATCH", path, body))
        if path in self.fail_paths:
            raise RuntimeError("PERMISSION_DENIED: cannot patch")
        return {}

    def patches_to(self, needle):
        return [c for c in self.calls if c[0] == "PATCH" and needle in c[1]]

    def posts_to(self, needle):
        return [c for c in self.calls if c[0] == "POST" and needle in c[1]]


def _unit(asset_type, key, payload, classification="", **over):
    u = {"asset_type": asset_type, "natural_key": key, "source_id": f"src-{key}",
         "fingerprint": f"sha256:{key}", "import_action": "create", "export_status": "success",
         "classification": classification, "payload": payload, "note": ""}
    u.update(over)
    return u


def _importer(client, units, dry_run=False, state=True):
    d = tempfile.mkdtemp()
    cfg = Config.from_dict({"role": "target", "source_workspace_id": "111", "run_id": "r1",
                            "target_staging_location": d, "dry_run": dry_run,
                            "imports": ({"state_catalog": "c", "state_schema": "s"}
                                        if state else {})})
    aw = ArtifactWriter(cfg)
    aw.ensure_output_path()
    st = None
    if state:
        st = StateStore(FakeBackend(), cfg)
        st.ensure_table()
        st.load()
    by_type: dict = {}
    for u in units:
        by_type.setdefault(u["asset_type"], []).append(u)
    return IdentityImporter(client, cfg, aw, state=st, units_by_type=by_type), st


# ── create vs assign, per classification ───────────────────────────────────

def test_a_db_managed_sp_is_recreated_and_its_new_appid_is_mapped():
    """The mapping that cannot be recovered any other way: old appId → NEW appId."""
    client = FakeScimClient()
    imp, st = _importer(client, [
        _unit("service_principal", "old-app-uuid",
              {"displayName": "etl-sp", "entitlements": [{"value": "allow-cluster-create"}]},
              classification="db_managed_sp")])
    res = imp.run()
    assert res.created == 1
    assert client.posts_to("/ServicePrincipals"), "a DB-managed SP must be created"
    mapping = st.load_identity_map()["sp_mapping"]
    assert mapping["old-app-uuid"] == "new-app-uuid-1", \
        "the old→new applicationId map was not recorded"


def test_an_account_sp_is_NEVER_created_and_reports_a_prerequisite():
    """Creating an account/UMI SP mints a new appId and orphans every ACL — so it must not happen,
    and the gap must be reported as a prerequisite rather than an API error."""
    client = FakeScimClient()
    imp, st = _importer(client, [
        _unit("service_principal", "umi-app-uuid", {"displayName": "umi"},
              classification="umi_or_entra_sp")])
    res = imp.run()
    assert client.posts_to("/ServicePrincipals") == [], \
        "an account-managed SP must never be created"
    assert res.failed == 1
    row = st.row("service_principal", "umi-app-uuid")
    assert row["failure_category"] == "prerequisite_missing"
    assert "account admin" in row["last_error"]
    assert "orphan" in row["last_error"], "the message must say WHY, not just that it failed"


def test_an_account_sp_already_assigned_is_adopted_and_its_target_id_recorded():
    """The appId is stable but the TARGET SCIM id differs — and ACL remap needs that target id, so
    an adopt MUST still write an identity-map row."""
    client = FakeScimClient(sps=[{"id": "scim-existing", "applicationId": "umi-app-uuid"}])
    imp, st = _importer(client, [
        _unit("service_principal", "umi-app-uuid", {"displayName": "umi"},
              classification="umi_or_entra_sp")])
    res = imp.run()
    assert res.adopted == 1 and res.failed == 0
    m = st.load_identity_map()
    assert m["sp_mapping"]["umi-app-uuid"] == "umi-app-uuid", "appId must be unchanged"
    assert m["scim_ids"]["service_principal:umi-app-uuid"] == "scim-existing", \
        "the TARGET scim id must be recorded even though nothing was created"


def test_an_entra_user_is_never_created():
    client = FakeScimClient()
    imp, st = _importer(client, [
        _unit("user", "someone@corp.com", {"userName": "someone@corp.com"},
              classification="entra_user")])
    res = imp.run()
    assert client.posts_to("/Users") == []
    assert res.failed == 1
    assert st.row("user", "someone@corp.com")["failure_category"] == "prerequisite_missing"


def test_an_existing_entra_user_is_adopted_with_its_target_id():
    client = FakeScimClient(users=[{"id": "u-9", "userName": "someone@corp.com"}])
    imp, st = _importer(client, [
        _unit("user", "someone@corp.com", {"userName": "someone@corp.com"},
              classification="entra_user")])
    res = imp.run()
    assert res.adopted == 1
    assert st.load_identity_map()["scim_ids"]["user:someone@corp.com"] == "u-9"


# ── groups: two-pass, name-based member resolution ─────────────────────────

def test_groups_are_created_empty_then_members_patched():
    """Two-pass, so nested/cross membership resolves regardless of order."""
    client = FakeScimClient(users=[{"id": "u-1", "userName": "a@b.com"}])
    imp, _st = _importer(client, [
        _unit("group", "finance", {"displayName": "finance",
                                   "members": [{"display": "a@b.com", "kind": "user",
                                                "value": "SOURCE-ID-IGNORED"}]},
              classification="db_managed_group")])
    imp.run()
    create = client.posts_to("/Groups")
    assert len(create) == 1
    assert "members" not in create[0][2], "the group must be created EMPTY, members come after"
    patches = client.patches_to("/Groups/")
    member_patch = [p for p in patches if p[2]["Operations"][0]["path"] == "members"]
    assert member_patch, "members were never patched"
    # resolved BY NAME to the TARGET id, never the source id
    assert member_patch[0][2]["Operations"][0]["value"] == [{"value": "u-1"}]


def test_nested_group_membership_resolves_in_either_order():
    """A parent group listed BEFORE its child must STILL get the child as a member.

    This is the whole point of the second pass: membership is applied only after every group exists,
    so bundle ordering is irrelevant. Patching members during each group's own create looked
    equivalent but silently under-populated any group whose members came later in the bundle — the
    exact bug two-pass exists to prevent.
    """
    client = FakeScimClient()
    imp, _st = _importer(client, [
        # parent FIRST, child second — the ordering that a one-pass implementation gets wrong
        _unit("group", "parent", {"displayName": "parent",
                                  "members": [{"display": "child", "kind": "group"}]},
              classification="db_managed_group"),
        _unit("group", "child", {"displayName": "child", "members": []},
              classification="db_managed_group"),
    ])
    res = imp.run()
    assert res.created == 2
    parent_row = next(r for r in res.units if r["natural_key"] == "parent")
    assert parent_row["note"] == "1/1 members added", \
        "the forward-referenced child group was not added to its parent"
    assert parent_row["import_status"] == "created", "a fully-populated group is not degraded"
    # and the member really is the CHILD's target id, resolved by name
    child_id = imp._target_groups["child"]
    member_patch = [p for p in client.patches_to("/Groups/")
                    if p[2]["Operations"][0]["path"] == "members"]
    assert member_patch[0][2]["Operations"][0]["value"] == [{"value": child_id}]


def test_a_member_that_is_a_recreated_sp_resolves_through_the_id_map():
    """A DB-managed SP got a NEW appId, so the source appId matches nothing on target — the map is
    what closes the gap."""
    client = FakeScimClient(sps=[{"id": "sp-t", "applicationId": "new-app"}])
    imp, _st = _importer(client, [
        _unit("group", "g", {"displayName": "g",
                             "members": [{"display": "old-app", "kind": "service_principal"}]},
              classification="db_managed_group")])
    imp.identity_map = {"sp_mapping": {"old-app": "new-app"}}
    imp.run()
    patch = [p for p in client.patches_to("/Groups/")
             if p[2]["Operations"][0]["path"] == "members"]
    assert patch and patch[0][2]["Operations"][0]["value"] == [{"value": "sp-t"}]


def test_an_unresolvable_member_does_not_lose_the_resolvable_ones():
    """Sending a dangling id would fail the whole PATCH; partial success + an explicit list is
    strictly better than all-or-nothing."""
    client = FakeScimClient(users=[{"id": "u-1", "userName": "here@b.com"}])
    imp, _st = _importer(client, [
        _unit("group", "g", {"displayName": "g", "members": [
            {"display": "here@b.com", "kind": "user"},
            {"display": "missing@b.com", "kind": "user"}]},
              classification="db_managed_group")])
    res = imp.run()
    patch = [p for p in client.patches_to("/Groups/")
             if p[2]["Operations"][0]["path"] == "members"]
    assert patch[0][2]["Operations"][0]["value"] == [{"value": "u-1"}]
    assert "1/2 members added" in res.units[0]["note"]
    assert "missing@b.com" in res.units[0]["note"], "the unresolved member must be NAMED"


def test_an_account_group_is_not_recreated():
    client = FakeScimClient()
    imp, _st = _importer(client, [
        _unit("group", "corp-analysts", {"displayName": "corp-analysts"},
              classification="account_group")])
    res = imp.run()
    assert client.posts_to("/Groups") == []
    assert res.failed == 1


# ── built-in groups: never created, but membership MUST migrate ─────────────

def test_builtin_group_membership_is_patched_onto_the_existing_group():
    """Without this a source workspace admin would silently NOT be an admin on target."""
    client = FakeScimClient(users=[{"id": "u-admin", "userName": "boss@corp.com"}],
                            groups=[{"id": "grp-admins", "displayName": "admins"}])
    imp, st = _importer(client, [
        _unit("group_membership", "admins",
              {"displayName": "admins", "members": [{"display": "boss@corp.com", "kind": "user"}]},
              classification="builtin_group", import_action="add_members")])
    res = imp.run()
    assert client.posts_to("/Groups") == [], "a built-in group must never be created"
    patch = [p for p in client.patches_to("/Groups/grp-admins")
             if p[2]["Operations"][0]["path"] == "members"]
    assert patch, "membership was not added to the existing built-in group"
    assert patch[0][2]["Operations"][0]["value"] == [{"value": "u-admin"}]
    assert res.created == 1
    # the built-in group still needs a map row, because ACL remap resolves `admins` through it
    assert st.load_identity_map()["group_map"]["admins"] == "grp-admins"


# ── entitlements + roles are SEPARATE patches after create ──────────────────

def test_entitlements_and_roles_are_applied_as_separate_patches_after_create():
    client = FakeScimClient()
    imp, _st = _importer(client, [
        _unit("service_principal", "app-1",
              {"displayName": "sp", "entitlements": [{"value": "allow-cluster-create"}],
               "roles": [{"value": "some-role"}]},
              classification="db_managed_sp")])
    imp.run()
    create = client.posts_to("/ServicePrincipals")[0][2]
    assert "entitlements" not in create and "roles" not in create, \
        "several workspaces reject these inline — they must be separate PATCHes"
    paths = [p[2]["Operations"][0]["path"] for p in client.patches_to("/ServicePrincipals/")]
    assert "entitlements" in paths and "roles" in paths


def test_a_failed_entitlement_patch_does_not_fail_the_identity():
    """The identity already exists by then; losing it over an entitlement would be worse."""
    client = FakeScimClient()
    client.fail_paths = {"api/2.0/preview/scim/v2/ServicePrincipals/scim-1"}
    imp, _st = _importer(client, [
        _unit("service_principal", "app-1",
              {"displayName": "sp", "entitlements": [{"value": "allow-cluster-create"}]},
              classification="db_managed_sp")])
    res = imp.run()
    assert res.created == 1 and res.failed == 0
    assert any("could not set entitlements" in w for w in res.warnings)


# ── an SP with OAuth secrets is created but flagged degraded ────────────────

def test_an_sp_with_oauth_secrets_is_created_with_a_warning():
    """The SP exists but cannot authenticate until a new secret is minted — degraded, not clean."""
    client = FakeScimClient()
    imp, _st = _importer(client, [
        _unit("service_principal", "app-1", {"displayName": "sp"},
              classification="db_managed_sp",
              note="OAuth client secret(s) present — NOT exportable; recreate on target manually.")])
    res = imp.run()
    assert res.warned == 1, "an SP whose secrets can't migrate must not report as fully clean"
    assert "CANNOT be migrated" in res.units[0]["note"]


# ── ordering + dry run ─────────────────────────────────────────────────────

def test_load_order_is_users_then_sps_then_groups_then_membership():
    client = FakeScimClient()
    imp, _st = _importer(client, [
        _unit("group_membership", "admins", {"displayName": "admins", "members": []}),
        _unit("group", "g", {"displayName": "g"}, classification="db_managed_group"),
        _unit("user", "u@b.com", {"userName": "u@b.com"}, classification="needs_review"),
        _unit("service_principal", "app", {"displayName": "sp"}, classification="db_managed_sp"),
    ])
    order = [u["asset_type"] for u in imp.load()]
    assert order == ["user", "service_principal", "group", "group_membership"]


def test_dry_run_creates_no_identity_and_writes_no_map():
    client = FakeScimClient()
    imp, st = _importer(client, [
        _unit("service_principal", "app-1", {"displayName": "sp"},
              classification="db_managed_sp")], dry_run=True)
    res = imp.run()
    assert client.posts_to("/ServicePrincipals") == [] and client.patches_to("/") == []
    assert res.dry_run == 1
    assert st.load_identity_map()["sp_mapping"] == {}, \
        "a rehearsal must not write identity mappings"


def test_existence_check_paginates_via_get_scim():
    """SCIM pagination is a known latent bug in the reference tool; a truncated list here would
    report an existing identity as absent and CREATE A DUPLICATE."""
    seen = {}

    class CountingClient(FakeScimClient):
        def get_scim(self, resource, max_items=0, count=500):
            seen[resource] = seen.get(resource, 0) + 1
            return super().get_scim(resource, max_items, count)

    client = CountingClient()
    imp, _st = _importer(client, [])
    imp.existing_keys()
    assert set(seen) == {"Users", "ServicePrincipals", "Groups"}, \
        "all three SCIM types must be listed through the paginating helper"
