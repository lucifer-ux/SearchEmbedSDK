from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any

from cloud_text_search_implementation.db import (
    ANNOY_INDEX_PATH,
    ANNOY_STATE_PATH,
    count_cloud_chunks,
    get_all_chunk_vectors,
)
from cloud_text_search_implementation.embeddings import VECTOR_DIM

_LOCK = threading.Lock()
_ANNOY_INDEX = None
_ANNOY_LOADED = False


def annoy_installed() -> bool:
    try:
        import annoy  # noqa: F401
        return True
    except Exception:
        return False


def _default_state() -> dict[str, Any]:
    return {"needs_rebuild": False, "last_rebuild_at": None}


def load_state() -> dict[str, Any]:
    if not ANNOY_STATE_PATH.exists():
        return _default_state()
    try:
        data = json.loads(ANNOY_STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _default_state()
        return {
            "needs_rebuild": bool(data.get("needs_rebuild", False)),
            "last_rebuild_at": data.get("last_rebuild_at"),
        }
    except Exception:
        return _default_state()


def save_state(state: dict[str, Any]) -> None:
    ANNOY_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    ANNOY_STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def mark_dirty() -> None:
    state = load_state()
    state["needs_rebuild"] = True
    save_state(state)


def clear_dirty() -> None:
    state = load_state()
    state["needs_rebuild"] = False
    state["last_rebuild_at"] = datetime.now(timezone.utc).isoformat()
    save_state(state)


def _build_empty_index():
    from annoy import AnnoyIndex

    global _ANNOY_INDEX, _ANNOY_LOADED
    _ANNOY_INDEX = AnnoyIndex(VECTOR_DIM, "angular")
    _ANNOY_LOADED = True


def _load_index_from_disk() -> bool:
    if not annoy_installed():
        return False

    from annoy import AnnoyIndex

    global _ANNOY_INDEX, _ANNOY_LOADED
    if not ANNOY_INDEX_PATH.exists():
        _build_empty_index()
        return False

    ai = AnnoyIndex(VECTOR_DIM, "angular")
    ai.load(str(ANNOY_INDEX_PATH))
    _ANNOY_INDEX = ai
    _ANNOY_LOADED = True
    return True


def rebuild_annoy_index(n_trees: int = 10) -> dict[str, Any]:
    with _LOCK:
        if not annoy_installed():
            return {"ok": False, "error": "annoy_not_installed", "indexed_vectors": 0}

        from annoy import AnnoyIndex

        ai = AnnoyIndex(VECTOR_DIM, "angular")
        rows = get_all_chunk_vectors()
        added = 0
        for row in rows:
            chunk_id = int(row["chunk_id"])
            vec = row["vector"]
            if not isinstance(vec, list) or len(vec) != VECTOR_DIM:
                continue
            ai.add_item(chunk_id, [float(x) for x in vec])
            added += 1

        global _ANNOY_INDEX, _ANNOY_LOADED
        if added > 0:
            ai.build(max(2, int(n_trees)))
            ai.save(str(ANNOY_INDEX_PATH))
            _ANNOY_INDEX = ai
            _ANNOY_LOADED = True
        else:
            if ANNOY_INDEX_PATH.exists():
                ANNOY_INDEX_PATH.unlink(missing_ok=True)
            _build_empty_index()
        clear_dirty()
        return {"ok": True, "indexed_vectors": int(added), "index_exists": ANNOY_INDEX_PATH.exists()}


def ensure_annoy_ready() -> dict[str, Any]:
    state = load_state()
    if state.get("needs_rebuild") or not ANNOY_INDEX_PATH.exists():
        return rebuild_annoy_index()

    global _ANNOY_LOADED
    if not _ANNOY_LOADED:
        _load_index_from_disk()

    return {
        "ok": True,
        "indexed_vectors": int(count_cloud_chunks()),
        "index_exists": ANNOY_INDEX_PATH.exists(),
    }


def search_annoy(query_vector: list[float], top_k: int = 50) -> list[dict[str, float]]:
    if not annoy_installed():
        return []

    ensure_annoy_ready()
    if _ANNOY_INDEX is None:
        return []

    if not isinstance(query_vector, list) or len(query_vector) != VECTOR_DIM:
        return []

    try:
        ids, dists = _ANNOY_INDEX.get_nns_by_vector(
            [float(x) for x in query_vector],
            max(1, int(top_k)),
            include_distances=True,
        )
    except Exception:
        return []

    out = []
    for cid, dist in zip(ids, dists):
        semantic_score = max(0.0, float(1.0 - float(dist) / 2.0))
        out.append({"chunk_id": int(cid), "distance": float(dist), "semantic_score": semantic_score})
    return out


def get_annoy_status() -> dict[str, Any]:
    state = load_state()
    installed = annoy_installed()
    index_exists = ANNOY_INDEX_PATH.exists()
    needs_rebuild = bool(state.get("needs_rebuild", False))
    vector_count = int(count_cloud_chunks())
    ready = bool(installed and index_exists and not needs_rebuild and vector_count > 0)
    return {
        "installed": installed,
        "index_exists": index_exists,
        "needs_rebuild": needs_rebuild,
        "vector_ready_chunks": vector_count,
        "ready": ready,
        "last_rebuild_at": state.get("last_rebuild_at"),
    }
