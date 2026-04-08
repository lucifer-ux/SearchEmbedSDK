from __future__ import annotations

import csv
import json
import math
import os
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from collections.abc import Callable
from typing import Any

from cli.ui import console, error, header, info, section, success, warning


def _dataset_url(dataset: str) -> str:
    """Return the canonical BEIR dataset ZIP URL for a dataset name."""
    return f"https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/{dataset}.zip"


def _download_and_unzip_dataset(dataset: str, datasets_root: Path) -> Path:
    """Download and extract a BEIR dataset if missing, otherwise reuse local cache."""
    # Reuse local dataset cache to keep repeated benchmark runs fast and reproducible.
    datasets_root.mkdir(parents=True, exist_ok=True)
    dataset_dir = datasets_root / dataset
    if dataset_dir.exists() and (dataset_dir / "corpus.jsonl").exists():
        return dataset_dir

    url = _dataset_url(dataset)
    try:
        from beir import util  # type: ignore

        extracted = util.download_and_unzip(url, str(datasets_root))
        return Path(extracted).resolve()
    except Exception:
        zip_path = datasets_root / f"{dataset}.zip"
        urllib.request.urlretrieve(url, zip_path)
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(datasets_root)
        return dataset_dir.resolve()


def _load_corpus(corpus_path: Path) -> dict[str, str]:
    """Load corpus.jsonl as a mapping: doc_id -> combined title+text."""
    corpus: dict[str, str] = {}
    with corpus_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            doc_id = str(row.get("_id", "")).strip()
            if not doc_id:
                continue
            title = str(row.get("title") or "").strip()
            text = str(row.get("text") or "").strip()
            joined = f"{title}\n{text}".strip()
            if joined:
                corpus[doc_id] = joined
    return corpus


def _load_queries(queries_path: Path) -> dict[str, str]:
    """Load queries.jsonl as a mapping: query_id -> query text."""
    queries: dict[str, str] = {}
    with queries_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            qid = str(row.get("_id", "")).strip()
            text = str(row.get("text") or "").strip()
            if qid and text:
                queries[qid] = text
    return queries


def _load_qrels(qrels_path: Path) -> dict[str, dict[str, int]]:
    """Load qrels/test.tsv into query->doc->relevance integer mapping."""
    qrels: dict[str, dict[str, int]] = {}
    with qrels_path.open("r", encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        for row in reader:
            qid = str(row.get("query-id") or "").strip()
            docid = str(row.get("corpus-id") or "").strip()
            if not qid or not docid:
                continue
            try:
                rel = int(row.get("score") or 0)
            except ValueError:
                rel = 0
            qrels.setdefault(qid, {})[docid] = rel
    return qrels


def _clear_text_db(db_module: Any) -> None:
    """Remove previously indexed benchmark data from all text retrieval lanes."""
    # Each benchmark run should start from a clean index state across all text lanes.
    conn = db_module.get_conn()
    try:
        with conn:
            conn.execute("DELETE FROM files_fts")
            try:
                conn.execute("DELETE FROM files_fts_trigram")
            except Exception:
                pass
            conn.execute("DELETE FROM files")
    finally:
        conn.close()


def _index_corpus(corpus: dict[str, str], category: str = "beir") -> int:
    """Index corpus documents through production text DB APIs using synthetic file paths."""
    # Index BEIR docs through production DB APIs to benchmark real retrieval behavior.
    from text_search_implementation_v2 import db as text_db

    text_db.init_db()
    _clear_text_db(text_db)

    inserted = 0
    for idx, (doc_id, content) in enumerate(corpus.items(), start=1):
        filename = f"{doc_id}.txt"
        synthetic_path = str(Path("/__beir__") / category / filename)
        ok = text_db.upsert_file(
            path=synthetic_path,
            filename=filename,
            category=category,
            mtime=float(idx),
            content=content,
        )
        if ok:
            inserted += 1
    return inserted


def _dcg_at_k(rels: list[int], k: int) -> float:
    """Compute Discounted Cumulative Gain at cutoff k for graded relevance values."""
    score = 0.0
    for i, rel in enumerate(rels[:k], start=1):
        score += (float((2 ** rel) - 1)) / math.log2(i + 1)
    return score


def _metrics_for_query(relevant: dict[str, int], ranked_doc_ids: list[str], k: int) -> dict[str, float]:
    """Compute ndcg/map/recall/precision/mrr for one query at cutoff k."""
    # Standard IR metrics computed at k (for system-to-system comparability).
    rel_set = {doc_id for doc_id, rel in relevant.items() if rel > 0}
    if not rel_set:
        return {"ndcg": 0.0, "map": 0.0, "recall": 0.0, "precision": 0.0, "mrr": 0.0}

    top_docs = ranked_doc_ids[:k]
    hits = [1 if doc in rel_set else 0 for doc in top_docs]

    graded_rels = [int(relevant.get(doc, 0)) for doc in top_docs]
    ideal_rels = sorted((int(x) for x in relevant.values()), reverse=True)
    idcg = _dcg_at_k(ideal_rels, k)
    ndcg = (_dcg_at_k(graded_rels, k) / idcg) if idcg > 0 else 0.0

    retrieved_relevant = sum(hits)
    precision = float(retrieved_relevant) / float(k) if k > 0 else 0.0
    recall = float(retrieved_relevant) / float(len(rel_set)) if rel_set else 0.0

    precisions = []
    running_hits = 0
    for rank, is_hit in enumerate(hits, start=1):
        if is_hit:
            running_hits += 1
            precisions.append(float(running_hits) / float(rank))
    denom = min(len(rel_set), k)
    ap = (sum(precisions) / float(denom)) if denom > 0 else 0.0

    mrr = 0.0
    for rank, is_hit in enumerate(hits, start=1):
        if is_hit:
            mrr = 1.0 / float(rank)
            break

    return {
        "ndcg": ndcg,
        "map": ap,
        "recall": recall,
        "precision": precision,
        "mrr": mrr,
    }


def _mean(values: list[float]) -> float:
    """Return arithmetic mean; 0.0 for empty input."""
    return (sum(values) / float(len(values))) if values else 0.0


def _build_token_counter(encoding_name: str) -> Callable[[str], int] | None:
    """Create a token-count function from a tiktoken encoding name, or None if unavailable."""
    try:
        import tiktoken  # type: ignore

        enc = tiktoken.get_encoding(encoding_name)
        return lambda text: len(enc.encode(text or ""))
    except Exception:
        return None


def _baseline_tokens_for_query(
    qid: str,
    qrels: dict[str, dict[str, int]],
    corpus: dict[str, str],
    count_tokens: Callable[[str], int],
) -> int:
    """Compute oracle token baseline: sum tokens of all qrels-relevant docs for a query."""
    # Oracle reference: assumes perfect knowledge of relevant docs from qrels.
    # Useful as a lower bound, not a deploy-time baseline.
    total = 0
    for doc_id, rel in (qrels.get(qid, {}) or {}).items():
        if int(rel) <= 0:
            continue
        text = corpus.get(str(doc_id), "")
        if text:
            total += int(count_tokens(text))
    return total


def _contextcore_tokens_for_query(
    results: list[dict[str, Any]],
    corpus: dict[str, str],
    count_tokens: Callable[[str], int],
    context_top_k: int,
) -> int:
    """Compute token cost of returned chunk payloads for top-N results."""
    # ContextCore cost: token count of returned chunk payloads (LLM-facing context).
    total = 0
    for row in results[: max(1, int(context_top_k))]:
        chunk = str(row.get("chunk") or "").strip()
        if chunk:
            total += int(count_tokens(chunk))
            continue

        # Fallback to full document text only if chunk text is unavailable.
        filename = str(row.get("filename") or "")
        doc_id = Path(filename).stem if filename else ""
        doc_text = corpus.get(doc_id, "") if doc_id else ""
        if doc_text:
            total += int(count_tokens(doc_text))
    return total


def _retrieved_full_docs_tokens_for_query(
    results: list[dict[str, Any]],
    corpus: dict[str, str],
    count_tokens: Callable[[str], int],
    docs_top_k: int,
) -> int:
    """Compute practical baseline: top-N retrieved documents counted as full-text context."""
    # Practical baseline: same retrieved candidates, but with full-document context.
    # This is the apples-to-apples baseline for token reduction claims.
    total = 0
    seen: set[str] = set()
    collected = 0

    for row in results:
        filename = str(row.get("filename") or "")
        doc_id = Path(filename).stem if filename else ""
        if not doc_id or doc_id in seen:
            continue
        seen.add(doc_id)

        doc_text = corpus.get(doc_id, "")
        if doc_text:
            total += int(count_tokens(doc_text))
        collected += 1
        if collected >= max(1, int(docs_top_k)):
            break

    return total


def _token_reduction_percent(baseline_tokens: int, context_tokens: int) -> float:
    """Return percentage token reduction relative to a baseline token count."""
    if baseline_tokens <= 0:
        return 0.0
    return ((float(baseline_tokens) - float(context_tokens)) / float(baseline_tokens)) * 100.0


def _search_mode(
    engine: Any,
    query: str,
    top_k: int,
    retrieval_mode: str,
    max_chunks_per_doc: int,
    max_context_tokens_per_result: int | None,
) -> list[dict[str, Any]]:
    """Run the text engine in a specific retrieval mode and normalize output shape."""
    # Unified adapter for all benchmark systems so output shaping stays identical.
    rows = engine.search(
        query=query,
        top_k=top_k,
        include_metadata=True,
        retrieval_mode=retrieval_mode,
        max_chunks_per_doc=max_chunks_per_doc,
        max_context_tokens_per_result=max_context_tokens_per_result,
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        filename = str(row.get("filename") or "")
        doc_id = Path(filename).stem if filename else ""
        if not doc_id:
            continue
        out.append(
            {
                "doc_id": doc_id,
                "score": float(row.get("score", 0.0)),
                "filename": filename,
                "chunk": str(row.get("chunk") or ""),
            }
        )
    return out


def _write_comparison_reports(
    summaries: dict[str, dict[str, Any]],
    report_csv: str | None = None,
    report_md: str | None = None,
) -> list[str]:
    """Write optional CSV/Markdown comparison tables from per-system summaries."""
    # Export concise publication-ready comparison tables from per-system summaries.
    created: list[str] = []
    rows: list[dict[str, Any]] = []
    for system, s in summaries.items():
        tb = s.get("token_benchmark") or {}
        rows.append(
            {
                "system": system,
                "ndcg@k": float(s.get("ndcg@k", 0.0)),
                "map@k": float(s.get("map@k", 0.0)),
                "recall@k": float(s.get("recall@k", 0.0)),
                "precision@k": float(s.get("precision@k", 0.0)),
                "mrr@k": float(s.get("mrr@k", 0.0)),
                "avg_tokens_retrieved_full_docs": float(tb.get("average_retrieved_full_docs_baseline_tokens_per_query", 0.0)),
                "avg_tokens_contextcore": float(tb.get("average_contextcore_tokens_per_query", 0.0)),
                "token_reduction_overall_vs_retrieved_full_docs_percent": float(
                    tb.get("overall_reduction_vs_retrieved_full_docs_percent", 0.0)
                ),
            }
        )

    if report_csv:
        csv_path = Path(report_csv).expanduser().resolve()
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        with csv_path.open("w", encoding="utf-8", newline="") as fh:
            fieldnames = list(rows[0].keys()) if rows else ["system"]
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        created.append(str(csv_path))

    if report_md:
        md_path = Path(report_md).expanduser().resolve()
        md_path.parent.mkdir(parents=True, exist_ok=True)
        headers = [
            "System",
            "NDCG@k",
            "MAP@k",
            "Recall@k",
            "Precision@k",
            "MRR@k",
            "Avg Tokens (Full Docs)",
            "Avg Tokens (Chunks)",
            "Token Reduction Overall (%)",
        ]
        lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
        for row in rows:
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(row["system"]),
                        f"{row['ndcg@k']:.4f}",
                        f"{row['map@k']:.4f}",
                        f"{row['recall@k']:.4f}",
                        f"{row['precision@k']:.4f}",
                        f"{row['mrr@k']:.4f}",
                        f"{row['avg_tokens_retrieved_full_docs']:.1f}",
                        f"{row['avg_tokens_contextcore']:.1f}",
                        f"{row['token_reduction_overall_vs_retrieved_full_docs_percent']:.2f}",
                    ]
                )
                + " |"
            )
        md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        created.append(str(md_path))

    return created


def _parse_systems(raw: str | None) -> list[str]:
    """Parse and normalize requested system names, including backward-compatible aliases."""
    # Keep backward compatibility with older CLI aliases.
    allowed = {"contextcore_hybrid", "bm25_only", "trigram_only"}
    parts = [p.strip().lower() for p in str(raw or "").split(",") if p.strip()]
    if not parts:
        return ["contextcore_hybrid", "bm25_only", "trigram_only"]

    aliases = {
        "contextcore": "contextcore_hybrid",
        "bm25": "bm25_only",
        "trigram": "trigram_only",
    }

    out: list[str] = []
    for p in parts:
        p = aliases.get(p, p)
        if p not in allowed:
            raise ValueError(
                f"Unsupported system '{p}'. Supported: contextcore_hybrid,bm25_only,trigram_only"
            )
        if p not in out:
            out.append(p)
    return out


def run_benchmark(
    dataset: str = "scifact",
    top_k: int = 10,
    max_queries: int = 0,
    datasets_dir: str | None = None,
    output_json: str | None = None,
    measure_tokens: bool = False,
    token_encoding: str = "cl100k_base",
    context_top_k: int | None = None,
    systems: str = "contextcore_hybrid,bm25_only,trigram_only",
    report_csv: str | None = None,
    report_md: str | None = None,
) -> None:
    """Execute BEIR retrieval benchmark with optional token-cost and report exports."""
    header("ContextCore Benchmark")

    if top_k <= 0:
        error("top_k must be > 0")
        return

    effective_context_top_k = int(context_top_k) if context_top_k and context_top_k > 0 else int(top_k)
    try:
        selected_systems = _parse_systems(systems)
    except ValueError as exc:
        error(str(exc))
        return

    base_datasets_dir = Path(datasets_dir).expanduser().resolve() if datasets_dir else (Path.cwd() / "datasets")
    section("Dataset")
    info(f"Dataset: [bold]{dataset}[/bold]")
    info(f"Dataset root: [bold]{base_datasets_dir}[/bold]")
    info(f"Systems: [bold]{', '.join(selected_systems)}[/bold]")

    try:
        dataset_path = _download_and_unzip_dataset(dataset=dataset, datasets_root=base_datasets_dir)
    except Exception as exc:
        error(f"Failed to download dataset: {exc}")
        warning("Install BEIR for the most reliable downloader: pip install beir")
        return

    corpus_path = dataset_path / "corpus.jsonl"
    queries_path = dataset_path / "queries.jsonl"
    qrels_path = dataset_path / "qrels" / "test.tsv"

    missing = [p for p in (corpus_path, queries_path, qrels_path) if not p.exists()]
    if missing:
        error("Dataset is incomplete. Missing files:")
        for p in missing:
            console.print(f"  - {p}")
        return

    section("Load Data")
    corpus = _load_corpus(corpus_path)
    queries = _load_queries(queries_path)
    qrels = _load_qrels(qrels_path)
    info(f"Corpus docs: [bold]{len(corpus):,}[/bold]")
    info(f"Queries: [bold]{len(queries):,}[/bold]")
    info(f"Qrels queries: [bold]{len(qrels):,}[/bold]")

    token_counter: Callable[[str], int] | None = None
    token_measurement_enabled = bool(measure_tokens)
    if token_measurement_enabled:
        section("Token Setup")
        token_counter = _build_token_counter(token_encoding)
        if token_counter is None:
            warning("Token measurement requested but tiktoken is unavailable.")
            warning("Install with: pip install tiktoken")
            token_measurement_enabled = False
        else:
            info(f"Tokenizer encoding: [bold]{token_encoding}[/bold]")
            info("Oracle baseline: sum(tokens of all qrels-relevant docs)")
            info(f"Practical baseline: sum(tokens of top-{effective_context_top_k} retrieved full docs)")
            info(f"ContextCore: sum(tokens of top-{effective_context_top_k} retrieved chunks)")

    eval_qids = [qid for qid in qrels.keys() if qid in queries]
    if max_queries and max_queries > 0:
        eval_qids = eval_qids[:max_queries]

    if not eval_qids:
        error("No overlapping query ids between queries.jsonl and qrels/test.tsv")
        return

    section("Index Corpus")
    # Isolated storage prevents benchmark runs from modifying user index state.
    temp_root = Path(tempfile.mkdtemp(prefix=f"contextcore_beir_{dataset}_"))
    isolated_storage = temp_root / "storage"
    isolated_storage.mkdir(parents=True, exist_ok=True)
    info(f"Using isolated storage: [bold]{isolated_storage}[/bold]")

    original_storage_env = os.environ.get("CONTEXTCORE_STORAGE_DIR")
    os.environ["CONTEXTCORE_STORAGE_DIR"] = str(isolated_storage)

    try:
        inserted = _index_corpus(corpus, category=f"beir_{dataset}")
        success(f"Indexed documents: {inserted:,}")

        from text_search_implementation_v2.search import TextSearchEngineV2

        engine = TextSearchEngineV2()
        active_systems = list(selected_systems)
        # Trigram tokenizer is not guaranteed in all SQLite builds.
        if "trigram_only" in active_systems and not bool(getattr(engine, "_trigram_enabled", False)):
            warning("trigram_only requested but trigram tokenizer is unavailable in this SQLite build; skipping trigram_only.")
            active_systems = [s for s in active_systems if s != "trigram_only"]
        if not active_systems:
            error("No active retrieval systems to evaluate.")
            return

        section("Evaluate")
        system_summaries: dict[str, dict[str, Any]] = {}
        system_runs: dict[str, dict[str, Any]] = {}

        search_fns: dict[str, Callable[[str, int], list[dict[str, Any]]]] = {
            # Retrieval mode changes candidate generation; chunk packaging stays identical.
            "contextcore_hybrid": lambda q, k: _search_mode(
                engine,
                q,
                k,
                retrieval_mode="contextcore_hybrid",
                max_chunks_per_doc=1,
                max_context_tokens_per_result=None,
            ),
            "bm25_only": lambda q, k: _search_mode(
                engine,
                q,
                k,
                retrieval_mode="bm25_only",
                max_chunks_per_doc=1,
                max_context_tokens_per_result=None,
            ),
            "trigram_only": lambda q, k: _search_mode(
                engine,
                q,
                k,
                retrieval_mode="trigram_only",
                max_chunks_per_doc=1,
                max_context_tokens_per_result=None,
            ),
        }

        total = len(eval_qids)
        for system_name in active_systems:
            # Per-system accumulators for quality and token-efficiency reporting.
            ndcgs: list[float] = []
            maps: list[float] = []
            recalls: list[float] = []
            precisions: list[float] = []
            mrrs: list[float] = []
            queries_with_hits = 0
            run_rows: dict[str, Any] = {}

            oracle_baseline_token_totals: list[int] = []
            retrieved_full_doc_token_totals: list[int] = []
            contextcore_token_totals: list[int] = []
            reduction_vs_oracle_per_query: list[float] = []
            reduction_vs_retrieved_full_docs_per_query: list[float] = []

            search_fn = search_fns[system_name]
            for i, qid in enumerate(eval_qids, start=1):
                qtext = queries[qid]
                results = search_fn(qtext, top_k)

                scored_docs: dict[str, float] = {}
                for row in results:
                    doc_id = str(row.get("doc_id") or "").strip()
                    if not doc_id:
                        continue
                    scored_docs[doc_id] = float(row.get("score", 0.0))

                ranked_doc_ids = [doc for doc, _ in sorted(scored_docs.items(), key=lambda x: x[1], reverse=True)]
                if ranked_doc_ids:
                    queries_with_hits += 1

                row_payload: dict[str, Any] = {
                    "scores": scored_docs,
                    "ranked_doc_ids": ranked_doc_ids,
                }

                if token_measurement_enabled and token_counter is not None:
                # Persist both views so downstream reporting can choose correct comparator.
                    # Track both token comparisons:
                    # 1) oracle (reference lower bound), 2) retrieved full docs (publishable baseline).
                    oracle_baseline_tok = _baseline_tokens_for_query(
                        qid=qid,
                        qrels=qrels,
                        corpus=corpus,
                        count_tokens=token_counter,
                    )
                    retrieved_full_doc_tok = _retrieved_full_docs_tokens_for_query(
                        results=results,
                        corpus=corpus,
                        count_tokens=token_counter,
                        docs_top_k=effective_context_top_k,
                    )
                    context_tok = _contextcore_tokens_for_query(
                        results=results,
                        corpus=corpus,
                        count_tokens=token_counter,
                        context_top_k=effective_context_top_k,
                    )
                    reduction_vs_oracle = _token_reduction_percent(oracle_baseline_tok, context_tok)
                    reduction_vs_retrieved_full_docs = _token_reduction_percent(retrieved_full_doc_tok, context_tok)

                    oracle_baseline_token_totals.append(int(oracle_baseline_tok))
                    retrieved_full_doc_token_totals.append(int(retrieved_full_doc_tok))
                    contextcore_token_totals.append(int(context_tok))
                    reduction_vs_oracle_per_query.append(float(reduction_vs_oracle))
                    reduction_vs_retrieved_full_docs_per_query.append(float(reduction_vs_retrieved_full_docs))

                    row_payload["tokens"] = {
                        "oracle_baseline": int(oracle_baseline_tok),
                        "retrieved_full_docs_baseline": int(retrieved_full_doc_tok),
                        "contextcore": int(context_tok),
                        "reduction_vs_oracle_percent": float(reduction_vs_oracle),
                        "reduction_vs_retrieved_full_docs_percent": float(reduction_vs_retrieved_full_docs),
                    }

                run_rows[qid] = row_payload

                m = _metrics_for_query(qrels[qid], ranked_doc_ids, top_k)
                ndcgs.append(m["ndcg"])
                maps.append(m["map"])
                recalls.append(m["recall"])
                precisions.append(m["precision"])
                mrrs.append(m["mrr"])

                if i % 50 == 0 or i == total:
                    info(f"{system_name}: Evaluated {i}/{total} queries")

            summary = {
                "system": system_name,
                "dataset": dataset,
                "top_k": int(top_k),
                "evaluated_queries": len(eval_qids),
                "queries_with_hits": int(queries_with_hits),
                "ndcg@k": _mean(ndcgs),
                "map@k": _mean(maps),
                "recall@k": _mean(recalls),
                "precision@k": _mean(precisions),
                "mrr@k": _mean(mrrs),
                "dataset_path": str(dataset_path),
                "isolated_storage": str(isolated_storage),
            }

            if token_measurement_enabled and token_counter is not None:
                total_oracle_baseline = int(sum(oracle_baseline_token_totals))
                total_retrieved_full_docs_baseline = int(sum(retrieved_full_doc_token_totals))
                total_contextcore = int(sum(contextcore_token_totals))
                summary["token_benchmark"] = {
                    "enabled": True,
                    "encoding": token_encoding,
                    "context_top_k": int(effective_context_top_k),
                    "oracle_baseline_total_tokens": total_oracle_baseline,
                    "retrieved_full_docs_baseline_total_tokens": total_retrieved_full_docs_baseline,
                    "contextcore_total_tokens": total_contextcore,
                    "average_oracle_baseline_tokens_per_query": _mean([float(x) for x in oracle_baseline_token_totals]),
                    "average_retrieved_full_docs_baseline_tokens_per_query": _mean(
                        [float(x) for x in retrieved_full_doc_token_totals]
                    ),
                    "average_contextcore_tokens_per_query": _mean([float(x) for x in contextcore_token_totals]),
                    "average_reduction_vs_oracle_percent": _mean(reduction_vs_oracle_per_query),
                    "average_reduction_vs_retrieved_full_docs_percent": _mean(
                        reduction_vs_retrieved_full_docs_per_query
                    ),
                    "overall_reduction_vs_oracle_percent": _token_reduction_percent(
                        total_oracle_baseline, total_contextcore
                    ),
                    "overall_reduction_vs_retrieved_full_docs_percent": _token_reduction_percent(
                        total_retrieved_full_docs_baseline, total_contextcore
                    ),
                }

            system_summaries[system_name] = summary
            system_runs[system_name] = run_rows

        section("Results")
        for system_name in active_systems:
            summary = system_summaries[system_name]
            info(f"[bold]{system_name}[/bold]")
            success(f"NDCG@{top_k}: {summary['ndcg@k']:.4f}")
            success(f"MAP@{top_k}: {summary['map@k']:.4f}")
            success(f"Recall@{top_k}: {summary['recall@k']:.4f}")
            success(f"Precision@{top_k}: {summary['precision@k']:.4f}")
            success(f"MRR@{top_k}: {summary['mrr@k']:.4f}")
            info(f"Queries evaluated: [bold]{summary['evaluated_queries']}[/bold]")
            info(f"Queries with >=1 hit: [bold]{summary['queries_with_hits']}[/bold]")

            tb = summary.get("token_benchmark", {})
            if tb.get("enabled"):
                success(
                    "Token Reduction vs Retrieved Full Docs (avg): "
                    f"{float(tb['average_reduction_vs_retrieved_full_docs_percent']):.2f}%"
                )
                success(
                    "Token Reduction vs Retrieved Full Docs (overall): "
                    f"{float(tb['overall_reduction_vs_retrieved_full_docs_percent']):.2f}%"
                )
                info(
                    "Avg tokens/query (retrieved full docs -> contextcore): "
                    f"[bold]{float(tb['average_retrieved_full_docs_baseline_tokens_per_query']):.1f}[/bold] -> "
                    f"[bold]{float(tb['average_contextcore_tokens_per_query']):.1f}[/bold]"
                )
                info(
                    "Oracle reference reduction (avg / overall): "
                    f"[bold]{float(tb['average_reduction_vs_oracle_percent']):.2f}%[/bold] / "
                    f"[bold]{float(tb['overall_reduction_vs_oracle_percent']):.2f}%[/bold]"
                )

        report_paths = _write_comparison_reports(
            summaries=system_summaries,
            report_csv=report_csv,
            report_md=report_md,
        )
        for p in report_paths:
            success(f"Saved comparison report: {p}")

        if output_json:
            # JSON keeps an audit trail: summary metrics plus per-query scored runs.
            output_path = Path(output_json).expanduser().resolve()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            payload: dict[str, Any] = {
                "systems_summary": system_summaries,
                "systems_run": system_runs,
                "meta": {
                    "dataset": dataset,
                    "top_k": int(top_k),
                    "selected_systems": selected_systems,
                    "active_systems": active_systems,
                },
            }
            if len(active_systems) == 1:
                single = active_systems[0]
                payload["summary"] = system_summaries[single]
                payload["run"] = system_runs[single]
            output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            success(f"Saved benchmark report: {output_path}")

    except Exception as exc:
        error(f"Benchmark failed: {exc}")
    finally:
        if original_storage_env is None:
            os.environ.pop("CONTEXTCORE_STORAGE_DIR", None)
        else:
            os.environ["CONTEXTCORE_STORAGE_DIR"] = original_storage_env
        shutil.rmtree(temp_root, ignore_errors=True)
