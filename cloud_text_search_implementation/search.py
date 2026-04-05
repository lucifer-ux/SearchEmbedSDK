import re
import shutil
import subprocess
from pathlib import Path

from cloud_text_search_implementation.annoy_store import get_annoy_status, search_annoy
from cloud_text_search_implementation.db import (
    get_chunk_metadata_by_ids,
    init_db,
    normalize_query_for_fts,
    query_cloud_chunk_fts,
)
from cloud_text_search_implementation.embeddings import embed_text


def _get_rclone_path() -> str | None:
    system = shutil.which("rclone")
    if system:
        return system

    winget_base = Path.home() / "AppData/Local/Microsoft/WinGet/Packages"
    if winget_base.exists():
        for p in winget_base.rglob("rclone.exe"):
            return str(p)

    known_paths = [
        Path("C:/Program Files/rclone/rclone.exe"),
        Path("C:/Program Files (x86)/rclone/rclone.exe"),
    ]
    for p in known_paths:
        if p.exists():
            return str(p)
    return None


def _cloud_link(remote: str, path: str) -> str | None:
    rclone = _get_rclone_path()
    if not rclone:
        return None
    result = subprocess.run(
        [rclone, "link", f"{remote}:{path}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=6,
    )
    if result.returncode != 0:
        return None
    url = (result.stdout or "").strip()
    return url or None


def search_cloud_text(
    query: str,
    top_k: int = 20,
    exclude_sources: set[str] | None = None,
) -> list[dict]:
    init_db()
    q = (query or "").strip()
    if not q:
        return []

    excluded = {str(x) for x in (exclude_sources or set())}
    tokens = re.findall(r"\b\w+\b", q.lower())
    match_q = normalize_query_for_fts(q)
    fts_rows = query_cloud_chunk_fts(match_q, limit=max(50, top_k * 8)) if match_q else []

    lexical_rank: dict[int, float] = {}
    lexical_raw: dict[int, float] = {}
    for rank, row in enumerate(fts_rows):
        chunk_id = int(row["id"])
        lexical_rank[chunk_id] = max(0.0, 1.0 / float(rank + 1))
        lexical_raw[chunk_id] = float(-float(row["score"]))

    semantic_rank: dict[int, float] = {}
    semantic_raw: dict[int, float] = {}
    annoy_status = get_annoy_status()
    if annoy_status.get("ready"):
        qvec = embed_text(q)
        ann_hits = search_annoy(qvec, top_k=max(50, top_k * 8))
        for rank, hit in enumerate(ann_hits):
            chunk_id = int(hit["chunk_id"])
            semantic_rank[chunk_id] = max(0.0, 1.0 / float(rank + 1))
            semantic_raw[chunk_id] = float(hit.get("semantic_score", 0.0))

    candidate_ids = set(lexical_rank.keys()) | set(semantic_rank.keys())
    if not candidate_ids:
        return []

    meta_map = get_chunk_metadata_by_ids(sorted(candidate_ids))
    file_best: dict[tuple[str, str], dict] = {}
    for chunk_id in candidate_ids:
        meta = meta_map.get(chunk_id)
        if not meta:
            continue
        display_path = f"{meta['remote']}:{meta['path']}"
        if meta["path"] in excluded or display_path in excluded:
            continue

        lex = lexical_rank.get(chunk_id, 0.0)
        sem = semantic_rank.get(chunk_id, 0.0)
        score = (lex * 1.0) + (sem * 0.85)
        if tokens and lex <= 0.0 and sem <= 0.0:
            continue

        key = (meta["remote"], meta["path"])
        existing = file_best.get(key)
        row = {
            "path": display_path,
            "cloud_path": meta["path"],
            "remote": meta["remote"],
            "filename": meta["filename"],
            "category": "cloud_text",
            "score": float(score),
            "chunk": meta["chunk_text"],
            "chunk_index": int(meta["chunk_index"]),
            "semantic_score": float(semantic_raw.get(chunk_id, 0.0)),
            "bm25_score": float(lexical_raw.get(chunk_id, 0.0)),
            "source": "cloud",
            "matched_chunks": 1,
        }
        if existing is None:
            file_best[key] = row
        else:
            existing["matched_chunks"] = int(existing.get("matched_chunks", 1)) + 1
            if row["score"] > existing["score"]:
                row["matched_chunks"] = existing["matched_chunks"]
                file_best[key] = row

    out = list(file_best.values())
    out.sort(key=lambda x: x["score"], reverse=True)
    out = out[: max(1, int(top_k))]
    for row in out:
        row["cloud_url"] = _cloud_link(row["remote"], row["cloud_path"])
    return out
