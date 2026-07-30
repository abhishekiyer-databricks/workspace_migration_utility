"""
Transforms applied in 03_Transform_Review (on STAGED copies, never the raw export).

STUB ONLY (see PLAN.md §6 "strip", §8). Reused concepts from the reference tool's config.

TODO:
  apply_user_mappings(obj, transform_cfg)   -> remap emails via id map then domain map
  apply_excludes(items, patterns)           -> drop items matching any regex
  pause_schedules(job, transform_cfg)       -> set schedule.pause_status = PAUSED
  strip_runtime(obj, keys)                  -> remove non-importable runtime fields
  remap_references(obj, identity_map)       -> swap old sp/group ids for new ones
"""
from __future__ import annotations


def apply_user_mappings(obj: dict, transform_cfg) -> dict:
    raise NotImplementedError


def apply_excludes(items: list, patterns: list) -> list:
    raise NotImplementedError


def pause_schedules(job: dict, transform_cfg) -> dict:
    raise NotImplementedError


def remap_references(obj: dict, identity_map) -> dict:
    raise NotImplementedError
