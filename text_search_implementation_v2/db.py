# text_search_implementation_v2/db.py
import os
import re
import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
from config import get_storage_dir, load_env_file

load_env_file()

DATA_DIR = get_storage_dir() / "text_search_implementation_v2" / "storage"
DB_PATH = DATA_DIR / "text_search_implementation_v2.db"


class _DictRow(dict):
    """Small row shim used by fallback queries so callers can use row["id"]."""


class _CompatCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    @property
    def lastrowid(self):
        return getattr(self._cursor, "lastrowid", None)

    def _column_names(self) -> list[str]:
        names = []
        for desc in getattr(self._cursor, "description", None) or []:
            if isinstance(desc, (tuple, list)) and desc:
                names.append(str(desc[0]))
            elif hasattr(desc, "name"):
                names.append(str(desc.name))
        return names

    def _coerce_row(self, row):
        if row is None or hasattr(row, "keys") or isinstance(row, dict):
            return row
        names = self._column_names()
        if names and len(names) == len(row):
            return _DictRow(zip(names, row))
        return row

    def fetchone(self):
        return self._coerce_row(self._cursor.fetchone())

    def fetchall(self):
        return [self._coerce_row(row) for row in self._cursor.fetchall()]

    def __iter__(self):
        for row in self._cursor:
            yield self._coerce_row(row)


class _CompatConnection:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, *args, **kwargs):
        return _CompatCursor(self._conn.execute(*args, **kwargs))

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        return self._conn.close()

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._conn.__exit__(exc_type, exc, tb)


def _env_bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _turso_database_url() -> str:
    return (
        os.getenv("CONTEXTCORE_TEXT_TURSO_DATABASE_URL")
        or os.getenv("CONTEXTCORE_TURSO_DATABASE_URL")
        or os.getenv("TURSO_DATABASE_URL")
        or ""
    ).strip()


def _turso_auth_token() -> str:
    return (
        os.getenv("CONTEXTCORE_TEXT_TURSO_AUTH_TOKEN")
        or os.getenv("CONTEXTCORE_TURSO_AUTH_TOKEN")
        or os.getenv("TURSO_AUTH_TOKEN")
        or ""
    ).strip()


def using_turso() -> bool:
    backend = (
        os.getenv("CONTEXTCORE_TEXT_STORAGE_BACKEND")
        or os.getenv("CONTEXTCORE_TEXT_DB_BACKEND")
        or ""
    ).strip().lower()
    if backend in {"sqlite", "local"}:
        return False
    if backend in {"turso", "libsql"}:
        return True
    return bool(_turso_database_url()) or _env_bool(os.getenv("CONTEXTCORE_TEXT_USE_TURSO"))


def get_conn():
    if using_turso():
        try:
            import libsql
        except ImportError as exc:
            raise RuntimeError(
                "Turso text storage requires the 'libsql' Python package. "
                "Install dependencies with `pip install libsql`."
            ) from exc

        database_url = _turso_database_url()
        if not database_url:
            raise RuntimeError(
                "Turso text storage is enabled but TURSO_DATABASE_URL is not set."
            )
        conn = libsql.connect(
            database=database_url,
            auth_token=_turso_auth_token() or None,
        )
        return _CompatConnection(conn)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
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
                matter_id TEXT,
                mtime REAL,
                content TEXT
            )
            """
        )
        # Backward-compatible migration for old DBs that were missing content.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(files)").fetchall()}
        if "content" not in cols:
            conn.execute("ALTER TABLE files ADD COLUMN content TEXT")
        if "matter_id" not in cols:
            conn.execute("ALTER TABLE files ADD COLUMN matter_id TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_matter_id ON files(matter_id)")

        if using_turso():
            _init_turso_fts(conn)
        else:
            # FTS5 virtual table for content + filename
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
                    filename, content, content='files', content_rowid='id', tokenize='porter'
                );
                """
            )
            # Trigram lane for typo/noisy query recovery (may be unavailable on older SQLite builds).
            try:
                conn.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS files_fts_trigram USING fts5(
                        filename, content, content='files', content_rowid='id', tokenize='trigram'
                    );
                    """
                )
            except sqlite3.OperationalError:
                # Keep backward compatibility when trigram tokenizer is not supported.
                pass
    conn.close()


def _init_turso_fts(conn) -> None:
    try:
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_text_files_fts
            ON files USING fts (filename, content)
            WITH (tokenizer = 'default', weights = 'filename=2.0,content=1.0')
            """
        )
    except Exception:
        # Some libSQL/Turso deployments may not expose Tantivy FTS yet. Queries
        # fall back to a LIKE scan while the core storage still lives in Turso.
        pass


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (name,),
    ).fetchone()
    return bool(row)


def trigram_supported() -> bool:
    if using_turso():
        return False
    conn = get_conn()
    try:
        return _table_exists(conn, "files_fts_trigram")
    finally:
        conn.close()

# helper to upsert file metadata and fts content
def upsert_file(
    path: str,
    filename: str,
    category: str,
    mtime: float,
    content: str,
    matter_id: str | None = None,
):
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
                "UPDATE files SET filename = ?, category = ?, matter_id = ?, mtime = ?, content = ? WHERE id = ?",
                (filename, category, matter_id, mtime, content, file_id),
            )
            if not using_turso():
                conn.execute("DELETE FROM files_fts WHERE rowid = ?", (file_id,))
                if _table_exists(conn, "files_fts_trigram"):
                    conn.execute("DELETE FROM files_fts_trigram WHERE rowid = ?", (file_id,))
        else:
            file_id = None
            cur = conn.execute(
                "INSERT OR IGNORE INTO files (path, filename, category, matter_id, mtime, content) VALUES (?, ?, ?, ?, ?, ?)",
                (path, filename, category, matter_id, mtime, content),
            )
            file_id = getattr(cur, "lastrowid", None) or None
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
                    "UPDATE files SET filename = ?, category = ?, matter_id = ?, mtime = ?, content = ? WHERE id = ?",
                    (filename, category, matter_id, mtime, content, file_id),
                )
                if not using_turso():
                    conn.execute("DELETE FROM files_fts WHERE rowid = ?", (file_id,))
                    if _table_exists(conn, "files_fts_trigram"):
                        conn.execute("DELETE FROM files_fts_trigram WHERE rowid = ?", (file_id,))

        # SQLite FTS5 content tables need manual maintenance. Turso FTS indexes
        # are maintained from the base table automatically after commit.
        if not using_turso():
            conn.execute(
                "INSERT INTO files_fts(rowid, filename, content) VALUES (?, ?, ?)",
                (file_id, filename, content)
            )
            if _table_exists(conn, "files_fts_trigram"):
                conn.execute(
                    "INSERT INTO files_fts_trigram(rowid, filename, content) VALUES (?, ?, ?)",
                    (file_id, filename, content),
                )
    conn.close()
    return True


def _normalize_turso_fts_query(match_query: str) -> str:
    terms = [
        t.strip()
        for t in re.split(r"\s+OR\s+|\s+", match_query or "", flags=re.IGNORECASE)
        if t.strip()
    ]
    cleaned = []
    for term in terms:
        if term.upper() in {"AND", "OR", "NOT"}:
            continue
        cleaned.append(term)
    return " ".join(cleaned)


def _query_like_fallback(match_query: str, limit: int = 50):
    terms = [
        t.strip("*").lower()
        for t in re.findall(r"\b[\w-]+\*?", match_query or "")
        if t.upper() not in {"AND", "OR", "NOT"}
    ]
    if not terms:
        return []

    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, filename, content FROM files LIMIT 5000"
        ).fetchall()
    finally:
        conn.close()

    scored = []
    for row in rows:
        filename = str(row["filename"] or "").lower()
        content = str(row["content"] or "").lower()
        score = 0
        for term in terms:
            if not term:
                continue
            score += filename.count(term) * 3
            score += content.count(term)
        if score > 0:
            scored.append(_DictRow(id=int(row["id"]), score=-float(score)))
    scored.sort(key=lambda r: float(r["score"]))
    return scored[: max(1, int(limit))]


def query_fts(match_query: str, limit: int = 50):
    if using_turso():
        conn = get_conn()
        q = _normalize_turso_fts_query(match_query)
        if not q:
            conn.close()
            return []
        try:
            cur = conn.execute(
                """
                SELECT id, fts_score(filename, content, ?) AS score
                FROM files
                WHERE fts_match(filename, content, ?)
                ORDER BY score ASC
                LIMIT ?
                """,
                (q, q, int(limit)),
            )
            rows = cur.fetchall()
            conn.close()
            return rows
        except Exception:
            conn.close()
            return _query_like_fallback(match_query, limit=limit)

    conn = get_conn()
    cur = conn.execute(
        "SELECT rowid as id, bm25(files_fts) as score FROM files_fts WHERE files_fts MATCH ? ORDER BY score LIMIT ?",
        (match_query, limit)
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def query_fts_trigram(match_query: str, limit: int = 50):
    if using_turso():
        return []
    conn = get_conn()
    try:
        if not _table_exists(conn, "files_fts_trigram"):
            return []
        cur = conn.execute(
            """
            SELECT rowid as id, bm25(files_fts_trigram) as score
            FROM files_fts_trigram
            WHERE files_fts_trigram MATCH ?
            ORDER BY score
            LIMIT ?
            """,
            (match_query, limit),
        )
        return cur.fetchall()
    except sqlite3.OperationalError:
        # Tokenizer unsupported or malformed query for trigram syntax.
        return []
    finally:
        conn.close()

def get_file_metadata_by_ids(ids):
    if not ids:
        return []
    conn = get_conn()
    placeholders = ",".join("?" for _ in ids)
    cur = conn.execute(
        f"SELECT id, path, filename, category, matter_id FROM files WHERE id IN ({placeholders})",
        ids,
    )
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


def get_file_record(
    *,
    path: str | None = None,
    file_id: int | None = None,
    matter_id: str | None = None,
):
    if path is None and file_id is None:
        raise ValueError("path or file_id is required")

    where = []
    params: list[object] = []
    if path is not None:
        where.append("path = ?")
        params.append(path)
    if file_id is not None:
        where.append("id = ?")
        params.append(int(file_id))
    if matter_id is not None:
        where.append("matter_id = ?")
        params.append(matter_id)

    conn = get_conn()
    try:
        row = conn.execute(
            f"""
            SELECT id, path, filename, category, matter_id, mtime, content
            FROM files
            WHERE {' AND '.join(where)}
            ORDER BY id DESC
            LIMIT 1
            """,
            params,
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def get_file_mtime(path: str):
    conn = get_conn()
    cur = conn.execute("SELECT mtime FROM files WHERE path = ?", (path,))
    row = cur.fetchone()
    conn.close()
    return row["mtime"] if row else None


def _delete_fts_rows(conn, file_id: int) -> None:
    if using_turso():
        return
    conn.execute("DELETE FROM files_fts WHERE rowid = ?", (file_id,))
    if _table_exists(conn, "files_fts_trigram"):
        conn.execute("DELETE FROM files_fts_trigram WHERE rowid = ?", (file_id,))


def delete_file_by_path(path: str) -> bool:
    conn = get_conn()
    try:
        with conn:
            row = conn.execute("SELECT id FROM files WHERE path = ?", (path,)).fetchone()
            if not row:
                return False
            _delete_fts_rows(conn, int(row["id"]))
            conn.execute("DELETE FROM files WHERE id = ?", (int(row["id"]),))
        return True
    finally:
        conn.close()


def delete_files_under(root: Path | str, excluded_categories: set[str] | None = None) -> int:
    prefix = str(Path(root).expanduser().resolve())
    excluded = {c.lower() for c in (excluded_categories or set())}
    conn = get_conn()
    removed = 0
    try:
        with conn:
            rows = conn.execute(
                "SELECT id, category FROM files WHERE path LIKE ?",
                (f"{prefix}%",),
            ).fetchall()
            for row in rows:
                category = str(row["category"] or "").lower()
                if category in excluded:
                    continue
                _delete_fts_rows(conn, int(row["id"]))
                conn.execute("DELETE FROM files WHERE id = ?", (int(row["id"]),))
                removed += 1
        return removed
    finally:
        conn.close()


def delete_file_by_path_category(path: str, category: str) -> bool:
    conn = get_conn()
    try:
        with conn:
            row = conn.execute(
                "SELECT id FROM files WHERE path = ? AND category = ?",
                (path, category),
            ).fetchone()
            if not row:
                return False
            _delete_fts_rows(conn, int(row["id"]))
            conn.execute("DELETE FROM files WHERE id = ?", (int(row["id"]),))
        return True
    finally:
        conn.close()
