# text_search_implementation_v2/db.py
import sqlite3
import sys
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
from config import get_storage_dir

DATA_DIR = get_storage_dir() / "text_search_implementation_v2" / "storage"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "text_search_implementation_v2.db"

def get_conn():
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    # pragma for performance / concurrency
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn

def init_db():
    conn = get_conn()
    with conn:
        # metadata table
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY,
                path TEXT UNIQUE,
                filename TEXT,
                category TEXT,
                mtime REAL,
                content TEXT
            )
            """
        )
        # Backward-compatible migration for old DBs that were missing content.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(files)").fetchall()}
        if "content" not in cols:
            conn.execute("ALTER TABLE files ADD COLUMN content TEXT")
        # FTS5 virtual table for content + filename
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
                filename, content, content='files', content_rowid='id', tokenize='porter'
            );
            """
        )
    conn.close()

# helper to upsert file metadata and fts content
def upsert_file(path: str, filename: str, category: str, mtime: float, content: str):
    conn = get_conn()
    with conn:
        row = conn.execute(
            "SELECT id, mtime FROM files WHERE path = ?",
            (path,),
        ).fetchone()
        if row and row["mtime"] >= mtime:
            return False

        if row:
            file_id = row["id"]
            conn.execute(
                "UPDATE files SET filename = ?, category = ?, mtime = ?, content = ? WHERE id = ?",
                (filename, category, mtime, content, file_id),
            )
            conn.execute("DELETE FROM files_fts WHERE rowid = ?", (file_id,))
        else:
            file_id = None
            cur = conn.execute(
                "INSERT OR IGNORE INTO files (path, filename, category, mtime, content) VALUES (?, ?, ?, ?, ?)",
                (path, filename, category, mtime, content),
            )
            file_id = cur.lastrowid or None
            if not file_id:
                row = conn.execute(
                    "SELECT id, mtime FROM files WHERE path = ?",
                    (path,),
                ).fetchone()
                if not row:
                    return False
                if row["mtime"] >= mtime:
                    return False
                file_id = row["id"]
                conn.execute(
                    "UPDATE files SET filename = ?, category = ?, mtime = ?, content = ? WHERE id = ?",
                    (filename, category, mtime, content, file_id),
                )
                conn.execute("DELETE FROM files_fts WHERE rowid = ?", (file_id,))

        # insert into fts
        conn.execute(
            "INSERT INTO files_fts(rowid, filename, content) VALUES (?, ?, ?)",
            (file_id, filename, content)
        )
    conn.close()
    return True

def query_fts(match_query: str, limit: int = 50):
    conn = get_conn()
    cur = conn.execute(
        "SELECT rowid as id, bm25(files_fts) as score FROM files_fts WHERE files_fts MATCH ? ORDER BY score LIMIT ?",
        (match_query, limit)
    )
    rows = cur.fetchall()
    conn.close()
    return rows

def get_file_metadata_by_ids(ids):
    if not ids:
        return []
    conn = get_conn()
    placeholders = ",".join("?" for _ in ids)
    cur = conn.execute(f"SELECT id, path, filename, category FROM files WHERE id IN ({placeholders})", ids)
    rows = cur.fetchall()
    conn.close()
    return {r["id"]: dict(r) for r in rows}


def get_fts_content_by_ids(ids):
    if not ids:
        return {}
    conn = get_conn()
    placeholders = ",".join("?" for _ in ids)
    cur = conn.execute(f"SELECT id, content FROM files WHERE id IN ({placeholders})", ids)
    rows = cur.fetchall()
    conn.close()
    return {int(r["id"]): (r["content"] or "") for r in rows}

def get_file_mtime(path: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("SELECT mtime FROM files WHERE path = ?", (path,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def delete_file_by_path_category(path: str, category: str) -> bool:
    conn = get_conn()
    with conn:
        row = conn.execute(
            "SELECT id FROM files WHERE path = ? AND category = ?",
            (path, category),
        ).fetchone()
        if not row:
            return False
        conn.execute("DELETE FROM files_fts WHERE rowid = ?", (row["id"],))
        conn.execute("DELETE FROM files WHERE id = ?", (row["id"],))
    conn.close()
    return True
