"""
Excel workbook generator (openpyxl): Summary sheet + one sheet per asset type + a
"Migration Plan" checklist sheet, matching the uc-inventory-migration workbook style.

IMPORTANT: openpyxl needs a seekable local disk — do NOT write straight to a FUSE /Volumes
path (it corrupts). Callers render to a local /tmp path via ArtifactWriter.write_text_local_
then_copy, which hands this function that local path.
"""
from __future__ import annotations

import json
from typing import Optional

# Columns shown per object type (kept compact + serialisable).
_SHEET_COLS = {
    "identity": ["identity_type", "natural_key", "displayName", "email", "classification",
                 "externalId", "entitlements", "member_count", "has_nested_groups"],
    "compute": ["compute_type", "_natural_key", "cluster_source", "ephemeral", "pinned"],
    "secret_scope": ["name", "backend_type", "values_migratable", "key_names"],
    "job": ["name", "job_id", "format", "has_owner_acl", "run_as"],
    "sql": ["sql_type", "name", "warehouse_type"],
    "dlt_pipeline": ["name", "pipeline_id"],
    "lakeview_dashboard": ["display_name", "warehouse_id", "parent_path"],
    "genie_space": ["title", "warehouse_id", "migratable"],
    "serving_endpoint": ["name"],
    "misc": ["misc_type", "_natural_key", "key", "value"],
    "workspace_object": ["path", "object_type", "language", "is_user_root"],
}


def _cell(v):
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str)[:32000]
    return "" if v is None else v


def generate_excel(objects_by_type: dict, counts: dict, local_path: str, config=None) -> str:
    """Write the workbook to `local_path` (a real seekable path). Return the path.

    `objects_by_type`: {object_type: [obj, ...]}.  `counts`: {object_type: n}.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    hdr_fill = PatternFill("solid", fgColor="0B3D5C")
    hdr_font = Font(bold=True, color="FFFFFF")

    def _style_header(ws, ncols):
        for c in range(1, ncols + 1):
            cell = ws.cell(row=1, column=c)
            cell.fill = hdr_fill
            cell.font = hdr_font

    # Summary sheet
    ws = wb.active
    ws.title = "Summary"
    ws.append(["Object type", "Count"])
    for k, v in sorted(counts.items()):
        ws.append([k, v])
    ws.append(["TOTAL", sum(counts.values()) if counts else 0])
    _style_header(ws, 2)

    # One sheet per object type
    for otype, objs in objects_by_type.items():
        cols = _SHEET_COLS.get(otype) or (list(objs[0].keys()) if objs else ["(empty)"])
        cols = [c for c in cols if not c.startswith("_raw")]
        sheet = wb.create_sheet(title=otype[:31])
        sheet.append(cols)
        for o in objs:
            sheet.append([_cell(o.get(c)) for c in cols])
        _style_header(sheet, len(cols))

    # Migration Plan checklist sheet
    mp = wb.create_sheet(title="Migration Plan")
    mp.append(["Object type", "Natural key", "Status (fill in)", "Notes"])
    for otype, objs in objects_by_type.items():
        for o in objs:
            mp.append([otype, o.get("natural_key") or o.get("_natural_key")
                       or o.get("name") or o.get("path") or "", "", ""])
    _style_header(mp, 4)

    wb.save(local_path)
    return local_path
