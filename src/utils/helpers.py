"""
Shared helpers.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Iterable


def now_iso() -> str:
    """UTC ISO-8601 timestamp (e.g. 2026-07-29T10:11:12.345678Z)."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def now_compact() -> str:
    """Compact UTC stamp for run_ids / filenames: YYYYMMDD_HHMMSS."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d_%H%M%S")


def safe_str(v: Any) -> str:
    """str-coerce; None -> ''."""
    return "" if v is None else str(v)


def strip_fields(d: dict, keys: Iterable[str]) -> dict:
    """Return a shallow copy of dict `d` without any key in `keys`."""
    ks = set(keys)
    return {k: v for k, v in d.items() if k not in ks}


def parse_kv_list(raw: str) -> dict:
    """Parse a widget string like 'a=b, c=d' into {'a': 'b', 'c': 'd'}.

    Blank input -> {}. Whitespace around keys/values is stripped. Entries without '=' are
    ignored. Later duplicates win.
    """
    out: dict = {}
    if not raw:
        return out
    for part in raw.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        if k:
            out[k] = v
    return out


def parse_csv(raw: str) -> list:
    """Parse a widget string like 'x, y ,z' into ['x', 'y', 'z']. Blank input -> []."""
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def parse_bool(raw: Any, default: bool = False) -> bool:
    """Parse a widget/string boolean ('true'/'false', case-insensitive).

    None or blank -> default (so an absent/empty widget keeps its default).
    """
    if isinstance(raw, bool):
        return raw
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("true", "1", "yes", "y")
