"""
Identity classifier — the core enhancement over the reference tool.

Classifies each source identity so the target-side importer knows whether to CREATE
(workspace-local) or merely ASSIGN + entitle (account-managed).

Primary signal = `externalId` (present on Entra/SCIM-provisioned identities, absent on
Databricks-managed / workspace-local ones). Optionally corroborated by an account-level set
of known ids (if the caller can read the account); with workspace-admin only, we trust
`externalId` and flag nothing-but-obvious edge cases as NEEDS_REVIEW.

Categories:
  ENTRA_USER          — account-managed user (stable email); assign + entitle
  UMI_OR_ENTRA_SP     — account-managed SP (stable Azure applicationId); assign by appId
  DB_MANAGED_SP       — workspace-local SP; recreate → new appId → record in map → remap ACLs
  ACCOUNT_GROUP       — account/SCIM group; assign + entitle (no recreate)
  DB_MANAGED_GROUP    — workspace-local group; recreate members/nesting/entitlements
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from src.utils.helpers import safe_str


class IdentityClass(str, Enum):
    ENTRA_USER = "entra_user"
    UMI_OR_ENTRA_SP = "umi_or_entra_sp"
    DB_MANAGED_SP = "db_managed_sp"
    ACCOUNT_GROUP = "account_group"
    DB_MANAGED_GROUP = "db_managed_group"
    BUILTIN_GROUP = "builtin_group"   # users/admins — always exist on target; never recreate
    NEEDS_REVIEW = "needs_review"     # low-confidence; surfaced for operator confirmation
    UNKNOWN = "unknown"


# Built-in workspace groups that exist on every workspace and must NOT be recreated
# (migrate review: `admins` is implicit on target; `users` is the default all-users group).
_BUILTIN_GROUPS = {"admins", "users"}


def _has_external_id(obj: dict) -> bool:
    return bool(safe_str(obj.get("externalId")).strip())


def classify_user(user: dict) -> IdentityClass:
    """Users are Entra-managed in this customer's model. Presence of externalId confirms it;
    absence is unusual for a user, so flag for review rather than assuming workspace-local."""
    return IdentityClass.ENTRA_USER if _has_external_id(user) else IdentityClass.NEEDS_REVIEW


def classify_service_principal(sp: dict, account_app_ids: Optional[set] = None) -> IdentityClass:
    """Azure UMI/Entra SP vs Databricks-managed SP.

    externalId present ⇒ UMI_OR_ENTRA_SP. If an account-level app-id set is supplied, an SP
    whose applicationId is in it is account-managed even if externalId is unset (deterministic).
    Otherwise absence of externalId ⇒ DB_MANAGED_SP (workspace-local, new appId on target).
    """
    if _has_external_id(sp):
        return IdentityClass.UMI_OR_ENTRA_SP
    if account_app_ids is not None and safe_str(sp.get("applicationId")) in account_app_ids:
        return IdentityClass.UMI_OR_ENTRA_SP
    return IdentityClass.DB_MANAGED_SP


def classify_group(group: dict, account_group_names: Optional[set] = None) -> IdentityClass:
    """Built-in vs account/SCIM vs Databricks-managed (workspace-local).

    Built-in `users`/`admins` always exist on the target ⇒ BUILTIN_GROUP (never recreate).
    externalId present ⇒ ACCOUNT_GROUP. If an account-level group-name set is supplied, a
    match confirms ACCOUNT_GROUP deterministically. Otherwise absence ⇒ DB_MANAGED_GROUP
    (workspace-local, recreate on target) — the customer's "groups created inside Databricks".
    """
    if safe_str(group.get("displayName")) in _BUILTIN_GROUPS:
        return IdentityClass.BUILTIN_GROUP
    if _has_external_id(group):
        return IdentityClass.ACCOUNT_GROUP
    if account_group_names is not None and safe_str(group.get("displayName")) in account_group_names:
        return IdentityClass.ACCOUNT_GROUP
    return IdentityClass.DB_MANAGED_GROUP


def classify_identity(obj: dict, *, account_app_ids: Optional[set] = None,
                      account_group_names: Optional[set] = None) -> IdentityClass:
    """Dispatch on `identity_type` (as produced by IdentityCollector)."""
    t = obj.get("identity_type")
    if t == "user":
        return classify_user(obj)
    if t == "service_principal":
        return classify_service_principal(obj, account_app_ids)
    if t == "group":
        return classify_group(obj, account_group_names)
    return IdentityClass.UNKNOWN


def classify_all(identities: list[dict], **kw) -> list[dict]:
    """Annotate each identity dict with a `classification` field (in place) and return it."""
    for obj in identities:
        obj["classification"] = classify_identity(obj, **kw).value
    return identities


def classification_summary(identities: list[dict]) -> dict:
    """Count identities per classification (for the report + go/no-go)."""
    out: dict = {}
    for obj in identities:
        c = obj.get("classification", IdentityClass.UNKNOWN.value)
        out[c] = out.get(c, 0) + 1
    return out

