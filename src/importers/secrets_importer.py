"""
SecretsImporter — phase 4: secret scopes (+ their MANAGE principal) (Plan 3 §6, §6c, D4).

**Secret VALUES never migrate, and that is physical rather than a limitation of this tool.** No
Databricks API returns a secret value — that is the entire point of a secret store. So every key gets
a `manual` unit telling the operator which value to re-populate, on EVERY run rather than only the
first (the scope's key-name list IS fingerprinted, so an added key is detected).

Two implementation traps, both verified live (memory `fvm1-test-fixtures-and-akv-state`):

1. **An Azure Key Vault-backed scope needs an AZURE AD token, not a Databricks token.** Only for the
   linking call (`POST secrets/scopes/create` with `scope_backend_type=AZURE_KEYVAULT`): Databricks
   must prove to *Azure* that the caller may read that vault, and a Databricks OAuth/context token
   carries no AAD identity — the call fails with `"must have userAADToken defined!"`. If the run-as
   identity is an Entra SP / managed identity it can mint that token itself, headlessly. Note this is
   SEPARATE from vault permissions: the AAD token is *who is asking*, the vault's access policy is
   *whether they're allowed* — and the failure note distinguishes them, because the customer fixes
   the two differently. On any failure that scope fails, is reported with its remediation, and the
   run CONTINUES (D4).

2. **`initial_manage_principal` must be set AT CREATE.** `users:MANAGE` cannot be patched afterwards —
   getting it wrong means deleting and recreating the scope, so it is resolved through the identity
   map BEFORE the create rather than fixed up later.

**Cross-region reality check, stated rather than buried:** carrying the vault over verbatim means a
region-2 workspace reads a region-1 vault. That works if the target identity is granted access and
the network path allows it, and it is the customer's stated preference — but it leaves a cross-region
dependency, so the note says so plainly (preflight WARNs on it too).
"""
from __future__ import annotations

from src.importers.base_importer import BaseImporter, PrerequisiteMissing
from src.utils.helpers import safe_str

_AKV = "AZURE_KEYVAULT"


class SecretsImporter(BaseImporter):
    component = "secrets"
    asset_types = ("secret_scope", "secret_value")

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self._manage_cache = None      # lazily read from export/acls.json

    def load(self) -> list[dict]:
        """Scopes first, then the per-key `secret_value` units (all `manual`, never attempted)."""
        return self.units_for("secret_scope", "secret_value")

    def existing_keys(self) -> dict:
        """`{scope_name: scope_name}` — a scope's NAME is its identifier.

        `secrets/scopes/list` exposes no pagination. A truncated list here would attempt to recreate
        an existing scope, which fails as RESOURCE_ALREADY_EXISTS and is adopted — so the downside is
        contained rather than becoming a duplicate.
        """
        doc = self.client.get("api/2.0/secrets/scopes/list") or {}
        found = {safe_str(s.get("name")): safe_str(s.get("name"))
                 for s in (doc.get("scopes") or []) if s.get("name")}
        self.context.setdefault("secret_scope_target_ids", {}).update(found)
        return found

    def create_one(self, unit: dict) -> dict:
        if safe_str(unit.get("asset_type")) != "secret_scope":
            # `secret_value` units are `manual` and never reach here — this is a guard, not a path.
            raise PrerequisiteMissing(
                "secret VALUES are never readable through any API, so they cannot be imported — "
                "re-populate this key by hand on the target (≤128 KB per value)")

        payload = unit.get("payload") or {}
        name = self.natural_key(unit)
        backend = safe_str(payload.get("backend_type")).upper() or "DATABRICKS"

        body: dict = {"scope": name}
        manage_principal = self._manage_principal(name)
        if manage_principal:
            # MUST be set at create — `users:MANAGE` cannot be patched later.
            body["initial_manage_principal"] = manage_principal

        if backend == _AKV:
            return self._create_akv_scope(unit, body, payload)

        self.client.post("api/2.0/secrets/scopes/create", body)
        keys = payload.get("key_names") or []
        return {"target_id": name,
                "note": (f"Databricks-backed scope created (MANAGE={manage_principal}). Its "
                         f"{len(keys)} secret VALUE(s) are NOT migratable — no API returns a value — "
                         f"so re-populate them on target; each key has its own manual row.")}

    def _create_akv_scope(self, unit: dict, body: dict, payload: dict) -> dict:
        """Link the scope to the SAME Azure Key Vault, using an AAD token (§6c/D4)."""
        name = self.natural_key(unit)
        meta = payload.get("keyvault_metadata") or {}
        resource_id = safe_str(meta.get("resource_id"))
        dns_name = safe_str(meta.get("dns_name"))
        if not (resource_id and dns_name):
            raise PrerequisiteMissing(
                f"scope `{name}` is Azure Key Vault-backed but the export carries no vault "
                f"resource_id/dns_name, so there is nothing to link it to. Recreate it by hand "
                f"against the correct vault.")

        body = {**body, "scope_backend_type": _AKV,
                "backend_azure_keyvault": {"resource_id": resource_id, "dns_name": dns_name}}

        aad_token = safe_str(self.context.get("aad_token"))
        if not aad_token:
            # Distinguished from a vault-PERMISSION failure on purpose: the customer fixes these two
            # causes differently, and one generic error would send them down the wrong path.
            raise PrerequisiteMissing(
                f"scope `{name}` is Azure Key Vault-backed, which needs an AZURE AD token for app "
                f"2ff814a6-3304-4ab8-85cb-cd0e6f879c1d — a Databricks token cannot make this call "
                f"(the API replies \"must have userAADToken defined!\"). None could be minted: the "
                f"run-as identity must be an Entra SP / Azure managed identity with client "
                f"credentials available to the run. Until then, create this scope by hand against "
                f"vault {dns_name}.")

        try:
            # The AAD token is used for THIS CALL ONLY — everything else uses the Databricks token.
            self._post_with_bearer("api/2.0/secrets/scopes/create", body, aad_token)
        except Exception as exc:  # noqa: BLE001
            raise PrerequisiteMissing(
                f"scope `{name}`: the AAD token was minted, but linking to vault {dns_name} was "
                f"REFUSED ({str(exc)[:200]}). Grant the run-as identity `get` + `list` on that vault "
                f"(an access policy, or the *Key Vault Secrets User* role) — that is a grant on the "
                f"AZURE VAULT, not on the Databricks scope.") from exc

        return {"target_id": name,
                "note": (f"linked to the SOURCE vault {dns_name} verbatim. NOTE this is a "
                         f"CROSS-REGION dependency — a region-2 workspace now reads a region-1 "
                         f"vault. That is the intended behaviour, but the target identity needs read "
                         f"access to it and the network path must allow it.")}

    def _post_with_bearer(self, path: str, body: dict, token: str):
        """POST with a one-off Authorization header, leaving the shared client untouched.

        Swapping the client's own token provider would leak the AAD token into every later call, so
        the override is scoped to this single request.
        """
        import requests
        url = f"{self.client.base_url}/{path.lstrip('/')}"
        resp = requests.post(url, headers={"Authorization": f"Bearer {token}",
                                           "Content-Type": "application/json"},
                             json=body, timeout=60)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json() if resp.text else {}

    def _source_manage_grants(self) -> dict:
        """`{scope_name: [principal, ...]}` holding MANAGE on source, read from `export/acls.json`.

        Read from the bundle rather than taken from cross-phase context, because the ACL phase runs
        LAST (phase 12) and this is needed at scope-create time in phase 4 — `users:MANAGE` cannot be
        patched afterwards, so waiting for the ACL phase would be too late by design.
        """
        if self._manage_cache is not None:
            return self._manage_cache
        cache: dict = {}
        for entry in (self.staging.read_json("export/acls.json") or []):
            if not isinstance(entry, dict) or safe_str(entry.get("asset_type")) != "secret_scope":
                continue
            scope = safe_str(entry.get("natural_key"))
            for grant in entry.get("grants") or []:
                if safe_str((grant or {}).get("permission_level")).upper() == "MANAGE":
                    cache.setdefault(scope, []).append(safe_str(grant.get("principal")))
        self._manage_cache = cache
        return cache

    def _manage_principal(self, scope: str) -> str:
        """The `initial_manage_principal` for a new scope, remapped to a TARGET principal.

        MUST be right first time: `users:MANAGE` cannot be patched later, so getting it wrong means
        deleting and recreating the scope. A specific principal is resolved through the identity map,
        because a DB-managed SP's appId changed on target and a stale one would leave the scope
        manageable by nobody but the run-as identity.
        """
        for principal in self._source_manage_grants().get(scope, []):
            if principal == "users":
                return "users"
            mapped = self.resolve_principal(principal)
            if mapped:
                return mapped
        # Default to `users`, matching the API's own default — better than a scope only the run-as
        # SP can manage.
        return "users"

    def update_one(self, unit: dict, target_id: str) -> dict:
        """A secret scope has NO edit API, and recreating it would destroy its values."""
        return {"target_id": target_id or self.natural_key(unit),
                "note": ("the scope changed on source (a key added/removed, or the vault "
                         "re-pointed) but the Secrets API has no scope EDIT call. The scope was left "
                         "as it is, because recreating it would DELETE every value stored in it. "
                         "Reconcile by hand, or deliberately delete and re-import the scope."),
                "warning": ("scope definition changed on source but cannot be updated in place (no "
                            "edit API exists) — reconcile manually")}
