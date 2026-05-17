from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest


def _reload_connector_modules(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("CONTEXTCORE_STORAGE_DIR", str(tmp_path))

    import Connectors.store as store_mod
    import Connectors.Connectors as connectors_mod

    store_mod = importlib.reload(store_mod)
    connectors_mod = importlib.reload(connectors_mod)
    store_mod.init_db()
    return store_mod, connectors_mod


def test_connector_store_insert_search_and_fetch(monkeypatch: pytest.MonkeyPatch, tmp_path):
    store_mod, connectors_mod = _reload_connector_modules(monkeypatch, tmp_path)

    doc = connectors_mod.ConnectorDocument(
        provider="notion",
        account_id="acct-1",
        external_id="page-1",
        uri="notion://acct-1/page/page-1",
        title="Quarterly Plan",
        body_text="Quarterly plan for retrieval and indexing connectors.",
        url="https://notion.so/page-1",
        updated_at="2026-05-17T00:00:00Z",
        object_type="page",
        metadata={"provider_object": "page"},
        raw_json="{}",
        content_hash=store_mod.compute_content_hash(
            "Quarterly plan for retrieval and indexing connectors."
        ),
    )

    assert store_mod.upsert_document_state(doc) == "inserted"
    hits = store_mod.search_connector_documents("retrieval connectors", top_k=5)
    assert hits
    assert hits[0]["path"] == doc.uri
    fetched = store_mod.fetch_document_by_uri(doc.uri)
    assert fetched
    assert "Quarterly plan" in fetched["content"]


def test_sync_connector_tracks_unchanged_documents(monkeypatch: pytest.MonkeyPatch, tmp_path):
    store_mod, connectors_mod = _reload_connector_modules(monkeypatch, tmp_path)

    class FakeProvider:
        provider_name = "notion"

        def validate_auth(self, auth):
            return connectors_mod.AccountIdentity(
                provider="notion",
                account_id="acct-1",
                display_name="Workspace",
                metadata={},
            )

        def discover_roots(self, account):
            return [
                connectors_mod.RemoteRef(
                    external_id="page-1",
                    object_type="page",
                    title="Root Page",
                )
            ]

        def crawl_object(self, account, ref):
            body = "Root content for connector sync."
            doc = connectors_mod.ConnectorDocument(
                provider="notion",
                account_id=account.account_id,
                external_id=ref.external_id,
                uri=f"notion://{account.account_id}/page/{ref.external_id}",
                title=ref.title,
                body_text=body,
                url=None,
                updated_at="2026-05-17T00:00:00Z",
                object_type="page",
                metadata={},
                raw_json="{}",
                content_hash=store_mod.compute_content_hash(body),
            )
            return [doc], []

    monkeypatch.setattr(
        connectors_mod,
        "_provider_for_name",
        lambda provider, auth: FakeProvider(),
    )

    first = connectors_mod.sync_connector("notion", "secret")
    second = connectors_mod.sync_connector("notion", "secret")

    assert first.updated == 1
    assert second.unchanged == 1
