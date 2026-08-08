"""
import_report — everything `04_Import` outputs (Plan 3 §1a, D16).

Import owns its OWN complete, customer-readable output set — it is not "test now, report later".
What Plan 4 adds is only the cross-stage inventoried→exported→imported join, which needs nothing
new from here because `import_results.json` is deliberately written in the shape Plan 4 joins on:
one row per unit, keyed `(asset_type, natural_key)`.

  • `import_results.json`  — machine-readable per-unit outcome (the join surface)
  • `import_results.html`  — the same, browsable, failures first
  • `import_status.xlsx`   — the operator's artifact: Summary + a sheet per family, with
                             Import Status / Action Taken / Target Id / Note columns
  • `manual_actions.md`    — everything a human must still do, with reasons (APPENDED to export's,
                             not overwritten — the export-side runbook rows still apply)
  • `acl_parity_report.*`  — written by the ACL phase itself (§6b), not here

The .xlsx is rendered to /tmp then byte-copied to the Volume: openpyxl needs seeks, and writing
straight to a FUSE `/Volumes` path corrupts the file (memory `uc-volume-file-io-limits`).
"""
from __future__ import annotations

from datetime import datetime

from src.utils.helpers import now_iso, safe_str
from src.utils.logger import get_logger

_LOG = get_logger("import_report")

# import_status → (label, colour). Failures red, degraded amber, deferred grey — so the report is
# scannable and `skipped_no_object` never reads as an error (it usually isn't one).
_STATUS_STYLE = {
    "created": ("Created", "D1FAE5"),
    "created_with_warning": ("Created (warning)", "FDE68A"),
    "updated": ("Updated", "DBEAFE"),
    "adopted": ("Adopted (pre-existing)", "CFFAFE"),
    "skipped": ("Skipped (unchanged)", "E5E7EB"),
    "failed": ("FAILED", "FEE2E2"),
    "manual": ("Manual step", "FEF3C7"),
    "not_selected": ("Deferred (not selected)", "F1F5F9"),
    "skipped_no_object": ("Skipped (no target object)", "EDE9FE"),
    "deleted_in_source": ("Deleted in source", "FFE4E6"),
    "": ("—", "FFFFFF"),
}

_SUMMARY_ORDER = ("created", "updated", "adopted", "skipped", "created_with_warning", "manual",
                  "not_selected", "skipped_no_object", "failed")

# Fine-grained importer `asset_type` → the inventory/export CARD KEY it belongs to, so the import
# workbook lays out ONE SHEET PER ASSET TYPE exactly like inventory.xlsx / export_status.xlsx
# (IMP-1: the three stages must be structurally identical, not one-sheet-per-family here and
# one-per-type there). Any asset_type absent from this map falls back to its own name as a tab, so
# a newly-added importer type is never silently dropped.
_ASSET_TYPE_TO_CARD = {
    "user": "users", "service_principal": "service_principals",
    "group": "groups", "group_membership": "groups",
    "notebook": "notebooks", "workspace_file": "workspace_files",
    "directory": "notebooks", "repo": "repos",
    "job": "jobs", "cluster": "clusters", "instance_pool": "instance_pools",
    "cluster_policy": "cluster_policies", "cluster_library": "cluster_libraries",
    "global_init_script": "global_init_scripts",
    "sql_warehouse": "sql_warehouses", "legacy_query": "sql_queries",
    "legacy_alert": "sql_alerts", "alert_v2": "sql_alerts",
    "legacy_dashboard": "sql_dashboards",
    "dlt_pipeline": "dlt_pipelines", "lakeview_dashboard": "lakeview_dashboards",
    "genie_space": "genie_spaces", "serving_endpoint": "serving_endpoints",
    "secret_scope": "secret_scopes", "secret_value": "secret_scopes",
    "workspace_conf": "workspace_conf", "acl": "object_permissions",
}


def _card_for_asset_type(asset_type: str) -> str:
    return _ASSET_TYPE_TO_CARD.get(safe_str(asset_type), safe_str(asset_type) or "other")


def _all_rows(results: list) -> list[dict]:
    """Every per-unit row across all phases, failures first then by (asset_type, natural_key)."""
    rows: list[dict] = []
    for res in results:
        rows.extend(res.units)
    rows.sort(key=lambda r: (safe_str(r.get("import_status")) != "failed",
                             safe_str(r.get("asset_type")), safe_str(r.get("natural_key"))))
    return rows


def write_import_reports(aw, config, summary: dict, results: list, context: dict) -> dict:
    """Write every import artifact. Never raises — a reporting failure must not fail the run."""
    rows = _all_rows(results)
    written: dict[str, str] = {}
    # The deleted-in-source finding is discovered by the runner into `context`; fold it into the
    # summary so every renderer below sees one object rather than needing both.
    summary = {**summary, "deleted_in_source": context.get("deleted_in_source", {})}

    payload = {
        "run_id": summary.get("run_id"),
        "source_workspace_id": summary.get("source_workspace_id"),
        "connectivity_mode": summary.get("connectivity_mode"),
        "dry_run": summary.get("dry_run"),
        "run_status": summary.get("run_status"),
        "generated_utc": summary.get("generated_utc") or now_iso(),
        "elapsed_sec": summary.get("elapsed_sec"),
        "totals": summary.get("totals", {}),
        "per_phase": summary.get("per_phase", []),
        "counts_by_status": _counts_by_status(rows),
        "counts_by_asset_type": _counts_by_asset_type(rows),
        "deleted_in_source": context.get("deleted_in_source", {}),
        # The join surface Plan 4 reads: one row per unit, keyed (asset_type, natural_key).
        "units": rows,
    }
    try:
        aw.write_json("import_results.json", payload)
        written["json"] = "import_results.json"
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("import_results.json not written", error=str(exc))

    try:
        aw.write_bytes("import_results.html",
                       _render_html(config, summary, rows).encode("utf-8"))
        written["html"] = "import_results.html"
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("import_results.html not written", error=str(exc))

    try:
        path = aw.write_text_local_then_copy(
            "import_status.xlsx",
            lambda local: _render_xlsx(local, config, summary, rows))
        if path:
            written["xlsx"] = "import_status.xlsx"
    except Exception as exc:  # noqa: BLE001 — Excel is a convenience; never fail the run
        _LOG.warning("import_status.xlsx not written", error=str(exc))

    try:
        aw.write_bytes("manual_actions_import.md",
                       _render_manual_actions(summary, rows).encode("utf-8"))
        written["manual_actions"] = "manual_actions_import.md"
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("manual_actions_import.md not written", error=str(exc))

    _LOG.info("import reports written", **written)
    return written


def _counts_by_status(rows: list[dict]) -> dict:
    out: dict = {}
    for r in rows:
        s = safe_str(r.get("import_status"))
        out[s] = out.get(s, 0) + 1
    return out


def _counts_by_asset_type(rows: list[dict]) -> dict:
    out: dict = {}
    for r in rows:
        at = safe_str(r.get("asset_type"))
        bucket = out.setdefault(at, {"total": 0})
        bucket["total"] += 1
        s = safe_str(r.get("import_status"))
        bucket[s] = bucket.get(s, 0) + 1
    return out


# ── HTML ────────────────────────────────────────────────────────────────────

def _esc(v) -> str:
    return (safe_str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _render_html(config, summary: dict, rows: list[dict]) -> str:
    by_status = _counts_by_status(rows)
    by_type = _counts_by_asset_type(rows)
    failures = [r for r in rows if safe_str(r.get("import_status")) == "failed"]
    warned = [r for r in rows if safe_str(r.get("import_status")) == "created_with_warning"]
    manual = [r for r in rows if safe_str(r.get("import_status")) == "manual"]

    mode_banner = ""
    if summary.get("dry_run"):
        mode_banner = ('<div class="banner dry">DRY RUN — decisions are real, but NOTHING was '
                       'written to the target workspace.</div>')
    if safe_str(summary.get("run_status")) == "aborted":
        mode_banner += ('<div class="banner abort">RUN ABORTED — this report is PARTIAL. '
                        f'Reason: {_esc(summary.get("abort_reason"))}</div>')

    def cards() -> str:
        out = []
        for status in _SUMMARY_ORDER:
            label, colour = _STATUS_STYLE.get(status, (status, "FFFFFF"))
            out.append(f'<div class="card" style="background:#{colour}">'
                       f'<div class="n">{by_status.get(status, 0)}</div>'
                       f'<div class="l">{_esc(label)}</div></div>')
        return "".join(out)

    def table(items, cols, title, cls=""):
        if not items:
            return ""
        head = "".join(f"<th>{_esc(c[0])}</th>" for c in cols)
        body = ""
        for r in items:
            tds = "".join(f"<td>{_esc(r.get(c[1]))}</td>" for c in cols)
            body += f"<tr>{tds}</tr>"
        return (f'<h2>{_esc(title)} ({len(items)})</h2>'
                f'<table class="{cls}"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>')

    type_rows = ""
    for at in sorted(by_type):
        b = by_type[at]
        cells = "".join(f"<td>{b.get(s, 0) or ''}</td>" for s in _SUMMARY_ORDER)
        type_rows += f"<tr><td>{_esc(at)}</td><td><b>{b['total']}</b></td>{cells}</tr>"
    type_head = "".join(f"<th>{_STATUS_STYLE[s][0]}</th>" for s in _SUMMARY_ORDER)

    unit_cols = [("Asset Type", "asset_type"), ("Natural Key", "natural_key"),
                 ("Import Status", "import_status"), ("Action Taken", "action_taken"),
                 ("Target Id", "target_id"), ("Note", "note")]
    # Failures additionally show the COMPLETE server error verbatim (never hidden behind the note).
    fail_cols = unit_cols + [("Actual server error", "error_raw")]

    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Import Results — run {_esc(summary.get('run_id'))}</title>
<style>
 body{{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;margin:0;
      background:#f8fafc;color:#0f172a}}
 header{{background:#1e3a5f;color:#fff;padding:22px 28px}}
 header h1{{margin:0 0 6px;font-size:20px}} header .meta{{opacity:.85;font-size:13px}}
 main{{padding:20px 28px 60px}}
 .banner{{padding:10px 14px;border-radius:6px;margin:0 0 16px;font-weight:600}}
 .banner.dry{{background:#dbeafe;color:#1e40af}} .banner.abort{{background:#fee2e2;color:#991b1b}}
 .cards{{display:flex;flex-wrap:wrap;gap:10px;margin:0 0 22px}}
 .card{{border:1px solid #cbd5e1;border-radius:8px;padding:10px 16px;min-width:104px}}
 .card .n{{font-size:22px;font-weight:700}} .card .l{{font-size:11px;color:#334155}}
 h2{{font-size:15px;margin:26px 0 8px}}
 table{{border-collapse:collapse;background:#fff;width:100%;font-size:12px;
        box-shadow:0 1px 2px rgba(0,0,0,.06)}}
 th,td{{border:1px solid #e2e8f0;padding:5px 8px;text-align:left;vertical-align:top}}
 th{{background:#1e3a5f;color:#fff;position:sticky;top:0}}
 table.fail td{{background:#fee2e2}} table.warn td{{background:#fef9c3}}
 table.manual td{{background:#fef3c7}}
 td:nth-child(2){{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;max-width:520px;
                  word-break:break-all}}
</style></head><body>
<header>
 <h1>Import Results — {_esc(summary.get('source_workspace_id'))} → this workspace</h1>
 <div class="meta">run <b>{_esc(summary.get('run_id'))}</b> &middot;
  mode {_esc(summary.get('connectivity_mode'))} &middot;
  {'DRY RUN' if summary.get('dry_run') else 'LIVE'} &middot;
  status {_esc(summary.get('run_status'))} &middot;
  {_esc(summary.get('elapsed_sec'))}s &middot; generated {_esc(summary.get('generated_utc'))}</div>
</header><main>
{mode_banner}
<div class="cards">{cards()}</div>
{table(failures, fail_cols, "Failures — fix these, then re-run with retry_mode=failed_only", "fail")}
{table(warned, unit_cols, "Created with warnings — exist on target but degraded", "warn")}
{table(manual, unit_cols, "Manual steps required on target", "manual")}
<h2>Per asset type</h2>
<table><thead><tr><th>Asset Type</th><th>Total</th>{type_head}</tr></thead>
<tbody>{type_rows}</tbody></table>
{table(rows, unit_cols, "Every unit")}
</main></body></html>"""


# ── Excel ───────────────────────────────────────────────────────────────────

def _render_xlsx(local_path: str, config, summary: dict, rows: list[dict]) -> str:
    """Import Summary sheet + one sheet per family. Rendered locally (openpyxl needs seeks)."""
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    def fill(c):
        return PatternFill("solid", fgColor=c)

    def font(bold=False, color="000000", size=10, italic=False):
        return Font(bold=bold, color=color, size=size, italic=italic, name="Calibri")

    thin = Side(style="thin", color="CBD5E1")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)
    centre = Alignment(horizontal="center", vertical="center")
    left_wrap = Alignment(horizontal="left", vertical="top", wrap_text=True)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Import Summary"
    ws.sheet_view.showGridLines = False

    ws.merge_cells("A1:J1")
    c = ws["A1"]
    c.value = "Workspace Import — Status Summary"
    c.font = font(bold=True, color="FFFFFF", size=16)
    c.fill = fill("FF3621")
    c.alignment = centre
    ws.row_dimensions[1].height = 34

    ws.merge_cells("A2:J2")
    c = ws["A2"]
    c.value = (f"Source workspace {summary.get('source_workspace_id')}   |   "
               f"Run {summary.get('run_id')}   |   Mode {summary.get('connectivity_mode')}   |   "
               f"{'DRY RUN — nothing written' if summary.get('dry_run') else 'LIVE'}   |   "
               f"Status {summary.get('run_status')}   |   "
               f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    c.font = font(italic=True, color="475569", size=9)
    c.alignment = centre

    # Roll-up by status
    r = 4
    ws.cell(row=r, column=1, value="Outcome roll-up").font = font(bold=True, size=12,
                                                                  color="1E3A5F")
    r += 1
    by_status = _counts_by_status(rows)
    for col, status in enumerate(_SUMMARY_ORDER, 1):
        label, colour = _STATUS_STYLE.get(status, (status, "FFFFFF"))
        h = ws.cell(row=r, column=col, value=label)
        h.font = font(bold=True, size=9)
        h.fill = fill(colour)
        h.border = box
        h.alignment = centre
        v = ws.cell(row=r + 1, column=col, value=by_status.get(status, 0))
        v.font = font(bold=True, size=12)
        v.border = box
        v.alignment = centre
    r += 3

    # FAILURES FIRST — the whole point of the summary sheet is that nobody has to hunt for them.
    failures = [x for x in rows if safe_str(x.get("import_status")) == "failed"]
    ws.cell(row=r, column=1, value=f"Failures ({len(failures)})").font = font(
        bold=True, color="B91C1C", size=12)
    r += 1
    for col, h in enumerate(["Asset Type", "Natural Key", "Category",
                             "Actual server error + hint"], 1):
        cc = ws.cell(row=r, column=col, value=h)
        cc.font = font(bold=True, color="FFFFFF")
        cc.fill = fill("B91C1C")
        cc.border = box
    for x in failures:
        r += 1
        # Prefer the complete raw server error; fall back to the note (which already leads with the
        # server text) so the operator always sees what the target actually said — never a canned
        # message alone.
        reason = safe_str(x.get("error_raw")) or safe_str(x.get("note"))
        for col, v in enumerate([x.get("asset_type"), x.get("natural_key"),
                                 x.get("failure_category"), reason], 1):
            cc = ws.cell(row=r, column=col, value=safe_str(v))
            cc.fill = fill("FEE2E2")
            cc.border = box
            cc.font = font(size=9)
            cc.alignment = left_wrap
    r += 2

    # Manual actions table
    manual = [x for x in rows if safe_str(x.get("import_status")) == "manual"]
    ws.cell(row=r, column=1, value=f"Manual steps required ({len(manual)})").font = font(
        bold=True, color="92400E", size=12)
    r += 1
    for col, h in enumerate(["Asset Type", "Natural Key", "What a human must do"], 1):
        cc = ws.cell(row=r, column=col, value=h)
        cc.font = font(bold=True, color="FFFFFF")
        cc.fill = fill("92400E")
        cc.border = box
    for x in manual:
        r += 1
        for col, v in enumerate([x.get("asset_type"), x.get("natural_key"), x.get("note")], 1):
            cc = ws.cell(row=r, column=col, value=safe_str(v))
            cc.fill = fill("FEF3C7")
            cc.border = box
            cc.font = font(size=9)
            cc.alignment = left_wrap
    r += 2

    # Per-asset-type counts
    ws.cell(row=r, column=1, value="Per asset type").font = font(bold=True, size=12,
                                                                 color="1E3A5F")
    r += 1
    headers = ["Asset Type", "Total"] + [_STATUS_STYLE[s][0] for s in _SUMMARY_ORDER]
    for col, h in enumerate(headers, 1):
        cc = ws.cell(row=r, column=col, value=h)
        cc.font = font(bold=True, color="FFFFFF", size=9)
        cc.fill = fill("1E3A5F")
        cc.border = box
        cc.alignment = centre
    by_type = _counts_by_asset_type(rows)
    for at in sorted(by_type):
        r += 1
        b = by_type[at]
        vals = [at, b["total"]] + [b.get(s, 0) for s in _SUMMARY_ORDER]
        for col, v in enumerate(vals, 1):
            cc = ws.cell(row=r, column=col, value=v)
            cc.border = box
            cc.font = font(size=9, bold=(col == 2))
            cc.alignment = centre if col > 1 else left_wrap

    for i, w in enumerate([26, 52, 20, 70, 14, 14, 14, 14, 14, 14], 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    # ── one sheet PER ASSET TYPE, in inventory/export card order (IMP-1) ─────
    # Import keeps its own import-specific columns (status / action / ids / note), but the SHEET
    # LAYOUT — one tab per asset type, named + ordered like inventory.xlsx — now matches the other
    # two stages so an operator reads the same tabs across all three workbooks.
    from src.reports.inventory_view import _LABELS, _SUMMARY_CARD_KEYS

    cols = [("Asset Type", "asset_type", 24), ("Natural Key", "natural_key", 56),
            ("Import Status", "import_status", 22), ("Action Taken", "action_taken", 26),
            ("Target Id", "target_id", 24), ("Source Id", "source_id", 22),
            ("Failure Category", "failure_category", 20), ("Note / reason", "note", 70),
            ("Actual server error", "error_raw", 80)]

    by_card: dict[str, list] = {}
    for x in rows:
        by_card.setdefault(_card_for_asset_type(x.get("asset_type")), []).append(x)

    # Inventory card order first (so tabs line up with the other workbooks), then any leftover
    # cards not in the canonical list (defensive — a new importer type still gets a tab).
    ordered_cards = [k for k in _SUMMARY_CARD_KEYS if k in by_card]
    ordered_cards += [k for k in sorted(by_card) if k not in _SUMMARY_CARD_KEYS]

    import re as _re
    for card in ordered_cards:
        label = _LABELS.get(card, card.replace("_", " ").title())
        sheet_name = _re.sub(r"[\\/?*\[\]:]", "-", label)[:31]
        sheet = wb.create_sheet(sheet_name)
        sheet.sheet_view.showGridLines = False
        for col, (h, _k, w) in enumerate(cols, 1):
            cc = sheet.cell(row=1, column=col, value=h)
            cc.font = font(bold=True, color="FFFFFF", size=10)
            cc.fill = fill("1E3A5F")
            cc.border = box
            cc.alignment = centre
            sheet.column_dimensions[get_column_letter(col)].width = w
        for i, x in enumerate(by_card[card], start=2):
            status = safe_str(x.get("import_status"))
            _label, colour = _STATUS_STYLE.get(status, (status, "FFFFFF"))
            for col, (_h, key, _w) in enumerate(cols, 1):
                cc = sheet.cell(row=i, column=col, value=safe_str(x.get(key)))
                cc.border = box
                cc.font = font(size=9)
                cc.alignment = left_wrap
                if key == "import_status":
                    cc.fill = fill(colour)
                    cc.value = _label
        sheet.freeze_panes = "A2"

    wb.save(local_path)
    return local_path


# ── manual actions runbook ──────────────────────────────────────────────────

def _render_manual_actions(summary: dict, rows: list[dict]) -> str:
    """The import-side manual runbook.

    Written as its OWN file rather than overwriting export's `manual_actions.md`: the export-side
    rows (secret values to re-populate, DAB bundles to redeploy, oversize files to copy) still
    apply, and clobbering them would lose half the runbook.
    """
    manual = [r for r in rows if safe_str(r.get("import_status")) == "manual"]
    failed = [r for r in rows if safe_str(r.get("import_status")) == "failed"]
    warned = [r for r in rows if safe_str(r.get("import_status")) == "created_with_warning"]
    no_object = [r for r in rows if safe_str(r.get("import_status")) == "skipped_no_object"]

    out = [
        "# Manual actions after import",
        "",
        f"Run `{summary.get('run_id')}` — source workspace `{summary.get('source_workspace_id')}`"
        f"{' (DRY RUN — nothing was written)' if summary.get('dry_run') else ''}.",
        "",
        "This is the IMPORT-side runbook. The export-side `export/manual/manual_actions.md` still "
        "applies too (secret values, DAB redeploys, oversize files) — it is not superseded.",
        "",
    ]

    def section(title, items, guidance=""):
        if not items:
            return
        out.append(f"## {title} ({len(items)})")
        out.append("")
        if guidance:
            out.append(guidance)
            out.append("")
        by_type: dict[str, list] = {}
        for r in items:
            by_type.setdefault(safe_str(r.get("asset_type")), []).append(r)
        for at in sorted(by_type):
            out.append(f"### {at} ({len(by_type[at])})")
            for r in sorted(by_type[at], key=lambda x: safe_str(x.get("natural_key"))):
                note = safe_str(r.get("note"))
                out.append(f"- `{safe_str(r.get('natural_key'))}`" + (f" — {note}" if note else ""))
            out.append("")

    section("Manual recreate / re-populate", manual,
            "These have no REST create path in scope, so the tool never attempted them. Each line "
            "carries what it is and why.")
    section("Failures to fix, then retry", failed,
            "Fix the cause, then re-run `04_Import` with `retry_mode=failed_only` — that attempts "
            "ONLY these units and touches nothing else.")
    section("Created but degraded — verify before use", warned,
            "These EXIST on target but a reference could not be fully resolved (most often a "
            "notebook path inside a Git folder that has not been recreated). They will create "
            "fine and fail at first RUN, so they need a look. `retry_mode=failed_only` includes "
            "them once the prerequisite is in place.")
    section("Permissions not applied — target object absent", no_object,
            "The ACL could not be applied because its object does not exist on target yet "
            "(usually by design: DAB content, an out-of-scope repo, or a deferred family). Once "
            "the object exists, re-run with `import_assets=acls` and "
            "`retry_mode=skipped_only` to apply exactly these grants.")

    deleted = summary.get("deleted_in_source") or {}
    if deleted:
        out.append(f"## Deleted in source — review ({sum(len(v) for v in deleted.values())})")
        out.append("")
        out.append("These exist on TARGET (this tool created them on an earlier run) but are no "
                   "longer in the source bundle. They were **not** deleted — deletion requires "
                   "`allow_deletes=true`. Confirm each was deliberately removed on source.")
        out.append("")
        for at, keys in sorted(deleted.items()):
            out.append(f"### {at} ({len(keys)})")
            for k in sorted(keys):
                out.append(f"- `{k}`")
            out.append("")

    if len(out) <= 7:
        out.append("Nothing outstanding — no manual actions were recorded for this run.")
    return "\n".join(out) + "\n"
