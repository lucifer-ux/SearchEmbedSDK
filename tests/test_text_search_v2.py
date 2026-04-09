import importlib
from typing import Any
from pathlib import Path

import pytest


def _reload_text_modules(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("CONTEXTCORE_STORAGE_DIR", str(tmp_path))

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


def test_rrf_merge_is_deterministic():
    from text_search_implementation_v2.search import TextSearchEngineV2

    engine = TextSearchEngineV2()
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
