"""
BaseImporter — abstract interface all target-writing importers implement.

STUB ONLY (see PLAN.md §2, §8). The write-side mirror of BaseCollector:
load → skip-if-exists → create, with dry-run, checkpointing, and per-importer stats.
An importer failure must NEVER stop the pipeline; it is recorded and the run continues.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


class ImportResult:
    """Per-asset counters: total / created / skipped / failed / dry_run + errors/warnings."""

    def __init__(self, component: str) -> None:
        self.component = component
        self.total = 0
        self.created = 0
        self.skipped = 0
        self.failed = 0
        self.dry_run = 0
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def as_dict(self) -> dict:
        raise NotImplementedError


class BaseImporter(ABC):
    component: str = "unknown"

    def __init__(self, target_client, config, staging, identity_map=None, dbutils=None) -> None:
        self.client = target_client   # auth.ApiClient bound to TARGET
        self.config = config
        self.staging = staging        # exporters.ArtifactWriter (read staged JSON, checkpoint)
        self.identity_map = identity_map
        self.dbutils = dbutils

    @abstractmethod
    def load(self) -> list[dict]:
        """Read staged export JSON for this asset from the run dir."""

    @abstractmethod
    def existing_keys(self) -> set:
        """Return identifier set already present on target (for skip-if-exists)."""

    @abstractmethod
    def create_one(self, item: dict) -> dict:
        """Create a single asset on the target (respecting dry_run)."""

    def run(self) -> ImportResult:
        """load → for each item: skip-if-exists / checkpoint / create_one, collecting results.

        TODO: honour config.dry_run; update checkpoint per item; never raise.
        """
        raise NotImplementedError
