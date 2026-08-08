"""
BaseImporter — the write-side mirror of BaseCollector, and the home of the fail-soft contract.

THE INVARIANT THIS FILE ENFORCES (D21, §7d(1a)):

    Every importer is FAIL-SOFT PER UNIT. One asset's failure — for ANY reason: missing
    prerequisite, API rejection, permission denied, unresolved dependency, unsupported operation —
    becomes that unit's `failed` row + report line, and NEVER propagates. The job's exit status
    reflects *the run completing*, not *every unit succeeding*.

Only four WHOLE-RUN preconditions abort, all before any unit is attempted, and all cases where
continuing would produce a *wrong* target rather than an incomplete one (bad manifest, preflight
NO-GO, unreachable state schema when live, unauthenticatable source client). Those are the
runner's business, not an importer's.

The per-unit flow (`run()`), which no subclass overrides:

    load units → for each: is it selected / retryable? → decide (state row + LIVE existence check)
              → dry-run or act → record outcome in the state store + the checkpoint → next unit

Two independent guards make this idempotent (§4), which is why a re-run converges whether or not
the checkpoint survived:
  1. the state-table decision (create/update/skip/adopt by fingerprint), and
  2. a LIVE existence check by natural key on the target, which catches objects created outside the
     tool or by an attempt that died between the API call and the bookkeeping write → ADOPT rather
     than duplicate. `RESOURCE_ALREADY_EXISTS` from any create is caught and downgraded to an adopt
     too, since a race between the check and the create is possible.

Subclasses implement the small asset-specific parts: `load()`, `existing_keys()`, `create_one()`,
and optionally `update_one()`. Everything else — ordering, dry-run, checkpointing, state,
error classification, reporting — lives here so it is written once and behaves the same everywhere.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any, Optional

from src.state.state_store import (ACTION_ADOPTED, ACTION_CREATED, ACTION_CREATED_WITH_WARNING,
                                   ACTION_FAILED, ACTION_MANUAL, ACTION_NOT_SELECTED,
                                   ACTION_SKIPPED, ACTION_SKIPPED_NO_OBJECT, ACTION_UPDATED,
                                   CAT_API_ERROR, CAT_DEPENDENCY_UNRESOLVED, CAT_NOT_SUPPORTED,
                                   CAT_PERMISSION_DENIED, CAT_PREREQUISITE_MISSING, UpsertAction)
from src.utils.helpers import safe_str
from src.utils.logger import get_logger

# Flush the checkpoint every N units. Every write to a UC Volume is a FULL-file rewrite (verified
# live — memory `uc-volume-file-io-limits`), so per-item flushing is O(n²) bytes; one flush at the
# end would lose all bookkeeping on a crash. Same batch size and rationale as the export pass.
CHECKPOINT_BATCH = 200

# API error text → (failure_category, remediation HINT). The hint is only ever APPENDED to the
# actual server error, never a replacement for it: the operator must always see what the target
# really said (a canned string that guesses the cause — e.g. "needs workspace-admin" when the real
# reason was a missing UC table — sends people down the wrong path). Matching only picks the retry
# BUCKET (category) and adds guidance; the raw message is surfaced verbatim by `classify_error`.
# Matched case-insensitively as substrings, first match wins.
_ERROR_MAP: tuple[tuple[str, str, str], ...] = (
    ("RESOURCE_DOES_NOT_EXIST", CAT_DEPENDENCY_UNRESOLVED,
     "hint: a referenced object does not exist on target — most often an unrecreated Git folder "
     "or a notebook path that was never imported. Recreate the dependency, then re-run with "
     "retry_mode=failed_only"),
    ("PERMISSION_DENIED", CAT_PERMISSION_DENIED,
     "hint: this is usually workspace-admin on the target, but can also be a referenced object "
     "(warehouse / UC table) the identity cannot see — read the server message above to tell "
     "which"),
    ("must have userAADToken", CAT_PREREQUISITE_MISSING,
     "hint: linking an Azure Key Vault-backed secret scope needs an AZURE AD token, not a "
     "Databricks token — the run-as identity must be an Entra SP / managed identity (Plan 3 §6c)"),
    ("FEATURE_DISABLED", CAT_NOT_SUPPORTED,
     "hint: the feature is not enabled on the target workspace — enable it or accept the gap"),
    ("QUOTA_EXCEEDED", CAT_PREREQUISITE_MISSING,
     "hint: a target-region quota was exceeded — raise the quota, then retry with "
     "retry_mode=failed_only"),
    ("INVALID_PARAMETER_VALUE", CAT_API_ERROR,
     "hint: the target rejected a field in the payload — the offending field is named above"),
    ("TABLE_OR_VIEW_NOT_FOUND", CAT_PREREQUISITE_MISSING,
     "hint: the payload references a Unity Catalog table that does not exist on target. UC is OUT "
     "OF SCOPE for this utility, so the table must be created by the UC migration before this "
     "asset will work"),
    ("does not exist", CAT_DEPENDENCY_UNRESOLVED,
     "hint: a referenced object does not exist on target — check that the prerequisite family was "
     "imported first"),
)

# Substrings meaning "it's already there" — a create that races the existence check. Treated as an
# ADOPT, never a failure: the object exists, which is the outcome we wanted.
_ALREADY_EXISTS_MARKERS = ("RESOURCE_ALREADY_EXISTS", "already exists", "ALREADY_EXISTS")


class PrerequisiteMissing(RuntimeError):
    """A prerequisite the tool must NOT work around — raised by an importer, not by the API.

    Defined here (rather than per-importer) so `classify_error` can recognise it by type and file it
    as `prerequisite_missing`. That distinction is one the customer asked for: "waiting on customer
    IT / an account admin" and "the API rejected our payload" need different actions, so they must
    not look the same in the report.

    The raiser writes the ACTIONABLE message — which identity to assign, which vault to permission —
    and it is passed through verbatim rather than replaced by a generic one.
    """


class SkippedNoObject(RuntimeError):
    """A declarative apply (an ACL) whose TARGET OBJECT does not exist (§6b-i, D23).

    Deliberately its own status — `skipped_no_object`, NOT `failed`. The object legitimately doesn't
    exist yet, usually BY DESIGN: bundle-owned content the customer's `bundle deploy` recreates, an
    out-of-scope Git repo, or a family deferred by `import_assets`. Filing it as a failure would make
    every bundle-using workspace show permanent red, which is precisely how an operator learns to
    ignore red. It sits in the `skipped_only` retry bucket, which is where "take it up later" belongs.

    `category` carries WHICH of the seven cases applied, so "which permissions are still outstanding,
    and why" is a SQL query rather than a hunt through Excel.
    """

    def __init__(self, message: str, category: str = "") -> None:
        super().__init__(message)
        self.category = category


class UnsupportedOperation(RuntimeError):
    """This asset has no REST create path in scope, so the tool never attempts it.

    Filed as `not_supported` rather than a failure, because attempting it would produce a permanent
    red result on every run forever, which trains the operator to ignore red (D10).
    """


def classify_error(exc: Exception) -> tuple[str, str]:
    """`(failure_category, human_message)` for an error.

    The message ALWAYS carries the actual server error verbatim — never a hardcoded string that
    replaces it. A matched `_ERROR_MAP` entry only adds a remediation HINT (and picks the retry
    bucket); it never hides what the target said. This is deliberate: a canned "needs
    workspace-admin" once masked a missing-UC-table / warehouse-permission genie failure, which is
    exactly the misdiagnosis this avoids. The full raw text is also kept in `error_raw`.
    """
    raw = str(exc).strip()
    # Raiser-authored messages (PrerequisiteMissing / UnsupportedOperation) are already the actual,
    # actionable text — pass through verbatim.
    if isinstance(exc, PrerequisiteMissing):
        return CAT_PREREQUISITE_MISSING, raw
    if isinstance(exc, UnsupportedOperation):
        return CAT_NOT_SUPPORTED, raw
    for marker, category, hint in _ERROR_MAP:
        if marker.lower() in raw.lower():
            # actual server error FIRST, remediation hint appended — the operator sees both.
            return category, f"{raw[:400]}  |  {hint}"
    return CAT_API_ERROR, f"the target API rejected this call: {raw[:400]}"


def is_already_exists(exc: Exception) -> bool:
    raw = str(exc)
    return any(m.lower() in raw.lower() for m in _ALREADY_EXISTS_MARKERS)


class ImportResult:
    """Per-importer counters + the per-unit rows that become `import_results.json`."""

    def __init__(self, component: str) -> None:
        self.component = component
        self.total = 0
        self.created = 0
        self.updated = 0
        self.adopted = 0
        self.skipped = 0
        self.failed = 0
        self.manual = 0
        self.not_selected = 0
        self.skipped_no_object = 0
        self.warned = 0
        self.dry_run = 0
        self.elapsed_sec = 0.0
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.units: list[dict] = []      # one row per unit, joinable on (asset_type, natural_key)

    _COUNTER_FOR = {
        ACTION_CREATED: "created",
        ACTION_UPDATED: "updated",
        ACTION_ADOPTED: "adopted",
        ACTION_SKIPPED: "skipped",
        ACTION_FAILED: "failed",
        ACTION_MANUAL: "manual",
        ACTION_NOT_SELECTED: "not_selected",
        "skipped_no_object": "skipped_no_object",
        ACTION_CREATED_WITH_WARNING: "warned",
    }

    def add(self, row: dict) -> None:
        """Record one unit's outcome and bump the matching counter."""
        self.total += 1
        self.units.append(row)
        counter = self._COUNTER_FOR.get(safe_str(row.get("import_status")))
        if counter:
            setattr(self, counter, getattr(self, counter) + 1)
        if row.get("dry_run"):
            self.dry_run += 1
        if safe_str(row.get("import_status")) == ACTION_FAILED:
            self.errors.append(f"{row.get('asset_type')}/{row.get('natural_key')}: "
                               f"{row.get('note')}")
        elif safe_str(row.get("import_status")) == ACTION_CREATED_WITH_WARNING:
            self.warnings.append(f"{row.get('asset_type')}/{row.get('natural_key')}: "
                                 f"{row.get('note')}")

    def as_dict(self) -> dict:
        return {
            "component": self.component,
            "total": self.total,
            "created": self.created,
            "updated": self.updated,
            "adopted": self.adopted,
            "skipped": self.skipped,
            "failed": self.failed,
            "manual": self.manual,
            "not_selected": self.not_selected,
            "skipped_no_object": self.skipped_no_object,
            "created_with_warning": self.warned,
            "dry_run": self.dry_run,
            "elapsed_sec": round(self.elapsed_sec, 3),
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


class BaseImporter(ABC):
    """Base class for every target-writing importer.

    `component` is the family name (identity/compute/…); `asset_types` are the fine-grained types
    it handles, in the order they must be created WITHIN the phase.
    """

    component: str = "unknown"
    asset_types: tuple = ()
    # asset_types whose work is DECLARATIVE against an object that already exists — adding members
    # to a built-in group, or PUTting an object's permissions. For these, "the object is on target"
    # does NOT mean "the operation was applied", so an ADOPT must still perform the call rather than
    # skip it. Without this, built-in group membership would never migrate (the `admins` group
    # always exists, so every run would adopt and do nothing) and a source admin would silently not
    # be an admin on target. Fingerprint-based SKIP still applies, so a re-run with unchanged
    # membership/grants is still a no-op.
    declarative_asset_types: tuple = ()

    def __init__(self, client, config, staging, state=None, identity_map=None, dbutils=None,
                 context=None, units_by_type=None, retry_keys=None) -> None:
        self.client = client          # auth.ApiClient bound to the TARGET
        self.config = config
        self.staging = staging        # exporters.ArtifactWriter (read the bundle, checkpoint)
        self.state = state            # state.StateStore (may be a disabled no-op store)
        self.identity_map = identity_map or {}
        self.dbutils = dbutils
        # Every unit in the bundle, so an importer can reach a sibling asset_type (e.g. the ACL
        # phase needs to know which objects other phases created).
        self.units_by_type = units_by_type or {}
        # None = "attempt everything"; a set narrows the work list to outstanding units (retry_mode,
        # §7d). Retry narrows the LIST only — each unit still runs the full upsert decision, so a
        # retry can never duplicate an object a previous attempt created but failed to record.
        self.retry_keys = retry_keys
        # Shared cross-phase scratch space: id maps built by earlier phases (cluster name → target
        # id, warehouse name → target id, …). The runner owns it; importers read and contribute.
        self.context = context if context is not None else {}
        self.log = get_logger(self.__class__.__name__)
        self.result = ImportResult(self.component)
        self._pending_cp: list[str] = []
        self._pending_cp_results: dict = {}

    # ── properties ────────────────────────────────────────────────────────
    @property
    def dry_run(self) -> bool:
        return bool(self.config.dry_run)

    # ── the small asset-specific surface subclasses implement ─────────────
    @abstractmethod
    def load(self) -> list[dict]:
        """Units to import, from the bundle, in the order they must be created."""

    def existing_keys(self) -> dict:
        """`{natural_key: target_id}` for objects ALREADY on target, by natural key.

        This is the live existence check — the guard that catches an object created outside the tool
        or by an attempt that died before its bookkeeping write, so it is ADOPTED rather than
        duplicated. Must PAGINATE: a bare list that silently truncates would report an existing
        object as absent and create a DUPLICATE, which is the worst failure mode here.
        """
        return {}

    @abstractmethod
    def create_one(self, unit: dict) -> dict:
        """Create one object on the target. Return `{"target_id": ..., "note": ..., "warning": ...}`.

        Raise on failure — `run()` catches, classifies and records it, then continues.
        """

    def update_one(self, unit: dict, target_id: str) -> dict:
        """Update an existing target object via its EDIT api, against the STORED target id.

        Default: no edit API for this asset → report it rather than pretending it updated.
        """
        return {"target_id": target_id, "warning":
                f"{unit.get('asset_type')} has no update API in this tool — the target object was "
                f"left as it is. Recreate it by hand if the source change matters."}

    # ── unit-level helpers subclasses use ─────────────────────────────────
    @staticmethod
    def natural_key(unit: dict) -> str:
        return safe_str(unit.get("natural_key"))

    def units_for(self, *asset_types: str) -> list[dict]:
        """Bundle units of the given asset_types, in the given order, minus toggled-off ones.

        `export_status == "skip"` means the operator turned that family off at EXPORT time, so
        there is no payload to import — those units are already recorded in the ledger with their
        reason and must not be re-reported here as an import problem.
        """
        out: list[dict] = []
        for at in asset_types:
            for u in self.units_by_type.get(at, []) or []:
                if safe_str(u.get("export_status")) == "skip":
                    continue
                out.append(u)
        return out

    def in_work_list(self, unit: dict) -> bool:
        """Whether `retry_mode` includes this unit (always True when retry_mode=off)."""
        if self.retry_keys is None:
            return True
        return (safe_str(unit.get("asset_type")), self.natural_key(unit)) in self.retry_keys

    def source_id_to_key(self, asset_type: str) -> dict:
        """`{source_object_id: natural_key}` from the bundle, for THIS asset_type.

        The remap chain is always `source id → natural key → target id`, never `source id → target
        id` directly. That indirection is deliberate: the natural key (name/path) is the only thing
        stable across two workspaces, and it lets a reference be resolved against an object imported
        in an EARLIER session (whose target id lives in the state table) just as easily as one
        created in this run.
        """
        out: dict = {}
        for u in self.units_by_type.get(asset_type, []) or []:
            src = safe_str(u.get("source_id"))
            if src:
                out[src] = safe_str(u.get("natural_key"))
        return out

    def target_id_map(self, asset_type: str) -> dict:
        """`{natural_key: target_id}` for an asset_type: this run's creations PLUS earlier sessions.

        Merging both is what makes phase-at-a-time migration work — remapping a job onto a cluster
        imported last week must succeed without re-importing compute.
        """
        combined: dict = {}
        if self.state is not None:
            combined.update(self.state.target_ids_for(asset_type))
        combined.update(self.context.get(f"{asset_type}_target_ids", {}) or {})
        return {k: v for k, v in combined.items() if v}

    def remap_id(self, asset_type: str, source_id: str) -> tuple[str, str]:
        """Resolve a SOURCE object id to its TARGET id. Returns `(target_id, natural_key)`.

        `("", key)` means "we know what it was called but it isn't on target"; `("", "")` means the
        source id isn't in the bundle at all. Callers distinguish these because the remediation
        differs: import the missing family, versus investigate a reference to something that was
        never exported.
        """
        src = safe_str(source_id)
        if not src:
            return "", ""
        key = self.source_id_to_key(asset_type).get(src, "")
        if not key:
            return "", ""
        return safe_str(self.target_id_map(asset_type).get(key, "")), key

    def resolve_principal(self, principal: str, principal_type: str = "") -> str:
        """Map a SOURCE principal (email / appId / group name) to its TARGET equivalent.

        Matched BY NAME, never by source id (master §10a): emails and group displayNames are
        stable, and a recreated Databricks-managed SP's appId is looked up in `sp_mapping`. An
        unknown principal is returned unchanged — the caller decides whether that's fatal, since
        for most greenfield targets it means "an account identity we didn't create".
        """
        p = safe_str(principal)
        if not p:
            return p
        if principal_type == "service_principal" or p in (self.identity_map.get("sp_mapping") or {}):
            return (self.identity_map.get("sp_mapping") or {}).get(p, p)
        if principal_type == "user":
            return (self.identity_map.get("user_map") or {}).get(p, p)
        return p

    # ── the run loop (never raises) ───────────────────────────────────────
    def run(self) -> ImportResult:
        """load → decide → act → record, per unit. NEVER raises (D21).

        A unit-level exception is classified, recorded against that unit, and the loop continues to
        the next unit. Even an unexpected exception in `load()` or `existing_keys()` is contained:
        it becomes a component-level error rather than aborting the phase, because a whole family
        failing to list is still not a reason to abandon the other families.
        """
        t0 = time.time()
        try:
            units = self.load()
        except Exception as exc:  # noqa: BLE001 — a family that can't even load must not abort
            self.log.error("importer load failed", component=self.component, error=str(exc))
            self.result.errors.append(f"{self.component}: load failed: {exc}")
            self.result.elapsed_sec = time.time() - t0
            return self.result

        try:
            existing = self.existing_keys()
        except Exception as exc:  # noqa: BLE001
            # Failing OPEN here would risk duplicates, so treat "cannot list" as an empty map but
            # say so loudly — the create path's RESOURCE_ALREADY_EXISTS adopt is the safety net.
            self.log.warning("existence check failed — relying on ALREADY_EXISTS adopts",
                             component=self.component, error=str(exc))
            self.result.warnings.append(
                f"{self.component}: could not list existing objects ({exc}); duplicate protection "
                f"falls back to RESOURCE_ALREADY_EXISTS handling")
            existing = {}

        self.log.info("importing", component=self.component, units=len(units),
                      already_on_target=len(existing), dry_run=self.dry_run)

        for unit in units:
            try:
                self._process_one(unit, existing)
            except Exception as exc:  # noqa: BLE001 — THE fail-soft guarantee (D21)
                # Nothing a single unit does may end the run. This is the last line of defence:
                # _process_one already handles expected API errors, so reaching here means a bug or
                # an unforeseen shape — still recorded per-unit, still continuing.
                self._record(unit, ACTION_FAILED, note=f"unexpected importer error: {exc}",
                             error_raw=str(exc), category=CAT_API_ERROR)
                self.log.error("unit failed (unexpected)", component=self.component,
                               natural_key=self.natural_key(unit), error=str(exc))

        self.flush_checkpoint()
        self.result.elapsed_sec = time.time() - t0
        self.log.info("phase done", component=self.component, **{
            k: v for k, v in self.result.as_dict().items()
            if k in ("total", "created", "updated", "adopted", "skipped", "failed", "manual")})
        return self.result

    def _process_one(self, unit: dict, existing: dict) -> None:
        """Decide and act on ONE unit. Expected API errors are handled here."""
        key = self.natural_key(unit)
        asset_type = safe_str(unit.get("asset_type"))

        # 0. retry_mode narrowed the work list and this unit isn't in it. Recorded as skipped so
        #    the report still accounts for every unit (a narrowed run must not look like a run in
        #    which these units didn't exist), but nothing is attempted and no state row changes.
        if not self.in_work_list(unit):
            self.result.add({
                "asset_type": asset_type, "natural_key": key, "family": self.component,
                "source_id": safe_str(unit.get("source_id")), "target_id": "",
                "import_status": ACTION_SKIPPED,
                "action_taken": "Not in this retry's work list",
                "fingerprint": safe_str(unit.get("fingerprint")),
                "note": f"retry_mode={self.config.imports.retry_mode} narrowed this run to "
                        f"outstanding units; this one was not outstanding",
                "failure_category": "", "dry_run": self.dry_run})
            return

        # 1. Units the bundle already marked as human work (repos, legacy dashboards, secret
        #    values, oversize notebooks, UC-backed endpoints). Never attempted — attempting
        #    produces a permanent red failure every run, which trains the operator to ignore red.
        if safe_str(unit.get("import_action")) in ("manual", "review_required"):
            self._record(unit, ACTION_MANUAL, note=safe_str(unit.get("note")) or
                         "requires a manual step on target")
            return

        # 2. Bundle-owned content: the customer's `databricks bundle deploy` recreates it, and
        #    importing bundle STATE would point their next deploy at source-workspace object ids
        #    (verified live). Branch on `import_action`, NEVER on `migration_mode` — see D10/§6d.
        if safe_str(unit.get("import_action")) == "dab_redeploy":
            self._record(unit, ACTION_SKIPPED, note=safe_str(unit.get("note")) or
                         "bundle-owned — recreated by `databricks bundle deploy` on the target")
            return

        # 2b. Databricks-generated artifact (e.g. a `users-clone-…` group, IMP-7a): a platform
        #     bookkeeping object that exists only on the source. There is nothing for a human to do
        #     and nothing to create — skip it cleanly rather than flagging it as manual work.
        if safe_str(unit.get("import_action")) == "skip_generated":
            self._record(unit, ACTION_SKIPPED, note=safe_str(unit.get("note")) or
                         "Databricks-generated artifact (identity-federation bookkeeping) — not a "
                         "real customer object; nothing to migrate")
            return

        # 3. Resume: this attempt already did it. The recorded OUTCOME is restored (not just a
        #    done-flag), because import_results.json is written only at the end and so never
        #    exists after a crash — the checkpoint is the only resumable record.
        if not self.config.imports.force_full_import:
            prior = self.staging.get_results(self._cp_component()).get(key)
            if prior and self.staging.is_done(self._cp_component(), key):
                self._record(unit, safe_str(prior.get("import_status")) or ACTION_SKIPPED,
                             target_id=safe_str(prior.get("target_id")),
                             note=safe_str(prior.get("note")) or "already done in this run "
                                                                "(resumed from checkpoint)",
                             checkpoint=False)
                return

        # 4. The upsert decision: state row + LIVE existence check.
        fingerprint = safe_str(unit.get("fingerprint"))
        exists = key in existing
        action = (self.state.decide(asset_type, key, fingerprint, exists)
                  if self.state is not None else
                  (UpsertAction.ADOPT if exists else UpsertAction.CREATE))

        if action is UpsertAction.SKIP:
            self._record(unit, ACTION_SKIPPED, target_id=existing.get(key, ""),
                         note="unchanged since the last import (fingerprint match)")
            return

        if self.dry_run:
            # A dry run makes the real decision and reports it, but mutates nothing — that's what
            # makes a rehearsal's report meaningful rather than a guess.
            intended = {UpsertAction.CREATE: "would CREATE", UpsertAction.UPDATE: "would UPDATE",
                        UpsertAction.ADOPT: "would ADOPT (already on target)"}[action]
            self._record(unit, self._dry_status(action), target_id=existing.get(key, ""),
                         note=f"dry run: {intended}", dry=True)
            return

        if action is UpsertAction.ADOPT:
            target_id = safe_str(existing.get(key))
            # A DECLARATIVE unit's work isn't the object's existence — it's applying members/grants
            # TO that object. Adopting without performing it would silently migrate nothing.
            if asset_type in self.declarative_asset_types:
                self._do_declarative(unit, target_id)
                return
            # Otherwise: adopting isn't the end of it either — the object exists but may be STALE,
            # so compare fingerprints and update if the source has moved on since.
            row = self.state.row(asset_type, key) if self.state is not None else None
            if row and safe_str(row.get("last_source_fingerprint")) not in ("", fingerprint):
                self._do_update(unit, target_id)
            else:
                self._record(unit, ACTION_ADOPTED, target_id=target_id,
                             note="already existed on target — adopted into the migration state "
                                  "(not duplicated)")
            return

        if action is UpsertAction.UPDATE:
            stored = (self.state.get_target_id(asset_type, key) if self.state is not None else "")
            target_id = safe_str(stored) or safe_str(existing.get(key))
            self._do_update(unit, target_id)
            return

        # CREATE
        try:
            out = self.create_one(unit) or {}
        except SkippedNoObject as exc:
            # A declarative unit whose object isn't there — outstanding work, not an error (§6b-i).
            self._record(unit, ACTION_SKIPPED_NO_OBJECT, note=str(exc), category=exc.category)
            return
        except Exception as exc:  # noqa: BLE001
            if is_already_exists(exc):
                # Raced the existence check — the object exists, which is the outcome we wanted.
                self._record(unit, ACTION_ADOPTED, target_id=safe_str(existing.get(key)),
                             note="already existed on target (create raced the existence check) — "
                                  "adopted, not duplicated")
                return
            category, message = classify_error(exc)
            self._record(unit, ACTION_FAILED, note=message, error_raw=str(exc), category=category)
            self.log.warning("create failed", component=self.component, natural_key=key,
                             category=category, error=str(exc)[:300])
            return
        warning = safe_str(out.get("warning"))
        self._record(unit, ACTION_CREATED_WITH_WARNING if warning else ACTION_CREATED,
                     target_id=safe_str(out.get("target_id")),
                     note=warning or safe_str(out.get("note")))

    def _do_declarative(self, unit: dict, target_id: str) -> None:
        """Apply a declarative unit (members / grants) onto an object that already exists.

        Routed through `create_one`, because for these asset_types "create" IS the declarative
        apply — there is no separate object to make. Recorded as `created` when it applies cleanly,
        so the report reads as "we did the work" rather than "we found it already there".
        """
        key = self.natural_key(unit)
        try:
            out = self.create_one(unit) or {}
        except SkippedNoObject as exc:
            self._record(unit, ACTION_SKIPPED_NO_OBJECT, note=str(exc), category=exc.category)
            return
        except Exception as exc:  # noqa: BLE001
            category, message = classify_error(exc)
            self._record(unit, ACTION_FAILED, target_id=target_id, note=message,
                         error_raw=str(exc), category=category)
            self.log.warning("declarative apply failed", component=self.component,
                             natural_key=key, error=str(exc)[:300])
            return
        warning = safe_str(out.get("warning"))
        self._record(unit, ACTION_CREATED_WITH_WARNING if warning else ACTION_CREATED,
                     target_id=safe_str(out.get("target_id")) or target_id,
                     note=warning or safe_str(out.get("note")))

    def _do_update(self, unit: dict, target_id: str) -> None:
        """The UPDATE path — always against the STORED target id, never a source id."""
        key = self.natural_key(unit)
        if not target_id:
            self._record(unit, ACTION_FAILED,
                         note="the source object changed but no target id is recorded, so there is "
                              "nothing to update. Re-run with force_full_import=true to recreate.",
                         category=CAT_DEPENDENCY_UNRESOLVED)
            return
        try:
            out = self.update_one(unit, target_id) or {}
        except Exception as exc:  # noqa: BLE001
            category, message = classify_error(exc)
            self._record(unit, ACTION_FAILED, target_id=target_id, note=message,
                         error_raw=str(exc), category=category)
            self.log.warning("update failed", component=self.component, natural_key=key,
                             target_id=target_id, error=str(exc)[:300])
            return
        warning = safe_str(out.get("warning"))
        self._record(unit, ACTION_CREATED_WITH_WARNING if warning else ACTION_UPDATED,
                     target_id=safe_str(out.get("target_id")) or target_id,
                     note=warning or safe_str(out.get("note")) or
                     "source changed since the last import — updated in place")

    @staticmethod
    def _dry_status(action: UpsertAction) -> str:
        return {UpsertAction.CREATE: ACTION_CREATED, UpsertAction.UPDATE: ACTION_UPDATED,
                UpsertAction.ADOPT: ACTION_ADOPTED}.get(action, ACTION_SKIPPED)

    # ── recording (state table + checkpoint + result rows) ────────────────
    def _cp_component(self) -> str:
        return f"import:{self.component}"

    def _record(self, unit: dict, status: str, *, target_id: str = "", note: str = "",
                error_raw: str = "", category: str = "", dry: bool = False,
                checkpoint: bool = True) -> None:
        """Record one outcome in all three places: state table, checkpoint, result rows."""
        asset_type = safe_str(unit.get("asset_type"))
        key = self.natural_key(unit)
        row = {
            "asset_type": asset_type,
            "natural_key": key,
            "family": self.component,
            "source_id": safe_str(unit.get("source_id")),
            "target_id": safe_str(target_id),
            "import_status": status,
            "action_taken": _ACTION_TAKEN_LABEL.get(status, status),
            "fingerprint": safe_str(unit.get("fingerprint")),
            "note": note,
            # The COMPLETE, untruncated server error — the report shows it verbatim so nothing is
            # ever hidden behind a summarised note (blank for non-error outcomes).
            "error_raw": safe_str(error_raw),
            "failure_category": category,
            "dry_run": bool(dry),
        }
        self.result.add(row)

        if self.state is not None:
            self.state.record(
                asset_type, key, action=status, fingerprint=safe_str(unit.get("fingerprint")),
                source_object_id=safe_str(unit.get("source_id")), target_object_id=safe_str(target_id),
                error=note if status in (ACTION_FAILED, ACTION_CREATED_WITH_WARNING,
                                         ACTION_MANUAL, "skipped_no_object") else "",
                error_raw=error_raw, failure_category=category)

        # The checkpoint stores the OUTCOME, not just a done-key: a resumed unit needs its
        # target_id/status back, and import_results.json (written only at the end) cannot supply
        # them after a crash. Dry runs are deliberately NOT checkpointed — a rehearsal must not
        # make the next real run think the work is done.
        if checkpoint and not dry:
            self._pending_cp.append(key)
            self._pending_cp_results[key] = {"import_status": status, "target_id": safe_str(target_id),
                                             "fingerprint": safe_str(unit.get("fingerprint")),
                                             "source_id": safe_str(unit.get("source_id")),
                                             "note": note}
            if len(self._pending_cp) >= CHECKPOINT_BATCH:
                self.flush_checkpoint()

    def flush_checkpoint(self) -> None:
        """Write the pending checkpoint batch. Called per batch, at phase end, and in `finally`."""
        if not self._pending_cp and not self._pending_cp_results:
            return
        try:
            self.staging.mark_done_bulk(self._cp_component(), self._pending_cp,
                                        self._pending_cp_results)
        except Exception as exc:  # noqa: BLE001 — bookkeeping must not break the run
            self.log.warning("checkpoint flush failed", component=self.component, error=str(exc))
        self._pending_cp, self._pending_cp_results = [], {}


# Human label per status, for the Excel "Action Taken" column. Kept beside the vocabulary so a new
# status can't be added without a label (the report would otherwise render a bare enum).
_ACTION_TAKEN_LABEL = {
    ACTION_CREATED: "Created on target",
    ACTION_CREATED_WITH_WARNING: "Created — with a warning",
    ACTION_UPDATED: "Updated in place",
    ACTION_ADOPTED: "Adopted (already existed)",
    ACTION_SKIPPED: "Skipped — unchanged",
    ACTION_FAILED: "FAILED",
    ACTION_MANUAL: "Manual step required",
    ACTION_NOT_SELECTED: "Deferred — not selected this run",
    "skipped_no_object": "Skipped — target object absent",
}
