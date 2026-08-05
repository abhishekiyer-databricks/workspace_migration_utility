"""
IdentityImporter — phase 1, the highest-risk write in the tool (Plan 3 §6, §7.1; master §7).

Order WITHIN the phase is fixed and load-bearing:
    users → service principals → groups (created EMPTY) → group members → entitlements

**Why groups are two-pass.** A group's members can include other groups, in any order, and forward
references are normal. Creating every group empty first and PATCHing members afterwards makes
membership resolve regardless of order, with no topological sort to get wrong.

**Why classification decides create-vs-assign, and why that matters more than anything else here.**
An account-managed identity (Entra user, Azure UMI / Entra SP, account group) ALREADY EXISTS at the
account level with a stable key. Creating one instead of assigning it mints a **new applicationId**
and orphans every ACL that referenced it. So:

    entra_user / umi_or_entra_sp / account_group / builtin_group → ADOPT (assign + entitle only)
    db_managed_sp / db_managed_group                             → CREATE (new id) → RECORD the map

**Why the id map is written per identity, immediately.** A recreated Databricks-managed SP's new
applicationId has no visible link back to the source appId, so the map CANNOT be rebuilt by
re-reading the target — it is the only record. Losing it means the next run creates a SECOND SP and
every ACL pointing at the first is attached to an orphan. Hence a row per identity as it happens,
plus the mandatory phase-boundary flush, and a row even for identities we deliberately do NOT
create (their TARGET SCIM id differs even when the natural key doesn't, and ACL remap needs it).

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
_SP_CREATE_FIELDS = ("displayName", "active")
_GROUP_CREATE_FIELDS = ("displayName",)

_SCIM = "api/2.0/preview/scim/v2"

# Classifications that must NEVER be created — they exist at the ACCOUNT level with a stable key.
# Creating one mints a new applicationId and orphans every ACL that pointed at it.
_ACCOUNT_MANAGED = {"entra_user", "umi_or_entra_sp", "account_group", "builtin_group"}


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
        for sp in self.client.get_scim("ServicePrincipals"):
            key = safe_str(sp.get("applicationId"))
            if key:
                self._target_sps[key] = safe_str(sp.get("id"))
        for group in self.client.get_scim("Groups"):
            key = safe_str(group.get("displayName"))
            if key:
                self._target_groups[key] = safe_str(group.get("id"))

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
            note = self._sync_members(target_id, payload.get("members") or [])
            return {"target_id": target_id, "note": f"entitlements re-applied; {note}",
                    "warning": note if "could not resolve" in note else ""}
        if asset_type == "group_membership":
            return self._add_builtin_members(unit)
        return {"target_id": target_id}

    # ── users ─────────────────────────────────────────────────────────────
    def _create_user(self, unit: dict) -> dict:
        """Entra users are NEVER created — they are account-level with a stable email.

        If SCIM hasn't provisioned the user into the target workspace yet, that is a customer-IT /
        account-admin prerequisite, reported as such rather than papered over by creating a
        workspace-local user with the same email (which would then diverge from Entra forever).
        """
        payload = unit.get("payload") or {}
        key = self.natural_key(unit)
        classification = safe_str(unit.get("classification"))

        if classification in _ACCOUNT_MANAGED:
            raise PrerequisiteMissing(
                f"`{key}` is an account-managed identity ({classification}) that is not assigned "
                f"to this workspace. It must be provisioned/assigned by Entra SCIM or an account "
                f"admin — creating it here would mint a workspace-local duplicate that diverges "
                f"from Entra. Assign it, then re-run with retry_mode=failed_only.")

        body = {k: payload[k] for k in _USER_CREATE_FIELDS if k in payload}
        body["userName"] = key
        created = self.client.post(f"{_SCIM}/Users", body)
        target_id = safe_str(created.get("id"))
        self._target_users[key] = target_id
        self._record_identity_row("user", key, target_id, key, unit, ACTION_CREATED)
        self._apply_entitlements("Users", target_id, payload)
        return {"target_id": target_id}

    # ── service principals ────────────────────────────────────────────────
    def _create_sp(self, unit: dict) -> dict:
        """Databricks-managed SPs are recreated (new appId → recorded); account SPs are assigned."""
        payload = unit.get("payload") or {}
        source_app_id = self.natural_key(unit)
        classification = safe_str(unit.get("classification"))

        if classification in _ACCOUNT_MANAGED:
            raise PrerequisiteMissing(
                f"service principal `{source_app_id}` ({classification}) is account-managed and "
                f"not assigned to this workspace. An account admin must add it to the target with "
                f"the SAME applicationId — creating it here would mint a NEW applicationId and "
                f"orphan every ACL, job run_as and secret grant that references it.")

        body = {k: payload[k] for k in _SP_CREATE_FIELDS if k in payload}
        body.setdefault("displayName", source_app_id)
        created = self.client.post(f"{_SCIM}/ServicePrincipals", body)
        target_id = safe_str(created.get("id"))
        new_app_id = safe_str(created.get("applicationId"))
        if new_app_id:
            self._target_sps[new_app_id] = target_id

        # THE critical mapping: old appId → NEW appId, written immediately because it cannot be
        # recovered by re-reading the target (the new appId has no link back to the source one).
        self._record_identity_row("service_principal", source_app_id, target_id, new_app_id,
                                  unit, ACTION_CREATED)
        self._apply_entitlements("ServicePrincipals", target_id, payload)

        note = f"recreated with a NEW applicationId {new_app_id} (source was {source_app_id})"
        warning = ""
        if safe_str(unit.get("note")):
            # OAuth client secrets are never exportable: the SP exists but cannot authenticate until
            # someone mints a new secret — degraded, not clean.
            warning = (f"{note}. OAuth client secret(s) existed on the SOURCE SP and CANNOT be "
                       f"migrated (no API returns a secret) — create a new secret on target and "
                       f"update whatever authenticates with it")
        return {"target_id": target_id, "note": note, "warning": warning}

    # ── groups (two-pass) ─────────────────────────────────────────────────
    def _create_group(self, unit: dict) -> dict:
        """Create the group EMPTY, then PATCH members — so nesting resolves in any order."""
        payload = unit.get("payload") or {}
        name = self.natural_key(unit)
        classification = safe_str(unit.get("classification"))

        if classification in _ACCOUNT_MANAGED:
            raise PrerequisiteMissing(
                f"group `{name}` ({classification}) is account-managed and not assigned to this "
                f"workspace. An account admin must assign it; recreating it as a workspace-local "
                f"group would shadow the account group and diverge from SCIM.")

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

        Not an optimisation — required. An account SP keeps its applicationId but its TARGET SCIM id
        differs, and every later permission call needs that target id. An adopt-without-recording
        would leave every ACL principal remap unable to resolve the principal.
        """
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
