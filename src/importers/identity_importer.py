"""
IdentityImporter — phase 1, the highest-risk write in the tool (Plan 3 §6, §7.1; master §7).

Order WITHIN the phase is fixed and load-bearing:
    users → service principals → groups (created EMPTY) → group members → entitlements

**Why groups are two-pass.** A group's members can include other groups, in any order, and forward
references are normal. Creating every group empty first and PATCHing members afterwards makes
membership resolve regardless of order, with no topological sort to get wrong.

**Users and SPs need no create-vs-assign decision at all (Plan 6 F3/F4/F5).** A POST to WORKSPACE
SCIM `/Users` or `/ServicePrincipals` creates the principal AT THE ACCOUNT and assigns it here,
returning the account's own id. So:

    user  → POST with `userName`       → dedupes by userName, adopts if it already exists
    SP    → POST with `applicationId`  → ADOPTS the account SP, applicationId PRESERVED

Passing `applicationId` is the whole ballgame: omit it and the target mints a NEW applicationId,
silently orphaning every ACL, job `run_as` and secret grant that referenced the SP. That is what
produced 13 duplicate SPs at account level. With it, `sp_mapping` is an IDENTITY map.

**Groups are the one type where the decision is real, and getting it wrong is unrecoverable.**
    kind=workspace_local → POST workspace SCIM (created EMPTY, members in pass 2)
    kind=account         → ASSIGN via permissionassignments; NEVER POST
    kind=system          → never create; membership only
POSTing an account group creates a workspace-local SHADOW with the same name, and that shadow then
permanently blocks assigning the real account group ("Workspace group with name X already exists").
See `_process_one`, where the guard has to live — a shadow IS in `existing_keys()`, so the base class
would otherwise silently ADOPT it.

**Multi-workspace safety.** One account is shared by 100+ workspace pairs, so this importer is
strictly ADDITIVE at account level: it never deletes an account principal, never patches an account
group's (account-global) membership, and never replays `account_admin`. Only workspace-scoped
entitlements and assignments are written.

**Why the id map is written per identity, immediately.** Every later permission call resolves a
principal by its TARGET SCIM id, which differs from the source id even when the natural key doesn't.
Hence a row per identity as it happens, plus the mandatory phase-boundary flush, and a row even for
identities we deliberately do NOT create.

Members and ACL principals are matched **by name** (userName / applicationId / displayName), never
by source id — source ids are meaningless on the target.
"""
from __future__ import annotations

from src.importers.base_importer import BaseImporter, PrerequisiteMissing
from src.state.state_store import (ACTION_ADOPTED, ACTION_CREATED, ACTION_CREATED_WITH_WARNING,
                                   ACTION_FAILED)
from src.utils.helpers import safe_str

# SCIM create whitelists (master §10a). Sending the whole source object back fails or writes
# server-derived junk, so only these fields travel; entitlements and roles are applied as
# SEPARATE PATCH passes after create, because several workspaces reject them inline.
_USER_CREATE_FIELDS = ("userName", "displayName", "emails", "name", "active")
# `applicationId` is LOAD-BEARING, not cosmetic (Plan 6 F4, verified live on target 2026-08-06):
#   POST with it    → adopts the existing account SP, SAME applicationId and SAME SCIM id
#   POST without it → mints a BRAND-NEW applicationId, orphaning every ACL/run_as/secret grant
# Omitting it is what produced 13 duplicate `wsmig_test_db_sp` appIds at account level.
_SP_CREATE_FIELDS = ("displayName", "active", "applicationId")
_GROUP_CREATE_FIELDS = ("displayName",)

_SCIM = "api/2.0/preview/scim/v2"
_ASSIGNMENTS = "api/2.0/preview/permissionassignments"

# Account-level roles must NEVER be replayed: they are account-GLOBAL, so granting one while
# migrating workspace N would escalate that identity across every other workspace in the account.
# Only workspace-scoped entitlements migrate.
_ACCOUNT_LEVEL_ROLES = {"account_admin"}

# Group kinds, as stamped into the bundle by src/identity/classifier.py.
KIND_ACCOUNT = "account"
KIND_WORKSPACE_LOCAL = "workspace_local"
KIND_SYSTEM = "system"

# Bundles exported before Plan 6 carry the OLD classification vocabulary and no `kind`. Mapping them
# forward keeps an old bundle importable instead of degrading every group to NEEDS_REVIEW (which
# would skip them all). `db_managed_group` maps to workspace_local because that is what it meant.
_LEGACY_KIND = {
    "account_group": KIND_ACCOUNT, "entra_user": KIND_ACCOUNT, "umi_or_entra_sp": KIND_ACCOUNT,
    "db_managed_sp": KIND_ACCOUNT,          # an SP is ALWAYS an account principal (Plan 6 F3)
    "db_managed_group": KIND_WORKSPACE_LOCAL,
    "builtin_group": KIND_SYSTEM,
}


def _kind_of(unit: dict) -> str:
    """The unit's kind, falling back to the legacy `classification` for pre-Plan-6 bundles."""
    kind = safe_str(unit.get("kind"))
    if kind in (KIND_ACCOUNT, KIND_WORKSPACE_LOCAL, KIND_SYSTEM):
        return kind
    return _LEGACY_KIND.get(safe_str(unit.get("classification")), kind)


class IdentityImporter(BaseImporter):
    component = "identity"
    asset_types = ("user", "service_principal", "group", "group_membership")
    # Built-in group membership is declarative against a group that ALWAYS exists on target, so an
    # ADOPT must still perform the member PATCH — otherwise a source workspace admin silently
    # would not be an admin on target.
    declarative_asset_types = ("group_membership",)

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        # name → target SCIM id, filled as we go and consumed by the member PATCH pass.
        self._target_users: dict = {}
        self._target_sps: dict = {}
        self._target_groups: dict = {}
        # name → lowercased meta.resourceType on TARGET. Needed to detect the shadow case: a
        # workspace-local group already occupying an account group's name (Plan 6 F6).
        self._target_group_kinds: dict = {}
        # displayName → target SCIM id, for users AND SPs. SCIM group members are identified by
        # `display` (a person's display name), not by userName/applicationId, so name-based member
        # resolution needs this index as well as the natural-key ones.
        self._target_by_display: dict = {}
        # principal_id → permissions already on target; None = could not read (never {}).
        self._target_assignments = None
        # PASS 2 work list: (group_name, target_group_id, members, unit, result_row). Membership is
        # deliberately NOT patched during a group's own create — see `run()`.
        self._member_pass: list[tuple] = []

    # ── load: the phase's fixed internal order ────────────────────────────
    def load(self) -> list[dict]:
        """Users → SPs → groups → built-in group membership.

        `group_membership` units come last because they PATCH members onto groups that must already
        exist (the built-in `admins`/`users`, which are never created).
        """
        return self.units_for("user", "service_principal", "group", "group_membership")

    # ── existence checks (paginated — a truncated list would DUPLICATE) ───
    def existing_keys(self) -> dict:
        """`{natural_key: target_scim_id}` across all three SCIM types.

        Uses `get_scim`, which paginates via startIndex/count. That matters more here than anywhere:
        a bare list that silently truncated would report an existing user as absent and create a
        duplicate — and SCIM pagination is a known latent bug in the reference `databrickslabs/
        migrate` tool, so it is implemented and asserted rather than assumed.
        """
        for user in self.client.get_scim("Users"):
            key = safe_str(user.get("userName"))
            if key:
                self._target_users[key] = safe_str(user.get("id"))
            # SCIM group members identify each member by `display`, which for a USER is the DISPLAY
            # NAME ("Aman Bansal"), not the userName. Without this index every member of `admins`
            # and `users` failed to resolve and those groups imported EMPTY — i.e. a source
            # workspace admin silently was not an admin on target.
            display = safe_str(user.get("displayName"))
            if display:
                self._target_by_display[display] = safe_str(user.get("id"))
        for sp in self.client.get_scim("ServicePrincipals"):
            key = safe_str(sp.get("applicationId"))
            if key:
                self._target_sps[key] = safe_str(sp.get("id"))
            display = safe_str(sp.get("displayName"))
            if display:
                self._target_by_display[display] = safe_str(sp.get("id"))
        for group in self.client.get_scim("Groups"):
            key = safe_str(group.get("displayName"))
            if key:
                self._target_groups[key] = safe_str(group.get("id"))
                # Recorded so an ACCOUNT group can detect a same-named workspace-local SHADOW
                # already sitting on target, which would block its assignment forever (F6).
                self._target_group_kinds[key] = safe_str(
                    (group.get("meta") or {}).get("resourceType")).lower()

        # Existing workspace permissions, so the assignment pass only PUTs real deltas. None (not
        # {}) when unreadable — {} would look like "nobody is assigned" and re-PUT everything.
        self._target_assignments = self._read_target_assignments()

        # Publish for later phases (job run_as, secret-scope MANAGE, ACL principals).
        self.context.setdefault("target_identities", {}).update({
            "users": self._target_users, "service_principals": self._target_sps,
            "groups": self._target_groups})

        # One flat map for the base class's existence check. A user and a group can share a name in
        # principle; that only risks a spurious ADOPT, and the per-type maps above are what the
        # create paths actually use, so the flat view is safe for its one purpose.
        out: dict = {}
        out.update(self._target_users)
        out.update(self._target_sps)
        out.update(self._target_groups)
        return out

    def _read_target_assignments(self):
        """`{principal_id: [permissions]}` on target, or None if it could not be read."""
        try:
            data = self.client.get(_ASSIGNMENTS)
        except Exception as exc:  # noqa: BLE001
            self.log.warning("could not read target workspace permission assignments; the "
                             "assignment pass will PUT unconditionally", error=str(exc)[:200])
            return None
        out: dict = {}
        for entry in (data or {}).get("permission_assignments", []) or []:
            principal = entry.get("principal") or {}
            pid = safe_str(principal.get("principal_id"))
            if pid:
                out[pid] = [p for p in (entry.get("permissions") or []) if p]
        return out

    # ── create / update ───────────────────────────────────────────────────
    def create_one(self, unit: dict) -> dict:
        asset_type = safe_str(unit.get("asset_type"))
        if asset_type == "user":
            return self._create_user(unit)
        if asset_type == "service_principal":
            return self._create_sp(unit)
        if asset_type == "group":
            return self._create_group(unit)
        if asset_type == "group_membership":
            return self._add_builtin_members(unit)
        raise RuntimeError(f"identity importer got an unexpected asset_type {asset_type!r}")

    def update_one(self, unit: dict, target_id: str) -> dict:
        """Re-apply the parts of an identity that CAN change: entitlements, roles, membership.

        Deliberately not a wholesale SCIM PUT: that would fight Entra/SCIM provisioning on an
        account-managed identity, and on a group it would drop members added on target by hand.
        """
        asset_type = safe_str(unit.get("asset_type"))
        payload = unit.get("payload") or {}
        if asset_type in ("user", "service_principal"):
            resource = "Users" if asset_type == "user" else "ServicePrincipals"
            self._apply_entitlements(resource, target_id, payload)
            return {"target_id": target_id, "note": "entitlements/roles re-applied"}
        if asset_type == "group":
            self._apply_entitlements("Groups", target_id, payload)
            if _kind_of(unit) == KIND_ACCOUNT:
                # Never re-sync an account group's members: account-global (§multi-workspace) and
                # Entra would revert it. Permissions ARE re-checked, since ADMIN/USER can change.
                self._ensure_assignment(target_id, unit, self.natural_key(unit))
                return {"target_id": target_id,
                        "note": "entitlements + workspace permissions re-applied; members are "
                                "account-owned and were not modified"}
            note = self._sync_members(target_id, payload.get("members") or [])
            return {"target_id": target_id, "note": f"entitlements re-applied; {note}",
                    "warning": note if "could not resolve" in note else ""}
        if asset_type == "group_membership":
            return self._add_builtin_members(unit)
        return {"target_id": target_id}

    # ── users ─────────────────────────────────────────────────────────────
    def _create_user(self, unit: dict) -> dict:
        """POST workspace SCIM — which ADOPTS the account user, or creates+assigns it.

        There is no workspace-local user (Plan 6 F3): a workspace-SCIM POST writes at the ACCOUNT
        and assigns to this workspace, returning the account's own id. And it dedupes by `userName`
        (F5), so a user that already exists at the account is adopted, not duplicated — which is
        exactly what makes this safe to run against 100 workspaces sharing one account.

        This is why there is no longer a `PrerequisiteMissing` branch here: the earlier code refused
        to act and asked an account admin to pre-assign every user by hand, which the API makes
        unnecessary.
        """
        payload = unit.get("payload") or {}
        key = self.natural_key(unit)

        body = {k: payload[k] for k in _USER_CREATE_FIELDS if k in payload}
        body["userName"] = key
        created = self.client.post(f"{_SCIM}/Users", body)
        target_id = safe_str(created.get("id"))
        self._target_users[key] = target_id
        self._record_identity_row("user", key, target_id, key, unit, ACTION_CREATED)
        self._apply_entitlements("Users", target_id, payload)
        # Workspace ADMIN vs USER lives only in permissionassignments (F8) — a source workspace
        # admin would otherwise land on target as a plain USER.
        self._ensure_assignment(target_id, unit, key)
        return {"target_id": target_id,
                "note": f"adopted/created the account user `{key}` (userName preserved)"}

    # ── service principals ────────────────────────────────────────────────
    def _create_sp(self, unit: dict) -> dict:
        """POST workspace SCIM WITH the source `applicationId`, which ADOPTS the account SP.

        The `applicationId` in the body is the whole point (Plan 6 F4). Passing it makes the target
        return the SAME applicationId and the SAME account SCIM id as the source; omitting it mints
        a new applicationId, which orphans every ACL, job `run_as` and secret grant that referenced
        the SP — silently, because a fresh SP looks perfectly healthy.

        Consequence: `sp_mapping` is now an IDENTITY map, so SP principals in ACLs need no
        translation. The map row is still recorded because ACL application resolves principals by
        target SCIM id.
        """
        payload = unit.get("payload") or {}
        source_app_id = self.natural_key(unit)

        body = {k: payload[k] for k in _SP_CREATE_FIELDS if k in payload}
        body.setdefault("displayName", source_app_id)
        # Never trust the payload alone: the natural key IS the source applicationId, and adopting
        # the right account SP depends entirely on sending it.
        if source_app_id:
            body["applicationId"] = source_app_id

        try:
            created = self.client.post(f"{_SCIM}/ServicePrincipals", body)
        except Exception as exc:  # noqa: BLE001
            # Already assigned to this workspace (a re-run, or another pair got here first). That is
            # an ADOPT, not a failure — resolve the existing id instead of creating anything.
            if "already exists" not in str(exc).lower():
                raise
            existing_id = self._lookup_sp_id(source_app_id)
            if not existing_id:
                raise
            self._target_sps[source_app_id] = existing_id
            self._record_identity_row("service_principal", source_app_id, existing_id,
                                      source_app_id, unit, ACTION_ADOPTED)
            self._apply_entitlements("ServicePrincipals", existing_id, payload)
            return {"target_id": existing_id,
                    "note": f"adopted the existing account service principal {source_app_id} "
                            f"(applicationId preserved)"}

        target_id = safe_str(created.get("id"))
        new_app_id = safe_str(created.get("applicationId"))
        if new_app_id:
            self._target_sps[new_app_id] = target_id

        self._record_identity_row("service_principal", source_app_id, target_id, new_app_id,
                                  unit, ACTION_CREATED)
        self._apply_entitlements("ServicePrincipals", target_id, payload)
        self._ensure_assignment(target_id, unit, source_app_id)

        warning = ""
        if new_app_id and source_app_id and new_app_id != source_app_id:
            # Should be impossible now that applicationId is sent. If it ever happens, every ACL
            # referencing this SP is silently orphaned, so it must be loud rather than logged.
            warning = (f"applicationId CHANGED ({source_app_id} → {new_app_id}) despite being "
                       f"requested — ACLs, job run_as and secret grants referencing the source SP "
                       f"will NOT resolve on target and must be remapped by hand")
            note = warning
        else:
            note = f"applicationId {new_app_id or source_app_id} preserved from source"

        if safe_str(unit.get("note")):
            # OAuth client secrets are never exportable: the SP exists but cannot authenticate until
            # someone mints a new secret — degraded, not clean.
            warning = (f"{note}. OAuth client secret(s) existed on the SOURCE SP and CANNOT be "
                       f"migrated (no API returns a secret) — create a new secret on target and "
                       f"update whatever authenticates with it")
        return {"target_id": target_id, "note": note, "warning": warning}

    def _lookup_sp_id(self, app_id: str) -> str:
        """Target SCIM id for an applicationId, re-reading SCIM (the local map may predate it)."""
        if not app_id:
            return ""
        if app_id in self._target_sps:
            return self._target_sps[app_id]
        for sp in self.client.get_scim("ServicePrincipals"):
            if safe_str(sp.get("applicationId")) == app_id:
                return safe_str(sp.get("id"))
        return ""

    # ── groups (two-pass) ─────────────────────────────────────────────────
    def _create_group(self, unit: dict) -> dict:
        """Groups are the ONE identity type where create-vs-assign is a real decision.

        `WORKSPACE_LOCAL` → POST workspace SCIM (created empty; members in pass 2).
        `ACCOUNT`         → resolve the account group and ASSIGN it. **Never POST.**

        Why POSTing an account group is the worst failure in this importer (Plan 6 F6, verified live
        on target 2026-08-06): the POST succeeds and creates a workspace-local group with the SAME
        NAME — a shadow. From then on, assigning the real account group fails forever with
        "Workspace group with name X already exists", and nothing in the report looks wrong, because
        a group by that name does exist. Recovery needs a human to delete the shadow.
        """
        payload = unit.get("payload") or {}
        name = self.natural_key(unit)
        kind = _kind_of(unit)

        if kind == KIND_SYSTEM:
            # admins/users exist on every workspace; only their membership migrates.
            return self._add_builtin_members(unit)

        if kind == KIND_ACCOUNT:
            return self._assign_account_group(unit, name)

        if kind != KIND_WORKSPACE_LOCAL:
            # NEEDS_REVIEW (no meta.resourceType in the bundle). Refuse rather than guess: guessing
            # workspace-local risks the permanent shadow above; guessing account silently drops a
            # real group. An operator can resolve it from the inventory report.
            raise PrerequisiteMissing(
                f"group `{name}` has an undetermined kind ({kind or 'missing'}) — the bundle has no "
                f"`meta.resourceType` for it. Creating it could shadow an account group of the same "
                f"name and permanently block assigning the real one, so it is skipped. Re-run "
                f"inventory/export with this version of the tool, then retry.")

        # A workspace-local group whose name is ALREADY taken by an account group on target would
        # 400 anyway; catching it here gives the operator the actual reason.
        body = {k: payload[k] for k in _GROUP_CREATE_FIELDS if k in payload}
        body["displayName"] = name
        created = self.client.post(f"{_SCIM}/Groups", body)
        target_id = safe_str(created.get("id"))
        self._target_groups[name] = target_id
        self._record_identity_row("group", name, target_id, name, unit, ACTION_CREATED)
        self._apply_entitlements("Groups", target_id, payload)

        # Membership is DEFERRED to pass 2, not patched here. Patching now would resolve members
        # against the groups created SO FAR, so a parent listed before its child would silently
        # lose the child — which is the exact failure two-pass exists to prevent.
        self._defer_members(name, target_id, payload.get("members") or [], unit)
        return {"target_id": target_id, "note": "group created empty; members applied in pass 2"}

    def _defer_members(self, name: str, target_id: str, members: list, unit: dict) -> None:
        """Queue a group's membership for pass 2 (after EVERY group exists)."""
        if members:
            self._member_pass.append((name, target_id, members, unit))

    # ── account groups: assign, never create ──────────────────────────────
    def _assign_account_group(self, unit: dict, name: str) -> dict:
        """Assign an existing ACCOUNT group to this workspace via permissionassignments.

        Members are deliberately NOT touched. An account group's membership is account-GLOBAL, so
        patching it while migrating workspace N would alter that same group in every other workspace
        in the account — and for an Entra-backed group, SCIM would revert it anyway. Assignment
        brings the members along automatically (verified live: an assigned account group appeared on
        target already carrying its 2 members).

        Workspace-scoped ENTITLEMENTS are applied, because those are per-workspace and are the
        correct thing to set here.
        """
        payload = unit.get("payload") or {}
        target_id = self._target_groups.get(name, "")

        if target_id:
            # Already visible in target SCIM. If it is a workspace-local group of the same name, the
            # shadow already exists and the account group can never be assigned — surface it.
            if self._target_group_kinds.get(name) == "workspacegroup":
                raise PrerequisiteMissing(
                    f"`{name}` is an ACCOUNT group on the source, but the target already has a "
                    f"WORKSPACE-LOCAL group with the same name. That shadow permanently blocks "
                    f"assigning the account group. Delete the workspace-local group `{name}` on the "
                    f"target, then re-run with retry_mode=failed_only.")
            self._apply_entitlements("Groups", target_id, payload)
            self._ensure_assignment(target_id, unit, name)
            return {"target_id": target_id,
                    "note": "account group already assigned; entitlements re-applied "
                            "(members are account-owned and were not modified)"}

        account_id = self._resolve_account_group_id(unit, name)
        if not account_id:
            entra = bool(unit.get("entra_backed"))
            raise PrerequisiteMissing(
                f"account group `{name}` does not exist in the TARGET account, so it cannot be "
                f"assigned to this workspace. " + (
                    "It is Entra-backed — Entra SCIM provisioning must create it in the target "
                    "account first (customer IT task)." if entra else
                    "An account admin must create it in the target account first.") +
                " Then re-run with retry_mode=failed_only.")

        try:
            self.client.put(f"{_ASSIGNMENTS}/principals/{account_id}",
                            {"permissions": self._permissions_for_unit(unit)})
        except Exception as exc:  # noqa: BLE001
            entra = bool(unit.get("entra_backed"))
            # The id came from the bundle or a name lookup; if the account doesn't have that
            # principal the PUT is where we find out. Translate it into the same actionable
            # prerequisite an up-front miss produces, rather than a raw API error.
            raise PrerequisiteMissing(
                f"account group `{name}` could not be assigned to this workspace (tried account "
                f"principal id {account_id}): {str(exc)[:200]}. " + (
                    "It is Entra-backed — Entra SCIM must provision it into the target account "
                    "first (customer IT task)." if entra else
                    "An account admin must create it in the target account first.") +
                " Then re-run with retry_mode=failed_only.") from exc
        self._target_groups[name] = account_id
        self._record_identity_row("group", name, account_id, name, unit, ACTION_ADOPTED)
        self._apply_entitlements("Groups", account_id, payload)
        return {"target_id": account_id,
                "note": f"assigned the existing account group (id {account_id} preserved); members "
                        f"are account-owned and were not modified"}

    def _resolve_account_group_id(self, unit: dict, name: str) -> str:
        """The TARGET ACCOUNT's id for this group — by displayName, else externalId.

        Both keys resolve to the same account record (Plan 6 F2), so `externalId` is a fallback for
        a group renamed between regions rather than a separate lookup path.

        Enumeration needs account read access. In `airgap` mode with workspace-admin only we cannot
        list the account, so `00_Account_Preflight` writes `account_principal_ids.json` into the
        bundle and it is consulted first — which keeps the air-gap intact while still letting the
        workspace-side PUT do the actual work.
        """
        preresolved = (self.context.get("account_principal_ids") or {}).get("groups") or {}
        for key in (name, safe_str(unit.get("externalId"))):
            if key and safe_str(preresolved.get(key)):
                return safe_str(preresolved[key])

        account_client = self.context.get("account_client")
        if account_client is None:
            # No account credentials. An account group's WORKSPACE SCIM id IS its ACCOUNT id
            # (verified live: `wsmig_acc_mixed_grp` is 152592557989155 in both), so when source and
            # target share an account the exported `source_id` is already the account id — and
            # `permissionassignments` accepts it workspace-side (F7). Probing it is safe: a wrong id
            # simply 404s/400s, which is reported exactly as a missing group would be.
            return safe_str(unit.get("source_id"))
        external_id = safe_str(unit.get("externalId"))
        try:
            for group in account_client.get_scim("Groups"):
                if safe_str(group.get("displayName")) == name:
                    return safe_str(group.get("id"))
                if external_id and safe_str(group.get("externalId")) == external_id:
                    return safe_str(group.get("id"))
        except Exception as exc:  # noqa: BLE001 — degrade to "unresolved", never abort the phase
            self.log.warning("could not enumerate account groups", group=name, error=str(exc)[:200])
        return ""

    def _permissions_for_unit(self, unit: dict) -> list:
        """Exported workspace permissions, defaulting to USER.

        `None` means inventory COULD NOT read permissionassignments (not "no permissions"), so it
        must not be treated as an empty grant — USER is the minimum that makes a principal usable,
        and over-granting ADMIN on a guess would be worse.
        """
        payload = unit.get("payload") or {}
        permissions = payload.get("workspace_permissions")
        if permissions is None:
            permissions = unit.get("workspace_permissions")
        if not permissions:
            return ["USER"]
        return [p for p in permissions if safe_str(p)] or ["USER"]

    def _ensure_assignment(self, principal_id: str, unit: dict, name: str) -> None:
        """PUT the workspace permission if it differs from what the target already has.

        This is the only carrier of workspace ADMIN vs USER (Plan 6 F8) — SCIM entitlements do not
        express it, so without this an admin on source silently lands as a plain USER on target.
        """
        wanted = self._permissions_for_unit(unit)
        if self._target_assignments is not None:
            current = self._target_assignments.get(safe_str(principal_id))
            if current is not None and sorted(current) == sorted(wanted):
                return
        try:
            self.client.put(f"{_ASSIGNMENTS}/principals/{principal_id}", {"permissions": wanted})
        except Exception as exc:  # noqa: BLE001 — the identity exists; don't fail it over this
            self.log.warning("could not set workspace permissions", principal=name,
                             error=str(exc)[:200])
            self.result.warnings.append(
                f"group/{name}: could not set workspace permissions {wanted}: {str(exc)[:160]}")

    def run(self):
        """Pass 1 (create/adopt every identity) → PASS 2 (apply group membership).

        The second pass is what makes membership order-independent: by the time it runs, every user,
        SP and group exists on target, so a parent group listed before its child still resolves the
        child. Doing it inside each group's create would resolve against only the groups made so far
        and silently under-populate any group whose members come later in the bundle.
        """
        result = super().run()
        self._apply_deferred_members()
        self.flush_checkpoint()
        return result

    def _apply_deferred_members(self) -> None:
        """PASS 2: patch every deferred group's members, now that all identities exist."""
        if not self._member_pass:
            return
        self.log.info("group membership pass 2", groups=len(self._member_pass))
        for name, target_id, members, unit in self._member_pass:
            try:
                note = self._sync_members(target_id, members)
            except Exception as exc:  # noqa: BLE001 — fail-soft per group (D21)
                from src.importers.base_importer import classify_error
                category, message = classify_error(exc)
                self._amend_row(name, "created_with_warning",
                                f"group created, but its members could not be applied: {message}",
                                category=category, error_raw=str(exc))
                self.log.warning("group membership failed", group=name, error=str(exc)[:200])
                continue
            # An unresolvable member is a real gap — the group EXISTS but is under-populated — so it
            # is flagged rather than reported clean. `retry_mode=failed_only` picks it up once the
            # missing identity is assigned.
            degraded = "could not resolve" in note
            self._amend_row(name, "created_with_warning" if degraded else "", note,
                            category="prerequisite_missing" if degraded else "")

    def _amend_row(self, natural_key: str, status: str, note: str, *, category: str = "",
                   error_raw: str = "") -> None:
        """Update an already-recorded row's status/note after pass 2.

        Pass 1 recorded the group as `created` before its membership was known; the outcome the
        operator needs is the COMBINED one, so the row is amended in place (report + state) rather
        than a second row being appended for the same unit.
        """
        for row in self.result.units:
            if safe_str(row.get("natural_key")) != natural_key or row.get("asset_type") != "group":
                continue
            old_status = safe_str(row.get("import_status"))
            new_status = status or old_status
            if new_status != old_status:
                # keep the counters honest when a `created` becomes `created_with_warning`
                if old_status in self.result._COUNTER_FOR:
                    attr = self.result._COUNTER_FOR[old_status]
                    setattr(self.result, attr, max(getattr(self.result, attr) - 1, 0))
                if new_status in self.result._COUNTER_FOR:
                    attr = self.result._COUNTER_FOR[new_status]
                    setattr(self.result, attr, getattr(self.result, attr) + 1)
                row["import_status"] = new_status
            row["note"] = note
            if category:
                row["failure_category"] = category
            if new_status == "created_with_warning":
                self.result.warnings.append(f"group/{natural_key}: {note}")
            if self.state is not None:
                self.state.record("group", natural_key, action=new_status,
                                  fingerprint=safe_str(row.get("fingerprint")),
                                  source_object_id=safe_str(row.get("source_id")),
                                  target_object_id=safe_str(row.get("target_id")),
                                  error=note if new_status != "created" else "",
                                  error_raw=error_raw, failure_category=category)
            self._pending_cp_results[natural_key] = {
                "import_status": new_status, "target_id": safe_str(row.get("target_id")),
                "fingerprint": safe_str(row.get("fingerprint")),
                "source_id": safe_str(row.get("source_id")), "note": note}
            if natural_key not in self._pending_cp:
                self._pending_cp.append(natural_key)
            return

    def _sync_members(self, group_id: str, members: list) -> str:
        """PATCH the group's members, resolving each BY NAME on the target.

        Source member ids are meaningless here, so every member is looked up by its
        userName/applicationId/displayName. A member that doesn't exist on target is skipped and
        NAMED — usually an identity that wasn't created (an unassigned account identity). Sending a
        dangling id would fail the whole PATCH and lose the resolvable members too, so partial
        success with an explicit list beats all-or-nothing.
        """
        resolved, unresolved = [], []
        for member in members or []:
            if not isinstance(member, dict):
                continue
            display = safe_str(member.get("display"))
            kind = safe_str(member.get("kind"))
            tid = self._resolve_member(display, kind)
            if tid:
                resolved.append({"value": tid})
            elif display:
                unresolved.append(f"{kind or 'member'}:{display}")

        if resolved:
            self.client.patch(f"{_SCIM}/Groups/{group_id}", {
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [{"op": "add", "path": "members", "value": resolved}],
            })
        parts = [f"{len(resolved)}/{len(members or [])} members added"]
        if unresolved:
            parts.append(f"could not resolve {len(unresolved)} member(s) on target "
                         f"({', '.join(unresolved[:5])}) — they were not created/assigned, so the "
                         f"group stays under-populated until they are")
        return "; ".join(parts)

    def _resolve_member(self, display: str, kind: str) -> str:
        """A member's TARGET SCIM id, by name. Falls back across types when `kind` is unknown."""
        if not display:
            return ""
        maps = {"user": self._target_users, "service_principal": self._target_sps,
                "group": self._target_groups}
        if kind in maps and display in maps[kind]:
            return maps[kind][display]
        # A DB-managed SP was recreated under a NEW appId, so the SOURCE appId won't match any
        # target key — go through the id map, which is precisely what it exists for.
        if kind in ("service_principal", "unknown"):
            mapped = (self.identity_map.get("sp_mapping") or {}).get(display, "")
            if mapped and mapped in self._target_sps:
                return self._target_sps[mapped]
        # Fall back to the DISPLAY-NAME index: SCIM member entries carry `display`, which for a user
        # is their display name rather than their userName, so the per-kind maps above cannot match.
        if display in self._target_by_display:
            return self._target_by_display[display]
        # `kind` is "unknown" when the source $ref was absent — try every type before giving up.
        for m in (self._target_users, self._target_sps, self._target_groups):
            if display in m:
                return m[display]
        return ""

    def _add_builtin_members(self, unit: dict) -> dict:
        """Add members to a group that ALREADY exists (`admins`/`users`), never create it.

        Without this a source workspace admin would silently NOT be an admin on target: the built-in
        group object exists on every workspace, but its membership does not carry over.
        """
        payload = unit.get("payload") or {}
        name = safe_str(payload.get("displayName")) or self.natural_key(unit)
        target_id = self._target_groups.get(name, "")
        if not target_id:
            raise PrerequisiteMissing(
                f"built-in group `{name}` was not found on the target workspace, which should be "
                f"impossible — check that the run-as identity can read SCIM Groups")
        self._record_identity_row("group", name, target_id, name, unit, ACTION_ADOPTED)
        note = self._sync_members(target_id, payload.get("members") or [])
        return {"target_id": target_id, "note": f"members added to the existing group: {note}",
                "warning": note if "could not resolve" in note else ""}

    # ── entitlements + roles (SEPARATE PATCH passes after create) ──────────
    def _apply_entitlements(self, resource: str, target_id: str, payload: dict) -> None:
        """Apply `entitlements` then `roles` as their own PATCHes (master §10a).

        Separate from create because several workspaces reject them inline, and separate from each
        other so one being unsupported doesn't lose the other. Best-effort per PATCH: an entitlement
        that cannot be set must not fail the identity, which by now already exists on target.

        Known, unfixable limitation (inherited from `databrickslabs/migrate`): a role granted BOTH
        directly and via a group is indistinguishable through the API, so only the group grant
        migrates. It surfaces in the ACL parity report rather than being quietly written off.
        """
        for attribute in ("entitlements", "roles"):
            values = payload.get(attribute) or []
            entries = [v if isinstance(v, dict) else {"value": v} for v in values]
            entries = [e for e in entries if e.get("value")]
            if attribute == "roles":
                # Account-level roles are account-GLOBAL. Replaying `account_admin` while migrating
                # workspace N would escalate that identity across every workspace in the account —
                # a privilege escalation, not a migration. Only workspace-scoped grants travel.
                dropped = [e for e in entries
                           if safe_str(e.get("value")).lower() in _ACCOUNT_LEVEL_ROLES]
                entries = [e for e in entries
                           if safe_str(e.get("value")).lower() not in _ACCOUNT_LEVEL_ROLES]
                for entry in dropped:
                    self.log.info("skipped account-level role (account-global, not migrated)",
                                  resource=resource, target_id=target_id,
                                  role=safe_str(entry.get("value")))
            if not entries or not target_id:
                continue
            try:
                self.client.patch(f"{_SCIM}/{resource}/{target_id}", {
                    "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                    "Operations": [{"op": "add", "path": attribute, "value": entries}],
                })
            except Exception as exc:  # noqa: BLE001 — never fail an existing identity over this
                self.log.warning("could not apply identity attribute", attribute=attribute,
                                 resource=resource, target_id=target_id, error=str(exc)[:200])
                self.result.warnings.append(
                    f"{resource}/{target_id}: could not set {attribute} "
                    f"{[e.get('value') for e in entries]}: {str(exc)[:160]}")

    # ── the identity map row ──────────────────────────────────────────────
    def _record_identity_row(self, entity_type: str, source_key: str, target_id: str,
                             target_key: str, unit: dict, action: str) -> None:
        """Write the durable map row NOW, not at the end of the phase (§7.1)."""
        if self.state is None:
            return
        self.state.record_identity(entity_type, source_key, target_id=target_id,
                                   target_key=target_key or source_key,
                                   source_id=safe_str(unit.get("source_id")),
                                   classification=safe_str(unit.get("classification")),
                                   action=action)

    # ── adopt hook: record the map row for identities we DON'T create ─────
    def _process_one(self, unit: dict, existing: dict) -> None:
        """Wrap the base flow so an ADOPT/SKIP also records its identity-map row.

        First, though, the shadow check has to happen HERE rather than only in `_create_group`. A
        workspace-local group squatting an account group's name IS present in `existing_keys`, so the
        base class would classify it ADOPT and never call the create path at all — silently binding
        the migration to the shadow, whose members and ACLs are unrelated to the real account group.
        Catching it before the base flow turns the worst silent failure in this importer into a
        loud, actionable one.

        Not an optimisation — required. An account SP keeps its applicationId but its TARGET SCIM id
        differs, and every later permission call needs that target id. An adopt-without-recording
        would leave every ACL principal remap unable to resolve the principal.
        """
        if (safe_str(unit.get("asset_type")) == "group"
                and _kind_of(unit) == KIND_ACCOUNT
                and self._target_group_kinds.get(self.natural_key(unit)) == "workspacegroup"):
            name = self.natural_key(unit)
            message = (
                f"`{name}` is an ACCOUNT group on the source, but the target already has a "
                f"WORKSPACE-LOCAL group with the same name — a shadow. It permanently blocks "
                f"assigning the account group ('Workspace group with name {name} already exists'), "
                f"and the shadow's members/ACLs are NOT the account group's. Delete the "
                f"workspace-local group `{name}` on the target, then re-run with "
                f"retry_mode=failed_only.")
            self._record(unit, ACTION_FAILED, note=message, error_raw=message,
                         category="prerequisite_missing")
            return

        before = len(self.result.units)
        super()._process_one(unit, existing)
        if len(self.result.units) == before:
            return
        row = self.result.units[-1]
        if safe_str(row.get("natural_key")) != self.natural_key(unit):
            return
        status = safe_str(row.get("import_status"))
        # The create path already wrote its row (with the NEW appId, which this generic path cannot
        # know), and a failure has no target id worth recording.
        if status in (ACTION_CREATED, ACTION_CREATED_WITH_WARNING, ACTION_FAILED):
            return
        target_id = safe_str(row.get("target_id"))
        if not target_id or row.get("dry_run"):
            return
        entity = {"user": "user", "service_principal": "service_principal",
                  "group": "group", "group_membership": "group"}.get(
                      safe_str(unit.get("asset_type")), "")
        if entity:
            self._record_identity_row(entity, self.natural_key(unit), target_id,
                                      self.natural_key(unit), unit, status)

        # An ADOPTed identity still needs its workspace permission applied. Found by the live run:
        # a user already present on target (adopted, never created) kept whatever ADMIN/USER it
        # happened to have, so a source ADMIN silently stayed a plain USER — the exact failure the
        # permissionassignments pass exists to prevent. The create paths call this themselves; this
        # covers every path that does NOT create.
        if safe_str(unit.get("asset_type")) in ("user", "service_principal", "group"):
            self._ensure_assignment(target_id, unit, self.natural_key(unit))
