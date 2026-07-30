"""
HTML report generator. Self-contained HTML (no external CDN), matching the
uc-inventory-migration report style.

Reports produced across stages:
  inventory report   (01) — counts + identity classification + scoping   [IMPLEMENTED]
  transform diff     (03) — pre/post per asset for sign-off              [later plan]
  import results     (04) — created/skipped/failed per asset             [later plan]
  validation report  (05) — source vs target reconciliation             [later plan]
"""
from __future__ import annotations

import html
from typing import Optional


def _esc(s) -> str:
    return html.escape(str(s), quote=True)


_CSS = """
body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;margin:0;background:#f6f7f9;color:#1b1f24}
header{background:#0b3d5c;color:#fff;padding:20px 28px}
header h1{margin:0;font-size:20px}header .sub{opacity:.8;font-size:13px;margin-top:4px}
.wrap{padding:24px 28px;max-width:1100px}
.cards{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:24px}
.card{background:#fff;border:1px solid #e3e6ea;border-radius:8px;padding:14px 18px;min-width:150px}
.card .n{font-size:26px;font-weight:600}.card .l{font-size:12px;color:#5a6472;text-transform:uppercase;letter-spacing:.04em}
h2{font-size:15px;margin:26px 0 10px;border-bottom:2px solid #e3e6ea;padding-bottom:6px}
table{border-collapse:collapse;width:100%;background:#fff;border:1px solid #e3e6ea;border-radius:8px;overflow:hidden}
th,td{text-align:left;padding:8px 12px;font-size:13px;border-bottom:1px solid #eef1f4}
th{background:#f0f3f6;font-weight:600}
.warn{background:#fff7e6;border:1px solid #ffe1a8;border-radius:8px;padding:12px 16px;margin:16px 0;font-size:13px}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600}
.b-ok{background:#e6f4ea;color:#137333}.b-rev{background:#fce8e6;color:#a50e0e}.b-neu{background:#e8eaed;color:#3c4043}
"""


def render_inventory(
    counts: dict,
    collector_stats: list,
    identity_summary: dict,
    warnings: list,
    workspace_url: str,
    generated_at: str,
    output_path: Optional[str] = None,
) -> str:
    """Build the inventory HTML. If `output_path` is given, write it there too; return HTML."""
    cards = "".join(
        f'<div class="card"><div class="n">{_esc(v)}</div><div class="l">{_esc(k)}</div></div>'
        for k, v in sorted(counts.items())
    )

    def _cls_badge(name: str) -> str:
        if name == "needs_review":
            cls = "b-rev"
        elif name in ("entra_user", "umi_or_entra_sp", "account_group", "builtin_group"):
            cls = "b-ok"
        else:
            cls = "b-neu"
        return f'<span class="badge {cls}">{_esc(name)}</span>'

    id_rows = "".join(
        f"<tr><td>{_cls_badge(k)}</td><td>{_esc(v)}</td></tr>"
        for k, v in sorted(identity_summary.items())
    ) or '<tr><td colspan="2">No identities collected</td></tr>'

    stat_rows = "".join(
        f"<tr><td>{_esc(s.get('object_type'))}</td><td>{_esc(s.get('count'))}</td>"
        f"<td>{_esc(s.get('elapsed_sec'))}s</td>"
        f"<td>{_esc('; '.join(s.get('errors', [])) or '—')}</td></tr>"
        for s in collector_stats
    )

    warn_block = ""
    if warnings:
        items = "".join(f"<li>{_esc(w)}</li>" for w in warnings)
        warn_block = f'<div class="warn"><b>Fetch warnings ({len(warnings)})</b><ul>{items}</ul></div>'

    total = sum(counts.values()) if counts else 0
    doc = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Workspace Inventory — {_esc(workspace_url)}</title><style>{_CSS}</style></head><body>
<header><h1>Workspace Migration — Source Inventory</h1>
<div class="sub">{_esc(workspace_url)} &nbsp;•&nbsp; {_esc(generated_at)} &nbsp;•&nbsp; {total:,} resources</div></header>
<div class="wrap">
{warn_block}
<h2>Resource counts</h2><div class="cards">{cards or '<i>none</i>'}</div>
<h2>Identity classification</h2>
<table><tr><th>Classification</th><th>Count</th></tr>{id_rows}</table>
<h2>Collector detail</h2>
<table><tr><th>Collector</th><th>Count</th><th>Elapsed</th><th>Errors</th></tr>{stat_rows}</table>
</div></body></html>"""

    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(doc)
    return doc


def render_transform_diff(before: dict, after: dict, output_path: str) -> str:
    raise NotImplementedError  # Plan 8 (target side)


def render_import_results(results: list, manual_actions: list, output_path: str) -> str:
    raise NotImplementedError  # Plan 8 (target side)


def render_validation(reconciliation: dict, output_path: str) -> str:
    raise NotImplementedError  # Plan 8 (target side)
