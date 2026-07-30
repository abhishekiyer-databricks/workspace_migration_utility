"""
state_store — TARGET-side Delta state table for incremental, repeatable migrations.

STUB ONLY (see PLAN_0_master.md §9). No implementation yet.

The utility migrates the same workspace MULTIPLE times (initial run, then re-runs weeks/months
later to carry over source changes). Plain skip-if-exists would silently DROP updates to assets
that already exist on target. So every asset is UPSERTed, driven by this state store.

Delta table on the TARGET (target holds the UC catalog; keeps the air-gap intact — the source
never needs target state). One row per migrated object:
    key:  (source_ws_id, asset_type, natural_key)
    cols: source_object_id, target_object_id, last_source_fingerprint, last_run_id,
          first_seen, last_seen, last_action (created|updated|skipped|failed|deleted_in_source)
Storing BOTH ids is essential: e.g. source policy "p1" (src id 3) → target id 9. On a re-run
where "p1" changed on source, we look up the row by natural_key and call the edit API against
`target_object_id=9` — updating the right object, not creating a duplicate.
Also persists the identity map (sp_mapping/group_map) so re-runs reuse previously-created
Databricks-managed SPs/groups instead of creating duplicates.

Import decision per asset (see §9 table):
    no state & not on target        -> CREATE
    no state & already on target     -> ADOPT (record; then compare fingerprint)
    state & fingerprint unchanged    -> SKIP
    state & fingerprint changed      -> UPDATE (asset's edit API)
    state & missing on source now    -> REPORT deleted_in_source (no auto-delete by default)
"""
from __future__ import annotations

from enum import Enum
from typing import Optional


class UpsertAction(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    SKIP = "skip"
    ADOPT = "adopt"
    DELETED_IN_SOURCE = "deleted_in_source"


class StateStore:
    def __init__(self, spark, config, table_fqn: str) -> None:
        self.spark = spark
        self.config = config
        self.table_fqn = table_fqn      # e.g. <staging_catalog>.<schema>.wsmig_state

    def ensure_table(self) -> None:
        """Create the Delta state table if absent. TODO."""
        raise NotImplementedError

    def decide(self, asset_type: str, natural_key: str, fingerprint: str,
               exists_on_target: bool) -> UpsertAction:
        """Return the upsert action for one asset given prior state + current fingerprint. TODO."""
        raise NotImplementedError

    def record(self, asset_type: str, natural_key: str, fingerprint: str,
               source_object_id: str, target_object_id: str,
               action: UpsertAction, run_id: str) -> None:
        """Upsert the state row after an import action, storing BOTH source and target ids
        (target_object_id is what the UPDATE path targets on a later run). TODO."""
        raise NotImplementedError

    def get_target_id(self, asset_type: str, natural_key: str) -> Optional[str]:
        """Return the stored target_object_id for a source asset (used by the UPDATE path). TODO."""
        raise NotImplementedError

    def mark_missing_in_source(self, asset_type: str, present_keys: set, run_id: str) -> list:
        """Flag state rows whose natural_key is no longer in source → deleted_in_source. TODO."""
        raise NotImplementedError

    # ── persistent identity map (across runs) ─────────────────────────────
    def load_identity_map(self) -> dict:
        raise NotImplementedError

    def save_identity_map(self, identity_map: dict) -> None:
        raise NotImplementedError
