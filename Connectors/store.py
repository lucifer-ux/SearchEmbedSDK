from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import get_storage_dir


def _resolve_data_dir() -> Path:
    preferred = get_storage_dir() / "connectors" / "storage"
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        return preferred
    except OSError:
        fallback = Path(__file__).resolve().parent.parent / ".contextcore" / "connectors" / "storage"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


DATA_DIR = _resolve_data_dir()
DB_PATH = DATA_DIR / "connectors.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_db() -> None:
    conn = get_conn()
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS connector_accounts (
                provider TEXT NOT NULL,
                account_id TEXT NOT NULL,
                display_name TEXT,
                auth_mode TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                last_validated_at TEXT NOT NULL,
                PRIMARY KEY (provider, account_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS connector_sync_state (
                provider TEXT NOT NULL,
                account_id TEXT NOT NULL,
                external_id TEXT NOT NULL,
                object_type TEXT NOT NULL,
                uri TEXT NOT NULL,
                title TEXT,
                url TEXT,
                parent_id TEXT,
                container_id TEXT,
                last_edited_time TEXT,
                content_hash TEXT,
                last_seen_at TEXT NOT NULL,
                last_synced_at TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                raw_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (provider, account_id, external_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS connector_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uri TEXT NOT NULL UNIQUE,
                provider TEXT NOT NULL,
                account_id TEXT NOT NULL,
                external_id TEXT NOT NULL,
                object_type TEXT NOT NULL,
                title TEXT NOT NULL,
                url TEXT,
                updated_at TEXT,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                raw_json TEXT NOT NULL DEFAULT '{}',
                indexed_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS connector_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL,
                uri TEXT NOT NULL,
                provider TEXT NOT NULL,
                title TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                start_offset INTEGER NOT NULL,
                end_offset INTEGER NOT NULL,
                chunk_text TEXT NOT NULL,
                FOREIGN KEY(document_id) REFERENCES connector_documents(id) ON DELETE CASCADE,
                UNIQUE(uri, chunk_index)
            )
            """
        )
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS connector_chunks_fts USING fts5(
                title, chunk_text, content='connector_chunks', content_rowid='id', tokenize='porter'
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_connector_docs_provider ON connector_documents(provider)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_connector_state_status ON connector_sync_state(provider, account_id, status)"
        )
    conn.close()


def _json_dumps(value: Any) -> str:
    if is_dataclass(value):
        value = asdict(value)
    return json.dumps(value if value is not None else {}, separators=(",", ":"))


def _split_chunks(text: str, chunk_chars: int = 900, chunk_overlap: int = 120) -> list[dict[str, Any]]:
    if not text:
        return []
    step = max(1, int(chunk_chars) - int(chunk_overlap))
    out: list[dict[str, Any]] = []
    i = 0
    idx = 0
    while i < len(text):
        end = min(len(text), i + int(chunk_chars))
        chunk = text[i:end].strip()
        if chunk:
            out.append(
                {
                    "index": idx,
                    "start": i,
                    "end": end,
                    "text": chunk,
                }
            )
            idx += 1
        if end >= len(text):
            break
        i += step
    return out


def upsert_account(
    provider: str,
    account_id: str,
    display_name: str,
    auth_mode: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    init_db()
    conn = get_conn()
    with conn:
        conn.execute(
            """
            INSERT INTO connector_accounts(provider, account_id, display_name, auth_mode, metadata_json, last_validated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, account_id) DO UPDATE SET
                display_name=excluded.display_name,
                auth_mode=excluded.auth_mode,
                metadata_json=excluded.metadata_json,
                last_validated_at=excluded.last_validated_at
            """,
            (
                provider,
                account_id,
                display_name,
                auth_mode,
                _json_dumps(metadata),
                _now_iso(),
            ),
        )
    conn.close()


def get_sync_state(provider: str, account_id: str, external_id: str) -> dict[str, Any] | None:
    init_db()
    conn = get_conn()
    try:
        row = conn.execute(
            """
            SELECT *
            FROM connector_sync_state
            WHERE provider = ? AND account_id = ? AND external_id = ?
            """,
            (provider, account_id, external_id),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_known_objects(
    provider: str,
    account_id: str,
    *,
    object_types: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    init_db()
    conn = get_conn()
    try:
        params: list[Any] = [provider, account_id]
        sql = """
            SELECT external_id, object_type, uri, title, url, parent_id, container_id, last_edited_time
            FROM connector_sync_state
            WHERE provider = ? AND account_id = ? AND status != 'deleted'
        """
        if object_types:
            placeholders = ",".join("?" for _ in object_types)
            sql += f" AND object_type IN ({placeholders})"
            params.extend(object_types)
        sql += " ORDER BY last_seen_at DESC"
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def upsert_document_state(document: Any) -> str:
    init_db()
    conn = get_conn()
    try:
        with conn:
            existing = conn.execute(
                "SELECT id, content_hash, updated_at FROM connector_documents WHERE uri = ?",
                (document.uri,),
            ).fetchone()
            now = _now_iso()
            if existing and existing["content_hash"] == document.content_hash and (
                (existing["updated_at"] or "") == (document.updated_at or "")
            ):
                conn.execute(
                    """
                    INSERT INTO connector_sync_state(
                        provider, account_id, external_id, object_type, uri, title, url, parent_id, container_id,
                        last_edited_time, content_hash, last_seen_at, last_synced_at, status, error, metadata_json, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL, ?, ?)
                    ON CONFLICT(provider, account_id, external_id) DO UPDATE SET
                        object_type=excluded.object_type,
                        uri=excluded.uri,
                        title=excluded.title,
                        url=excluded.url,
                        parent_id=excluded.parent_id,
                        container_id=excluded.container_id,
                        last_edited_time=excluded.last_edited_time,
                        content_hash=excluded.content_hash,
                        last_seen_at=excluded.last_seen_at,
                        last_synced_at=excluded.last_synced_at,
                        status='active',
                        error=NULL,
                        metadata_json=excluded.metadata_json,
                        raw_json=excluded.raw_json
                    """,
                    (
                        document.provider,
                        document.account_id,
                        document.external_id,
                        document.object_type,
                        document.uri,
                        document.title,
                        document.url,
                        document.parent_id,
                        document.container_id,
                        document.updated_at,
                        document.content_hash,
                        now,
                        now,
                        _json_dumps(document.metadata),
                        document.raw_json,
                    ),
                )
                return "unchanged"

            if existing:
                doc_id = int(existing["id"])
                conn.execute(
                    """
                    UPDATE connector_documents
                    SET provider=?, account_id=?, external_id=?, object_type=?, title=?, url=?, updated_at=?,
                        content=?, content_hash=?, metadata_json=?, raw_json=?, indexed_at=?
                    WHERE id=?
                    """,
                    (
                        document.provider,
                        document.account_id,
                        document.external_id,
                        document.object_type,
                        document.title,
                        document.url,
                        document.updated_at,
                        document.content,
                        document.content_hash,
                        _json_dumps(document.metadata),
                        document.raw_json,
                        now,
                        doc_id,
                    ),
                )
                conn.execute(
                    "DELETE FROM connector_chunks_fts WHERE rowid IN (SELECT id FROM connector_chunks WHERE document_id = ?)",
                    (doc_id,),
                )
                conn.execute("DELETE FROM connector_chunks WHERE document_id = ?", (doc_id,))
                result = "updated"
            else:
                cur = conn.execute(
                    """
                    INSERT INTO connector_documents(
                        uri, provider, account_id, external_id, object_type, title, url, updated_at,
                        content, content_hash, metadata_json, raw_json, indexed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        document.uri,
                        document.provider,
                        document.account_id,
                        document.external_id,
                        document.object_type,
                        document.title,
                        document.url,
                        document.updated_at,
                        document.content,
                        document.content_hash,
                        _json_dumps(document.metadata),
                        document.raw_json,
                        now,
                    ),
                )
                doc_id = int(cur.lastrowid)
                result = "inserted"

            for chunk in _split_chunks(document.content):
                cur = conn.execute(
                    """
                    INSERT INTO connector_chunks(
                        document_id, uri, provider, title, chunk_index, start_offset, end_offset, chunk_text
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        doc_id,
                        document.uri,
                        document.provider,
                        document.title,
                        int(chunk["index"]),
                        int(chunk["start"]),
                        int(chunk["end"]),
                        chunk["text"],
                    ),
                )
                chunk_id = int(cur.lastrowid)
                conn.execute(
                    "INSERT INTO connector_chunks_fts(rowid, title, chunk_text) VALUES (?, ?, ?)",
                    (chunk_id, document.title, chunk["text"]),
                )

            conn.execute(
                """
                INSERT INTO connector_sync_state(
                    provider, account_id, external_id, object_type, uri, title, url, parent_id, container_id,
                    last_edited_time, content_hash, last_seen_at, last_synced_at, status, error, metadata_json, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL, ?, ?)
                ON CONFLICT(provider, account_id, external_id) DO UPDATE SET
                    object_type=excluded.object_type,
                    uri=excluded.uri,
                    title=excluded.title,
                    url=excluded.url,
                    parent_id=excluded.parent_id,
                    container_id=excluded.container_id,
                    last_edited_time=excluded.last_edited_time,
                    content_hash=excluded.content_hash,
                    last_seen_at=excluded.last_seen_at,
                    last_synced_at=excluded.last_synced_at,
                    status='active',
                    error=NULL,
                    metadata_json=excluded.metadata_json,
                    raw_json=excluded.raw_json
                """,
                (
                    document.provider,
                    document.account_id,
                    document.external_id,
                    document.object_type,
                    document.uri,
                    document.title,
                    document.url,
                    document.parent_id,
                    document.container_id,
                    document.updated_at,
                    document.content_hash,
                    now,
                    now,
                    _json_dumps(document.metadata),
                    document.raw_json,
                ),
            )

            return result
    finally:
        conn.close()


def mark_deleted(
    provider: str,
    account_id: str,
    external_id: str,
    *,
    error: str | None = None,
) -> None:
    init_db()
    conn = get_conn()
    try:
        with conn:
            row = conn.execute(
                """
                SELECT uri
                FROM connector_sync_state
                WHERE provider=? AND account_id=? AND external_id=?
                """,
                (provider, account_id, external_id),
            ).fetchone()
            now = _now_iso()
            conn.execute(
                """
                UPDATE connector_sync_state
                SET status='deleted', error=?, last_synced_at=?, last_seen_at=?
                WHERE provider=? AND account_id=? AND external_id=?
                """,
                (error, now, now, provider, account_id, external_id),
            )
            if row:
                conn.execute("DELETE FROM connector_chunks_fts WHERE rowid IN (SELECT id FROM connector_chunks WHERE uri = ?)", (row["uri"],))
                conn.execute("DELETE FROM connector_chunks WHERE uri = ?", (row["uri"],))
                conn.execute("DELETE FROM connector_documents WHERE uri = ?", (row["uri"],))
    finally:
        conn.close()


def search_connector_documents(
    query: str,
    top_k: int = 20,
    exclude_sources: set[str] | None = None,
) -> list[dict[str, Any]]:
    init_db()
    q = (query or "").strip()
    if not q:
        return []
    tokens = re.findall(r"\b\w+\b", q.lower())
    if not tokens:
        return []
    match_q = " OR ".join(f"{t}*" for t in tokens)
    excluded = {str(x) for x in (exclude_sources or set())}

    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT c.id, c.uri, c.provider, c.title, c.chunk_index, c.chunk_text, d.url, d.updated_at,
                   bm25(connector_chunks_fts) AS score
            FROM connector_chunks_fts
            JOIN connector_chunks c ON c.id = connector_chunks_fts.rowid
            JOIN connector_documents d ON d.id = c.document_id
            WHERE connector_chunks_fts MATCH ?
            ORDER BY score
            LIMIT ?
            """,
            (match_q, max(20, int(top_k) * 8)),
        ).fetchall()

        by_uri: dict[str, dict[str, Any]] = {}
        for row in rows:
            uri = str(row["uri"])
            if uri in excluded:
                continue
            rank_score = max(0.0, -float(row["score"]))
            title = str(row["title"] or "")
            text = str(row["chunk_text"] or "")
            title_bonus = 2.5 if q.lower() in title.lower() else 0.0
            text_bonus = 1.5 if q.lower() in text.lower() else 0.0
            score = rank_score + title_bonus + text_bonus
            existing = by_uri.get(uri)
            shaped = {
                "path": uri,
                "filename": title or uri,
                "category": "connector_text",
                "provider": row["provider"],
                "source": row["provider"],
                "score": float(score),
                "chunk": text,
                "chunk_index": int(row["chunk_index"] or 0),
                "url": row["url"],
                "updated_at": row["updated_at"],
                "matched_chunks": 1,
            }
            if existing is None or float(shaped["score"]) > float(existing["score"]):
                if existing:
                    shaped["matched_chunks"] = int(existing.get("matched_chunks", 1))
                by_uri[uri] = shaped
            else:
                existing["matched_chunks"] = int(existing.get("matched_chunks", 1)) + 1

        out = list(by_uri.values())
        out.sort(key=lambda x: float(x.get("score", 0.0)), reverse=True)
        return out[: max(1, int(top_k))]
    finally:
        conn.close()


def fetch_document_by_uri(uri: str) -> dict[str, Any] | None:
    init_db()
    conn = get_conn()
    try:
        row = conn.execute(
            """
            SELECT uri, provider, title, url, updated_at, content, metadata_json, raw_json
            FROM connector_documents
            WHERE uri = ?
            """,
            (uri,),
        ).fetchone()
        if not row:
            return None
        return {
            "uri": row["uri"],
            "provider": row["provider"],
            "title": row["title"],
            "url": row["url"],
            "updated_at": row["updated_at"],
            "content": row["content"],
            "metadata": json.loads(row["metadata_json"] or "{}"),
            "raw_json": row["raw_json"],
        }
    finally:
        conn.close()


def get_provider_counts() -> dict[str, int]:
    init_db()
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT provider, COUNT(*) AS c
            FROM connector_documents
            GROUP BY provider
            ORDER BY provider
            """
        ).fetchall()
        return {str(r["provider"]): int(r["c"]) for r in rows}
    finally:
        conn.close()


def count_connector_documents() -> int:
    init_db()
    conn = get_conn()
    try:
        row = conn.execute("SELECT COUNT(*) AS c FROM connector_documents").fetchone()
        return int(row["c"] if row else 0)
    finally:
        conn.close()
