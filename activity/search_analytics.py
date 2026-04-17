from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from config import get_storage_dir


def _db_path() -> Path:
    root = get_storage_dir() / "activity"
    root.mkdir(parents=True, exist_ok=True)
    return root / "search_analytics.db"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS file_search_hits (
            source_path TEXT PRIMARY KEY,
            hit_count INTEGER NOT NULL DEFAULT 0,
            last_seen_utc TEXT NOT NULL
        )
        """
    )
    return conn


def record_search_hits(paths: list[str]) -> None:
    cleaned = [p.strip() for p in paths if isinstance(p, str) and p.strip()]
    if not cleaned:
        return
    unique = list(dict.fromkeys(cleaned))
    now = datetime.now(timezone.utc).isoformat()
    conn = _conn()
    with conn:
        for path in unique:
            conn.execute(
                """
                INSERT INTO file_search_hits(source_path, hit_count, last_seen_utc)
                VALUES (?, 1, ?)
                ON CONFLICT(source_path) DO UPDATE SET
                    hit_count = hit_count + 1,
                    last_seen_utc = excluded.last_seen_utc
                """,
                (path, now),
            )
    conn.close()


def top_searched_files(limit: int = 3) -> list[tuple[str, int]]:
    limit = max(1, min(int(limit), 20))
    conn = _conn()
    try:
        rows = conn.execute(
            """
            SELECT source_path, hit_count
            FROM file_search_hits
            ORDER BY hit_count DESC, last_seen_utc DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [(str(r["source_path"]), int(r["hit_count"])) for r in rows]
    finally:
        conn.close()
