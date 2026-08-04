"""
token_manager — authentication + REST client for THIS workspace only.

AIR-GAPPED model: a workspace NEVER authenticates to the other workspace. Each side (source
export / target import) runs as its own **workspace-admin run-as SP** and calls only its own
workspace REST APIs using that SP's **notebook-context token**.

  • No OAuth M2M.  • No PATs.  • No cross-workspace client.  • No secrets.

Auth strategy (adapted from the customer inventory notebook driver):
  1. SDK `WorkspaceClient()` ambient auth (works on serverless / shared / single-user) →
     read the bearer token + host from its config;
  2. fallback to the notebook-context token (classic single-user clusters).
"""
from __future__ import annotations

import time
from typing import Any, Optional

import requests

from src.config.config_manager import WorkspaceContext
from src.utils.logger import get_logger
from src.utils.retry import RetryableHTTPError, is_retryable_status, with_retry

_LOG = get_logger("auth")


class DownloadHTTPError(Exception):
    """A non-retryable 4xx/5xx during a raw byte download; carries status + server body."""

    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(message or f"HTTP {status}")
        self.status = status


class OversizeError(Exception):
    """A raw download was refused/aborted because it exceeded the caller's byte cap (§5a)."""

    def __init__(self, size: int, message: str = "") -> None:
        super().__init__(message or f"oversize: {size} bytes")
        self.size = size


# ---------------------------------------------------------------------------
# Context resolution (this workspace's run-as SP token + host)
# ---------------------------------------------------------------------------

def resolve_context(dbutils=None, spark=None, account_id: str = "") -> WorkspaceContext:
    """Resolve THIS workspace's host + bearer token from the run-as identity.

    Order: SDK ambient auth first (serverless/shared/single-user), then notebook context token.
    Raises RuntimeError if neither yields a token.
    """
    host, token = "", ""

    # 1. Databricks SDK ambient auth
    try:
        from databricks.sdk import WorkspaceClient
        wc = WorkspaceClient()
        auth_header = wc.config.authenticate() or {}
        bearer = auth_header.get("Authorization", "")
        if bearer.lower().startswith("bearer "):
            token = bearer.split(" ", 1)[1]
        if wc.config.host:
            host = wc.config.host
    except Exception as exc:
        _LOG.warning("SDK ambient auth unavailable; falling back to notebook context", error=str(exc))

    # 2. Notebook context token (classic clusters)
    if (not token or not host) and dbutils is not None:
        try:
            ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
            if not token:
                token = ctx.apiToken().get()
            if not host:
                host = "https://" + ctx.browserHostName().get()
        except Exception as exc:
            _LOG.warning("notebook context token unavailable", error=str(exc))

    if not token or not host:
        raise RuntimeError(
            "Could not resolve workspace host/token. Run as a Job (run-as SP) with SDK "
            "ambient auth available, or on a cluster exposing the notebook context token."
        )

    return WorkspaceContext(workspace_url=host.rstrip("/"), token=token, account_id=account_id)


# ---------------------------------------------------------------------------
# Token providers
# ---------------------------------------------------------------------------

class StaticTokenProvider:
    """Wraps an already-issued token (the notebook-context token for this workspace)."""

    def __init__(self, token: str) -> None:
        self._token = token

    def __call__(self) -> str:
        return self._token


# ---------------------------------------------------------------------------
# REST client (bound to THIS workspace)
# ---------------------------------------------------------------------------

class ApiClient:
    """Thin authenticated REST client with retry, pagination, and SCIM helpers.

    Records pagination-truncation and fetch warnings on `.warnings` so the report can surface
    them instead of failing silently.
    """

    def __init__(self, host: str, token_provider, verify_ssl: bool = True, timeout: int = 60) -> None:
        self._base = host.rstrip("/")
        self._token_provider = token_provider
        self._verify = verify_ssl
        self._timeout = timeout
        self._s = requests.Session()
        self.warnings: list[str] = []

    @property
    def base_url(self) -> str:
        return self._base

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token_provider()}",
                "Content-Type": "application/json"}

    def _request(self, method: str, path: str, *, params=None, json_body=None) -> Any:
        url = f"{self._base}/{path.lstrip('/')}"

        def _do():
            r = self._s.request(method, url, headers=self._headers(), params=params,
                                json=json_body, verify=self._verify, timeout=self._timeout)
            if is_retryable_status(r.status_code):
                retry_after = r.headers.get("Retry-After")
                raise RetryableHTTPError(
                    r.status_code,
                    f"{method} {url} -> {r.status_code}",
                    retry_after=float(retry_after) if retry_after else None,
                )
            r.raise_for_status()
            if r.text:
                try:
                    return r.json()
                except ValueError:
                    return {"_raw": r.text}
            return {}

        return with_retry(_do)

    def get(self, path: str, params: Optional[dict] = None) -> Any:
        return self._request("GET", path, params=params)

    def download_bytes(self, path: str, params: Optional[dict] = None, *,
                       max_bytes: int = 0) -> bytes:
        """GET a raw (non-JSON) body as bytes — for notebook/workspace-file CONTENT export.

        Streams the response so a large file isn't buffered twice. Retries on 429/5xx like every
        other call. `max_bytes>0` enforces a hard size cap: if the server advertises a larger
        Content-Length, OR the streamed body exceeds it, an `OversizeError` is raised (the caller
        turns that into a `skipped_oversize` record — Plan 2 §5a) instead of downloading a giant.
        Non-2xx responses raise (the body text is attached so callers can detect size errors).
        """
        url = f"{self._base}/{path.lstrip('/')}"

        def _do() -> bytes:
            with self._s.get(url, headers=self._headers(), params=params, verify=self._verify,
                             timeout=self._timeout, stream=True) as r:
                if is_retryable_status(r.status_code):
                    retry_after = r.headers.get("Retry-After")
                    raise RetryableHTTPError(
                        r.status_code, f"GET {url} -> {r.status_code}",
                        retry_after=float(retry_after) if retry_after else None)
                if r.status_code >= 400:
                    # Surface the server message (e.g. MAX_NOTEBOOK_SIZE_EXCEEDED) to the caller.
                    body = ""
                    try:
                        body = r.text[:2000]
                    except Exception:  # noqa: BLE001
                        pass
                    raise DownloadHTTPError(r.status_code, f"GET {url} -> {r.status_code}: {body}")
                if max_bytes:
                    clen = r.headers.get("Content-Length")
                    if clen and clen.isdigit() and int(clen) > max_bytes:
                        raise OversizeError(int(clen),
                                            f"Content-Length {clen} exceeds cap {max_bytes}")
                chunks: list[bytes] = []
                total = 0
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if max_bytes and total > max_bytes:
                        raise OversizeError(total, f"streamed body exceeds cap {max_bytes}")
                    chunks.append(chunk)
                return b"".join(chunks)

        return with_retry(_do)

    def post(self, path: str, body: dict) -> Any:
        return self._request("POST", path, json_body=body)

    def put(self, path: str, body: dict) -> Any:
        return self._request("PUT", path, json_body=body)

    def patch(self, path: str, body: dict) -> Any:
        return self._request("PATCH", path, json_body=body)

    def delete(self, path: str, params: Optional[dict] = None) -> Any:
        return self._request("DELETE", path, params=params)

    # ── pagination helpers ────────────────────────────────────────────────
    def get_paginated(self, path: str, result_key: str, *, token_key: str = "next_page_token",
                      params: Optional[dict] = None, max_pages: int = 100_000) -> list:
        """Cursor pagination. `params` are sent on EVERY page (page_size, filters, etc.).

        Raises an explicit TRUNCATED warning (recorded on self.warnings) if the page cap is hit
        while a cursor still exists — never silently truncates.
        """
        base = dict(params or {})
        page_params = dict(base)
        items: list = []
        for page in range(max_pages):
            data = self.get(path, params=page_params)
            batch = data.get(result_key, []) if isinstance(data, dict) else []
            if isinstance(batch, list):
                items.extend(batch)
            cursor = data.get(token_key, "") if isinstance(data, dict) else ""
            if not cursor:
                return items
            page_params = dict(base)
            page_params["page_token"] = cursor
        msg = (f"{path}: hit the {max_pages}-page cap at {len(items)} items — MORE DATA EXISTS "
               f"and was NOT fetched (raise max_pages).")
        self.warnings.append(msg)
        _LOG.warning("pagination truncated", path=path, items=len(items))
        return items

    def get_scim(self, resource: str, max_items: int = 0, count: int = 500) -> list:
        """Paginate SCIM (Users/Groups/ServicePrincipals) via startIndex/count.

        max_items>0 caps the total fetched (for very large workspaces).
        """
        items: list = []
        start = 1
        while True:
            data = self.get(f"api/2.0/preview/scim/v2/{resource}",
                            params={"startIndex": start, "count": count})
            resources = data.get("Resources", []) if isinstance(data, dict) else []
            total = data.get("totalResults", 0) if isinstance(data, dict) else 0
            items.extend(resources)
            if max_items and len(items) >= max_items:
                return items[:max_items]
            if not resources or start + count - 1 >= total:
                return items
            start += count

    def list_existing(self, path: str, result_key: str, name_field: str = "name") -> set:
        """Return a set of existing resource names for duplicate checks (best-effort)."""
        try:
            data = self.get(path)
            items = data.get(result_key, []) if isinstance(data, dict) else []
            return {i.get(name_field, "") for i in items if i.get(name_field)}
        except Exception as exc:
            _LOG.warning("list_existing failed", path=path, error=str(exc))
            return set()


def build_client(config, dbutils=None, spark=None) -> ApiClient:
    """Return an ApiClient bound to THIS workspace. Populates config.ctx if not already set."""
    if not config.ctx.token or not config.ctx.workspace_url:
        config.ctx = resolve_context(dbutils, spark, account_id=config.ctx.account_id)
    return ApiClient(config.ctx.workspace_url, StaticTokenProvider(config.ctx.token))
