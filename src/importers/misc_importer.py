"""
MiscImporter — writes misc to the TARGET workspace.

STUB ONLY (see PLAN.md §6 asset catalog, §2). Tokens / IP access lists / workspace conf (best-effort; some manual).
Implements BaseImporter: load -> skip-if-exists -> create_one, with dry-run + checkpoint.
"""
from __future__ import annotations

from src.importers.base_importer import BaseImporter, ImportResult


class MiscImporter(BaseImporter):
    component = "misc_importer"

    def load(self) -> list[dict]:
        raise NotImplementedError  # TODO: read staged export JSON for this asset

    def existing_keys(self) -> set:
        raise NotImplementedError  # TODO: list existing on target for skip-if-exists

    def create_one(self, item: dict) -> dict:
        raise NotImplementedError  # TODO: strip runtime + remap refs + POST (see PLAN.md §6)
