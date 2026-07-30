"""
InventoryRunner — orchestrates all source-side collectors for `01_Inventory`.

Keeps the notebook thin: build a client, run every in-scope collector (read-only), classify
identities, then emit inventory.json / .html / .xlsx + identity_classification.json +
config_resolved.json into the run's staging bundle dir. Nothing here mutates the workspace.
"""
from __future__ import annotations

from src.collectors.apps_collector import AppsCollector
from src.collectors.compute_collector import ComputeCollector
from src.collectors.dashboards_collector import DashboardsCollector
from src.collectors.dlt_collector import DltCollector
from src.collectors.genie_collector import GenieCollector
from src.collectors.identity_collector import IdentityCollector
from src.collectors.jobs_collector import JobsCollector
from src.collectors.lakebase_collector import LakebaseCollector
from src.collectors.misc_collector import MiscCollector
from src.collectors.secrets_collector import SecretsCollector
from src.collectors.serving_collector import ServingCollector
from src.collectors.sql_collector import SqlCollector
from src.collectors.workspace_collector import WorkspaceCollector
from src.identity.classifier import classify_all, classification_summary
from src.utils.helpers import now_iso
from src.utils.logger import get_logger

_LOG = get_logger("inventory")

# All in-scope collectors (inventory is always full-scope; toggles apply from Export on).
_COLLECTORS = [
    IdentityCollector, ComputeCollector, WorkspaceCollector, SecretsCollector,
    JobsCollector, SqlCollector, DltCollector, DashboardsCollector,
    GenieCollector, ServingCollector, MiscCollector,
    AppsCollector, LakebaseCollector,   # inventory-only (migration flagged manual, v1)
]


class InventoryRunner:
    def __init__(self, client, config, artifact_writer, dbutils=None) -> None:
        self.client = client
        self.config = config
        self.aw = artifact_writer
        self.dbutils = dbutils

    def run(self) -> dict:
        self.aw.ensure_output_path()
        objects_by_type: dict[str, list] = {}
        stats: list[dict] = []

        for cls in _COLLECTORS:
            coll = cls(self.client, self.config, self.dbutils)
            objs = coll.run()
            objects_by_type[coll.object_type] = objs
            stats.append(coll.stats())

        # Classify identities (annotates in place).
        identities = objects_by_type.get("identity", [])
        classify_all(identities)
        id_summary = classification_summary(identities)

        counts = {t: len(o) for t, o in objects_by_type.items()}
        warnings = [e for s in stats for e in s.get("errors", [])]

        # ── artifacts ─────────────────────────────────────────────────────
        self.aw.write_json("inventory.json", {
            "generated_utc": now_iso(),
            "source_workspace_id": self.config.source_workspace_id,
            "counts": counts,
            "objects_by_type": objects_by_type,
            "collector_stats": stats,
        })
        self.aw.write_json("identity_classification.json", {
            "summary": id_summary,
            "identities": [{k: v for k, v in o.items() if k != "_raw"} for o in identities],
        })
        self.aw.write_json("config_resolved.json", self.config.redacted())

        self._write_html(objects_by_type, counts, stats, id_summary, warnings)
        self._write_excel(objects_by_type, counts)

        _LOG.info("inventory complete", total=sum(counts.values()), warnings=len(warnings))
        return {"counts": counts, "identity_summary": id_summary,
                "warnings": warnings, "output_path": self.aw.root}

    def _write_html(self, objects_by_type, counts, stats, id_summary, warnings) -> None:
        from src.reports.html_generator import render_inventory
        html_doc = render_inventory(
            objects_by_type=objects_by_type, counts=counts, collector_stats=stats,
            identity_summary=id_summary, warnings=warnings,
            workspace_url=self.config.ctx.workspace_url, generated_at=now_iso(),
        )
        # HTML is plain text — safe to write directly to the Volume.
        self.aw.write_bytes("inventory.html", html_doc.encode("utf-8"))

    def _write_excel(self, objects_by_type, counts) -> None:
        try:
            from src.exporters.excel_generator import generate_excel
            self.aw.write_text_local_then_copy(
                "inventory.xlsx",
                lambda local: generate_excel(objects_by_type, counts, local, self.config),
            )
        except Exception as exc:  # noqa: BLE001 — Excel is optional; never fail the run
            _LOG.warning("excel generation skipped", error=str(exc))
