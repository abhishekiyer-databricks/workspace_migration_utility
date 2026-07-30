"""Offline fakes for testing collectors without a Databricks workspace."""
from __future__ import annotations


class FakeClient:
    """Mimics auth.ApiClient. Answers GET/paginated/SCIM from canned tables."""

    def __init__(self, get_table=None, paginated_table=None, scim_table=None):
        self.get_table = get_table or {}
        self.paginated_table = paginated_table or {}
        self.scim_table = scim_table or {}
        self.warnings: list[str] = []

    def get(self, path, params=None):
        params = params or {}
        entry = self.get_table.get(path)
        if callable(entry):
            return entry(params)
        return entry if entry is not None else {}

    def get_paginated(self, path, result_key, token_key="next_page_token", params=None, max_pages=100000):
        return self.paginated_table.get(path, [])

    def get_scim(self, resource, max_items=0, count=500):
        return self.scim_table.get(resource, [])
