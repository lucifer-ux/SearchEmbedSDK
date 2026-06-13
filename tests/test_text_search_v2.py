import importlib
import threading
import time
from typing import Any
from pathlib import Path

import pytest


def _reload_text_modules(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("CONTEXTCORE_STORAGE_DIR", str(tmp_path))
    monkeypatch.setenv("CONTEXTCORE_TEXT_STORAGE_BACKEND", "sqlite")

    import text_search_implementation_v2.db as db_mod
    import text_search_implementation_v2.search as search_mod

    db_mod = importlib.reload(db_mod)
    search_mod = importlib.reload(search_mod)
    db_mod.init_db()
    return db_mod, search_mod

 
def _seed_docs(db_mod: Any):
    docs = [
        (
            "/tmp/doc_ai.txt",
            "doc_ai.txt",
            "notes",
            1.0,
            "Deep learning model optimization improves retrieval quality and token reduction.",
        ),
        (
            "/tmp/doc_typo.txt",
            "doc_typo.txt",
            "notes",
            2.0,
            "Neural retrieval handles noisy terms and spelling variations.",
        ),
        (
            "/tmp/report.txt",
            "report.txt",
            "notes",
            3.0,
            "Quarterly report includes budgets and timeline details.",
        ),
    ]
    for path, filename, cat, mtime, content in docs:
        db_mod.upsert_file(path=path, filename=filename, category=cat, mtime=mtime, content=content)


class _Row(dict):
    pass


def test_query_routing_weights_clean_vs_noisy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    db_mod, search_mod = _reload_text_modules(monkeypatch, tmp_path)
    engine = search_mod.TextSearchEngineV2()

    clean = engine._lane_weights("contextcore_hybrid", "deep learning retrieval", ["deep", "learning", "retrieval"])
    noisy = engine._lane_weights("contextcore_hybrid", "d33p le@rning", ["d33p", "le", "rning"])

    assert clean[0] >= 0.8
    if db_mod.trigram_supported():
        assert noisy[1] >= clean[1]


def test_rrf_merge_is_deterministic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _, search_mod = _reload_text_modules(monkeypatch, tmp_path)

    engine = search_mod.TextSearchEngineV2()
    porter_rows = [_Row(id=1), _Row(id=2), _Row(id=3)]
    trigram_rows = [_Row(id=2), _Row(id=1), _Row(id=4)]

    s1 = engine._merge_candidates_rrf(porter_rows, trigram_rows, porter_weight=1.0, trigram_weight=0.8)
    s2 = engine._merge_candidates_rrf(porter_rows, trigram_rows, porter_weight=1.0, trigram_weight=0.8)
    assert s1 == s2

    ranked = [doc_id for doc_id, _ in sorted(s1.items(), key=lambda x: (x[1], -x[0]), reverse=True)]
    assert ranked[0] in {1, 2}


def test_chunk_scoring_phrase_and_proximity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _, search_mod = _reload_text_modules(monkeypatch, tmp_path)
    engine = search_mod.TextSearchEngineV2()

    q = "deep learning model"
    tokens = ["deep", "learning", "model"]

    close_phrase = "This deep learning model improves retrieval."
    loose_match = "Deep systems are useful. Later we discuss a model. Learning happens elsewhere."

    assert engine._chunk_score(close_phrase, tokens=tokens, query_text=q) > engine._chunk_score(
        loose_match, tokens=tokens, query_text=q
    )


def test_exact_filename_returns_chunk_fields(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    db_mod, search_mod = _reload_text_modules(monkeypatch, tmp_path)
    _seed_docs(db_mod)

    engine = search_mod.TextSearchEngineV2()
    rows = engine.search("report.txt", top_k=5, include_metadata=True)

    assert rows
    first = rows[0]
    assert first.get("filename") == "report.txt"
    assert isinstance(first.get("chunk"), str) and first.get("chunk")
    assert first.get("chunk_id")


def test_token_budget_enforced(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    db_mod, search_mod = _reload_text_modules(monkeypatch, tmp_path)

    long_text = " ".join(["retrieval"] * 2000)
    db_mod.upsert_file(
        path="/tmp/long.txt",
        filename="long.txt",
        category="notes",
        mtime=1.0,
        content=long_text,
    )

    engine = search_mod.TextSearchEngineV2()
    rows = engine.search(
        "retrieval",
        top_k=1,
        include_metadata=True,
        max_context_tokens_per_result=20,
        retrieval_mode="contextcore_hybrid",
    )

    assert rows
    chunk = rows[0].get("chunk") or ""
    approx_tokens = int(round(len(chunk.split()) * 1.35))
    assert approx_tokens <= 24


def test_retrieval_modes_return_rows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    db_mod, search_mod = _reload_text_modules(monkeypatch, tmp_path)
    _seed_docs(db_mod)
    engine = search_mod.TextSearchEngineV2()

    modes = ["contextcore_hybrid", "bm25_only"]
    if db_mod.trigram_supported():
        modes.append("trigram_only")

    for mode in modes:
        rows = engine.search("retrieval", top_k=3, include_metadata=True, retrieval_mode=mode)
        assert isinstance(rows, list)
        assert rows


def test_turso_backend_selected_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("CONTEXTCORE_TEXT_STORAGE_BACKEND", raising=False)
    monkeypatch.setenv("CONTEXTCORE_STORAGE_DIR", str(tmp_path))
    monkeypatch.setenv("TURSO_DATABASE_URL", "libsql://example.turso.io")

    import text_search_implementation_v2.db as db_mod

    db_mod = importlib.reload(db_mod)
    assert db_mod.using_turso()

    monkeypatch.setenv("CONTEXTCORE_TEXT_STORAGE_BACKEND", "sqlite")
    db_mod = importlib.reload(db_mod)
    assert not db_mod.using_turso()


def test_turso_backend_selected_from_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "TURSO_DATABASE_URL=libsql://dotenv-example.turso.io",
                "TURSO_AUTH_TOKEN=dotenv-token",
                "export CONTEXTCORE_TEXT_STORAGE_BACKEND=turso",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CONTEXTCORE_ENV_FILE", str(env_file))
    monkeypatch.delenv("TURSO_DATABASE_URL", raising=False)
    monkeypatch.delenv("TURSO_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("CONTEXTCORE_TEXT_STORAGE_BACKEND", raising=False)

    import config as config_mod
    import text_search_implementation_v2.db as db_mod

    config_mod._env_loaded = False
    config_mod = importlib.reload(config_mod)
    db_mod = importlib.reload(db_mod)

    assert db_mod.using_turso()


def test_search_can_filter_by_matter_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    db_mod, search_mod = _reload_text_modules(monkeypatch, tmp_path)
    db_mod.upsert_file(
        path="/tmp/matter_a.txt",
        filename="matter_a.txt",
        category="brief",
        mtime=1.0,
        content="workflow automation platform for brightops matter alpha",
        matter_id="matter-alpha",
    )
    db_mod.upsert_file(
        path="/tmp/matter_b.txt",
        filename="matter_b.txt",
        category="brief",
        mtime=2.0,
        content="workflow automation platform for unrelated matter beta",
        matter_id="matter-beta",
    )

    engine = search_mod.TextSearchEngineV2()
    rows = engine.search("workflow automation platform", top_k=5, matter_id="matter-alpha")

    assert rows
    assert all(row.get("matter_id") == "matter-alpha" for row in rows)
    assert rows[0]["path"] == "/tmp/matter_a.txt"


def test_text_upsert_and_file_content_api_functions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("CONTEXTCORE_STORAGE_DIR", str(tmp_path))
    monkeypatch.setenv("CONTEXTCORE_TEXT_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("CONTEXTCORE_PREWARM_ON_STARTUP", "0")
    monkeypatch.setenv("CONTEXTCORE_ENABLE_WATCHER", "0")
    monkeypatch.setenv("CONTEXTCORE_STARTUP_SCAN", "0")

    import config as config_mod
    import text_search_implementation_v2.db as db_mod
    import text_search_implementation_v2.search as search_mod
    import unimain as unimain_mod

    config_mod._config_cache = None
    config_mod._env_loaded = False
    config_mod = importlib.reload(config_mod)
    db_mod = importlib.reload(db_mod)
    search_mod = importlib.reload(search_mod)
    unimain_mod = importlib.reload(unimain_mod)
    db_mod.init_db()

    payload = unimain_mod.TextUpsertRequest(
        path="/virtual/matters/doc1.txt",
        filename="doc1.txt",
        category="matter_upload",
        matter_id="matter-123",
        mtime=123.0,
        content="This Master Services Agreement is for BrightOps Solutions Private Limited.",
    )
    upserted = unimain_mod.text_upsert(payload)
    assert upserted["ok"] is True
    assert upserted["file"]["matter_id"] == "matter-123"

    content = unimain_mod.text_file_content(
        path="/virtual/matters/doc1.txt",
        file_id=None,
        matter_id="matter-123",
        include_chunks=True,
        chunk_chars=300,
        chunk_overlap=50,
    )
    assert content["ok"] is True
    assert content["file"]["matter_id"] == "matter-123"
    assert "Master Services Agreement" in content["content"]
    assert content["chunks"]


def test_text_upsert_retries_transient_turso_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("CONTEXTCORE_STORAGE_DIR", str(tmp_path))
    monkeypatch.setenv("CONTEXTCORE_TEXT_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("CONTEXTCORE_PREWARM_ON_STARTUP", "0")
    monkeypatch.setenv("CONTEXTCORE_ENABLE_WATCHER", "0")
    monkeypatch.setenv("CONTEXTCORE_STARTUP_SCAN", "0")

    import config as config_mod
    import text_search_implementation_v2.db as db_mod
    import unimain as unimain_mod

    config_mod._config_cache = None
    config_mod._env_loaded = False
    config_mod = importlib.reload(config_mod)
    db_mod = importlib.reload(db_mod)
    unimain_mod = importlib.reload(unimain_mod)
    db_mod.init_db()

    attempts = {"count": 0}
    real_upsert = db_mod.upsert_file

    def flaky_upsert(*args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("Hrana: http error: temporary failure in name resolution")
        return real_upsert(*args, **kwargs)

    monkeypatch.setattr(db_mod, "upsert_file", flaky_upsert)
    monkeypatch.setattr(db_mod, "is_probably_transient_turso_error", lambda exc: "hrana" in str(exc).lower())

    payload = unimain_mod.TextUpsertRequest(
        path="/virtual/matters/retry.txt",
        filename="retry.txt",
        category="matter_upload",
        matter_id="matter-retry",
        content="retry body for transient failure coverage",
    )
    result = unimain_mod.text_upsert(payload)

    assert result["ok"] is True
    assert attempts["count"] == 2


def test_turso_write_lock_serializes_calls(monkeypatch: pytest.MonkeyPatch):
    import unimain as unimain_mod

    unimain_mod = importlib.reload(unimain_mod)
    monkeypatch.setattr(
        "text_search_implementation_v2.db.using_turso",
        lambda: True,
    )

    active = {"count": 0, "max": 0}
    lock = threading.Lock()

    def critical_section():
        with lock:
            active["count"] += 1
            active["max"] = max(active["max"], active["count"])
        time.sleep(0.05)
        try:
            return True
        finally:
            with lock:
                active["count"] -= 1

    results = []
    errors = []

    def worker():
        try:
            results.append(
                unimain_mod._run_with_optional_turso_text_write_lock(
                    critical_section,
                    request_id="lock-test",
                    context={"path": "/virtual/test.txt"},
                )
            )
        except Exception as exc:  # pragma: no cover - defensive
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(results) == 3
    assert active["max"] == 1
