"""
ConfigManager — centralises all runtime configuration for the workspace migration utility.

AIR-GAPPED model: the tool runs on TWO sides that never talk to each other. A `role` widget
declares which side this run is:
  • role="source"  → inventory/export; reads THIS (source) workspace; WRITES the bundle to
    `source_staging_location`.
  • role="target"  → preflight/transform/import/validate; READS the bundle from
    `target_staging_location` (uploaded there by ops); writes THIS (target) workspace.

Auth on BOTH sides = the run-as SP's notebook-context token for THIS workspace only
(no OAuth M2M, no PATs, no cross-workspace creds — see auth/token_manager.py).

Mirrors the `Config` dataclass pattern of uc-inventory-migration, EXTENDED to hold role +
staging locations + per-asset TOGGLES (all default True) + transform options.
Config is WIDGET-based: `from_dbutils()` reads notebook widgets / job params. No config files.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

ROLE_SOURCE = "source"
ROLE_TARGET = "target"
_VALID_ROLES = (ROLE_SOURCE, ROLE_TARGET)


@dataclass
class WorkspaceContext:
    """THIS workspace's context (resolved from the run-as SP via SDK / notebook context)."""
    workspace_url: str = ""          # derived from context
    token: str = ""                  # notebook-context token of the run-as SP
    account_id: str = ""             # optional (target side); enables account-level preflight


@dataclass
class AssetToggles:
    """Per-asset migration switches. ALL default True; operator flips to False to skip."""
    identity: bool = True
    compute: bool = True
    workspace: bool = True
    secrets: bool = True
    jobs: bool = True
    sql: bool = True
    dlt: bool = True
    dashboards: bool = True
    genie: bool = True
    serving: bool = True
    misc: bool = True


@dataclass
class TransformConfig:
    """Mapping + exclude + schedule options applied in 03_Transform_Review."""
    pause_job_schedules: bool = True
    user_domain_mapping: dict = field(default_factory=dict)   # old.com -> new.com
    user_id_mapping: dict = field(default_factory=dict)       # old@a.com -> new@b.com
    exclude_path_patterns: list = field(default_factory=list)
    exclude_job_name_patterns: list = field(default_factory=list)


@dataclass
class Config:
    """Top-level runtime config for one workspace migration run (one side of the air-gap)."""
    role: str = ""                   # "source" | "target"; guards mis-runs
    ctx: WorkspaceContext = field(default_factory=WorkspaceContext)
    toggles: AssetToggles = field(default_factory=AssetToggles)
    transform: TransformConfig = field(default_factory=TransformConfig)
    run_id: str = ""
    source_workspace_id: str = ""    # identifies the bundle: .../wsmig/<source_ws_id>/<run_id>
    # Staging locations — each a UC Volume path ("/Volumes/…"; managed or ADLS-backed
    # external volume; never raw abfss://):
    source_staging_location: str = ""   # role=source WRITES the bundle here
    target_staging_location: str = ""   # role=target READS the bundle here
    dry_run: bool = True
    # Source-side safety caps (0 = unlimited), carried from the inventory script.
    max_scim: int = 0
    max_workspace_items: int = 0
    max_ws_api_calls: int = 0

    @property
    def staging_location(self) -> str:
        """The staging root for THIS side (source writes / target reads)."""
        loc = self.source_staging_location if self.role == ROLE_SOURCE else self.target_staging_location
        return loc.rstrip("/")

    @property
    def output_path(self) -> str:
        """Run-isolated bundle dir: <staging_location>/wsmig/<source_ws_id>/<run_id>."""
        if not self.staging_location:
            raise ValueError("staging_location is empty for role=%r" % self.role)
        if not self.source_workspace_id:
            raise ValueError("source_workspace_id is required to build the bundle path")
        if not self.run_id:
            raise ValueError("run_id is required to build the bundle path")
        return f"{self.staging_location}/wsmig/{self.source_workspace_id}/{self.run_id}"

    # ── widget helpers ────────────────────────────────────────────────────
    @staticmethod
    def _widget(dbutils, name: str, default: str = "") -> str:
        """Read a widget value; return default if the widget is absent/blank."""
        try:
            val = dbutils.widgets.get(name)
        except Exception:
            return default
        return val if val not in (None, "") else default

    @classmethod
    def from_dbutils(cls, dbutils, spark, context_resolver=None) -> "Config":
        """Build config from widgets / job params + this workspace's notebook context.

        `context_resolver` is an optional callable returning a WorkspaceContext-like object
        (used for testing / to inject auth.token_manager.resolve_context). If None, the
        caller is expected to populate `cfg.ctx` afterwards (notebooks do this via
        auth.build_client). Widget parsing itself needs no workspace calls.
        """
        from src.utils.helpers import now_compact, parse_bool, parse_csv, parse_kv_list

        w = lambda n, d="": cls._widget(dbutils, n, d)  # noqa: E731

        role = (w("role") or "").strip().lower()
        if role not in _VALID_ROLES:
            raise ValueError(f"widget `role` must be one of {_VALID_ROLES}, got {role!r}")

        toggles = AssetToggles(
            identity=parse_bool(w("migrate_identity", "true"), True),
            compute=parse_bool(w("migrate_compute", "true"), True),
            workspace=parse_bool(w("migrate_workspace", "true"), True),
            secrets=parse_bool(w("migrate_secrets", "true"), True),
            jobs=parse_bool(w("migrate_jobs", "true"), True),
            sql=parse_bool(w("migrate_sql", "true"), True),
            dlt=parse_bool(w("migrate_dlt", "true"), True),
            dashboards=parse_bool(w("migrate_dashboards", "true"), True),
            genie=parse_bool(w("migrate_genie", "true"), True),
            serving=parse_bool(w("migrate_serving", "true"), True),
            misc=parse_bool(w("migrate_misc", "true"), True),
        )

        transform = TransformConfig(
            pause_job_schedules=parse_bool(w("pause_job_schedules", "true"), True),
            user_domain_mapping=parse_kv_list(w("user_domain_mapping")),
            user_id_mapping=parse_kv_list(w("user_id_mapping")),
            exclude_path_patterns=parse_csv(w("exclude_path_patterns")),
            exclude_job_name_patterns=parse_csv(w("exclude_job_name_patterns")),
        )

        cfg = cls(
            role=role,
            toggles=toggles,
            transform=transform,
            run_id=w("run_id") or now_compact(),
            source_workspace_id=w("source_workspace_id"),
            source_staging_location=w("source_staging_location"),
            target_staging_location=w("target_staging_location"),
            dry_run=parse_bool(w("dry_run", "true"), True),
            max_scim=int(w("max_scim", "0") or 0),
            max_workspace_items=int(w("max_workspace_items", "0") or 0),
            max_ws_api_calls=int(w("max_ws_api_calls", "0") or 0),
        )
        cfg.ctx.account_id = w("account_id")

        if context_resolver is not None:
            cfg.ctx = context_resolver()

        cfg.validate()
        return cfg

    def validate(self) -> None:
        """Fail fast on mis-configuration (e.g. wrong staging widget for the role)."""
        if self.role == ROLE_SOURCE and not self.source_staging_location:
            raise ValueError("role=source requires `source_staging_location`")
        if self.role == ROLE_TARGET and not self.target_staging_location:
            raise ValueError("role=target requires `target_staging_location`")
        if not self.source_workspace_id:
            raise ValueError("`source_workspace_id` is required")

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        """Build config from a plain dict (for tests / SDK callers)."""
        cfg = cls(
            role=d.get("role", ""),
            toggles=AssetToggles(**d.get("toggles", {})),
            transform=TransformConfig(**d.get("transform", {})),
            run_id=d.get("run_id", ""),
            source_workspace_id=d.get("source_workspace_id", ""),
            source_staging_location=d.get("source_staging_location", ""),
            target_staging_location=d.get("target_staging_location", ""),
            dry_run=d.get("dry_run", True),
            max_scim=d.get("max_scim", 0),
            max_workspace_items=d.get("max_workspace_items", 0),
            max_ws_api_calls=d.get("max_ws_api_calls", 0),
        )
        cfg.ctx = WorkspaceContext(**d.get("ctx", {}))
        return cfg

    def redacted(self) -> dict:
        """Return config as a dict with the context token removed (for config_resolved.json)."""
        d = asdict(self)
        if "ctx" in d and isinstance(d["ctx"], dict):
            d["ctx"] = {k: v for k, v in d["ctx"].items() if k != "token"}
        return d
