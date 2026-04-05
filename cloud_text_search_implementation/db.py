import sqlite3
from datetime import datetime, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import json
import re

_ROOT = Path(__file__).resolve().parent.parent
_ROOT_CONFIG_PATH = _ROOT / "config.py"
_SPEC = spec_from_file_location("root_config", _ROOT_CONFIG_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"Unable to load root config module at {_ROOT_CONFIG_PATH}")
_ROOT_CONFIG = module_from_spec(_SPEC)
_SPEC.loader.exec_module(_ROOT_CONFIG)

STATE_QUEUED = "QUEUED"
STATE_FETCHING = "FETCHING"
STATE_FETCHED = "FETCHED"
STATE_FAILED = "FAILED"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_data_dir() -> Path:
    preferred = _ROOT_CONFIG.get_storage_dir() / "cloud_text_search_implementation" / "storage"
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        return preferred
    except OSError:
        fallback = _ROOT / ".contextcore" / "cloud_text_search_implementation" / "storage"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


DATA_DIR = _resolve_data_dir()
DB_PATH = DATA_DIR / "cloud_text_search_implementation.db"
FALLBACK_DATA_DIR = _ROOT / ".contextcore" / "cloud_text_search_implementation" / "storage"
FALLBACK_DB_PATH = FALLBACK_DATA_DIR / "cloud_text_search_implementation.db"
ANNOY_INDEX_PATH = DATA_DIR / "cloud_text_chunks.ann"
ANNOY_STATE_PATH = DATA_DIR / "cloud_text_annoy_state.json"


def get_conn():
    last_exc = None
    for candidate in (DB_PATH, FALLBACK_DB_PATH):
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(candidate), timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            return conn
        except sqlite3.OperationalError as exc:
            last_exc = exc
    if last_exc:
        raise last_exc
    raise sqlite3.OperationalError("unable to open database file")


def init_db():
    conn = get_conn()
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cloud_manifest (
                path TEXT NOT NULL,
                remote TEXT NOT NULL,
                size INTEGER,
                modified TEXT,
                state TEXT NOT NULL DEFAULT 'QUEUED',
                error TEXT,
                claimed_at TEXT,
                fetched_at TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (path, remote)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS buffer (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL,
                remote TEXT NOT NULL,
                chunk_index INTEGER NOT NULL DEFAULT 0,
                chunk TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cloud_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL,
                remote TEXT NOT NULL,
                filename TEXT NOT NULL,
                size INTEGER,
                modified TEXT,
                content TEXT NOT NULL,
                indexed_at TEXT NOT NULL,
                UNIQUE(path, remote)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cloud_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL,
                path TEXT NOT NULL,
                remote TEXT NOT NULL,
                filename TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                start_offset INTEGER NOT NULL,
                end_offset INTEGER NOT NULL,
                chunk_text TEXT NOT NULL,
                UNIQUE(path, remote, chunk_index)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cloud_chunk_vectors (
                chunk_id INTEGER PRIMARY KEY,
                vector_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS cloud_documents_fts USING fts5(
                filename, content, content='cloud_documents', content_rowid='id', tokenize='porter'
            );
            """
        )
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS cloud_chunks_fts USING fts5(
                filename, chunk_text, content='cloud_chunks', content_rowid='id', tokenize='porter'
            );
            """
        )
        manifest_cols = {r["name"] for r in conn.execute("PRAGMA table_info(cloud_manifest)").fetchall()}
        if "error" not in manifest_cols:
            conn.execute("ALTER TABLE cloud_manifest ADD COLUMN error TEXT")
        if "claimed_at" not in manifest_cols:
            conn.execute("ALTER TABLE cloud_manifest ADD COLUMN claimed_at TEXT")
        if "fetched_at" not in manifest_cols:
            conn.execute("ALTER TABLE cloud_manifest ADD COLUMN fetched_at TEXT")
        if "updated_at" not in manifest_cols:
            conn.execute("ALTER TABLE cloud_manifest ADD COLUMN updated_at TEXT")
            conn.execute("UPDATE cloud_manifest SET updated_at = COALESCE(updated_at, CURRENT_TIMESTAMP)")

        buffer_cols = {r["name"] for r in conn.execute("PRAGMA table_info(buffer)").fetchall()}
        if "chunk_index" not in buffer_cols:
            conn.execute("ALTER TABLE buffer ADD COLUMN chunk_index INTEGER NOT NULL DEFAULT 0")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_manifest_state ON cloud_manifest(state)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_manifest_remote_state ON cloud_manifest(remote, state)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_buffer_file ON buffer(remote, path, chunk_index)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cloud_chunks_doc ON cloud_chunks(document_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cloud_chunks_path_remote ON cloud_chunks(path, remote)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cloud_docs_remote ON cloud_documents(remote)")
    conn.close()


def insert_manifest(conn, file_info: dict, remote: str) -> bool:
    path = str(file_info["Path"])
    size = file_info.get("Size")
    modified = file_info.get("ModTime")

    existing = conn.execute(
        "SELECT size, modified, state FROM cloud_manifest WHERE path = ? AND remote = ?",
        (path, remote),
    ).fetchone()

    if existing and existing["size"] == size and existing["modified"] == modified and existing["state"] == STATE_FETCHED:
        return False

    conn.execute(
        """
        INSERT INTO cloud_manifest (path, remote, size, modified, state, error, claimed_at, fetched_at, updated_at)
        VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, ?)
        ON CONFLICT(path, remote) DO UPDATE SET
            size=excluded.size,
            modified=excluded.modified,
            state=excluded.state,
            error=NULL,
            claimed_at=NULL,
            fetched_at=NULL,
            updated_at=excluded.updated_at
        """,
        (path, remote, size, modified, STATE_QUEUED, _now_iso()),
    )
    return True


def claim_next_file(conn, remote: str | None = None):
    if remote:
        file_row = conn.execute(
            """
            SELECT path, remote, size, modified
            FROM cloud_manifest
            WHERE state = ? AND remote = ?
            ORDER BY COALESCE(size, 0) ASC, path ASC
            LIMIT 1
            """,
            (STATE_QUEUED, remote),
        ).fetchone()
    else:
        file_row = conn.execute(
            """
            SELECT path, remote, size, modified
            FROM cloud_manifest
            WHERE state = ?
            ORDER BY COALESCE(size, 0) ASC, path ASC
            LIMIT 1
            """,
            (STATE_QUEUED,),
        ).fetchone()

    if not file_row:
        return None

    updated = conn.execute(
        """
        UPDATE cloud_manifest
        SET state = ?, claimed_at = ?, updated_at = ?, error = NULL
        WHERE path = ? AND remote = ? AND state = ?
        """,
        (STATE_FETCHING, _now_iso(), _now_iso(), file_row["path"], file_row["remote"], STATE_QUEUED),
    )
    conn.commit()
    if updated.rowcount == 0:
        return None
    return dict(file_row)


def update_manifest_state(conn, file_row: dict, new_state: str, error: str | None = None) -> bool:
    current_state = conn.execute(
        "SELECT state FROM cloud_manifest WHERE path = ? AND remote = ?",
        (file_row["path"], file_row["remote"]),
    ).fetchone()
    if not current_state:
        return False

    valid_transitions = {
        STATE_QUEUED: {STATE_FETCHING},
        STATE_FETCHING: {STATE_FETCHED, STATE_FAILED},
        STATE_FETCHED: {STATE_QUEUED},
        STATE_FAILED: {STATE_QUEUED},
    }
    if new_state not in valid_transitions.get(current_state["state"], set()):
        return False

    conn.execute(
        """
        UPDATE cloud_manifest
        SET state = ?, error = ?, fetched_at = ?, updated_at = ?
        WHERE path = ? AND remote = ?
        """,
        (
            new_state,
            error,
            _now_iso() if new_state in {STATE_FETCHED, STATE_FAILED} else None,
            _now_iso(),
            file_row["path"],
            file_row["remote"],
        ),
    )
    conn.commit()
    return True


def insert_buffer(conn, file_row: dict, chunk: str, chunk_index: int):
    conn.execute(
        """
        INSERT INTO buffer (path, remote, chunk_index, chunk)
        VALUES (?, ?, ?, ?)
        """,
        (file_row["path"], file_row["remote"], chunk_index, chunk),
    )


def clear_buffer(conn, file_row: dict):
    conn.execute(
        "DELETE FROM buffer WHERE path = ? AND remote = ?",
        (file_row["path"], file_row["remote"]),
    )


def read_buffered_content(conn, file_row: dict) -> str:
    rows = conn.execute(
        """
        SELECT chunk
        FROM buffer
        WHERE path = ? AND remote = ?
        ORDER BY chunk_index ASC
        """,
        (file_row["path"], file_row["remote"]),
    ).fetchall()
    return "".join((r["chunk"] or "") for r in rows)


def upsert_document(conn, file_row: dict, content: str):
    filename = Path(file_row["path"]).name
    now = _now_iso()
    existing = conn.execute(
        "SELECT id FROM cloud_documents WHERE path = ? AND remote = ?",
        (file_row["path"], file_row["remote"]),
    ).fetchone()

    if existing:
        doc_id = int(existing["id"])
        conn.execute(
            """
            UPDATE cloud_documents
            SET filename = ?, size = ?, modified = ?, content = ?, indexed_at = ?
            WHERE id = ?
            """,
            (filename, file_row.get("size"), file_row.get("modified"), content, now, doc_id),
        )
        conn.execute("DELETE FROM cloud_documents_fts WHERE rowid = ?", (doc_id,))
    else:
        cur = conn.execute(
            """
            INSERT INTO cloud_documents (path, remote, filename, size, modified, content, indexed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (file_row["path"], file_row["remote"], filename, file_row.get("size"), file_row.get("modified"), content, now),
        )
        doc_id = int(cur.lastrowid)

    conn.execute(
        "INSERT INTO cloud_documents_fts(rowid, filename, content) VALUES (?, ?, ?)",
        (doc_id, filename, content),
    )
    return doc_id


def _split_chunks(text: str, chunk_chars: int = 900, chunk_overlap: int = 120) -> list[dict]:
    if not text:
        return []
    step = max(1, chunk_chars - chunk_overlap)
    out = []
    i = 0
    idx = 0
    while i < len(text):
        end = min(len(text), i + chunk_chars)
        chunk = text[i:end].strip()
        if chunk:
            out.append({"index": idx, "start": i, "end": end, "text": chunk})
            idx += 1
        if end >= len(text):
            break
        i += step
    return out


def upsert_document_chunks(
    conn,
    file_row: dict,
    document_id: int,
    content: str,
    embed_fn,
    chunk_chars: int = 900,
    chunk_overlap: int = 120,
) -> int:
    old_rows = conn.execute(
        "SELECT id FROM cloud_chunks WHERE document_id = ?",
        (document_id,),
    ).fetchall()
    old_chunk_ids = [int(r["id"]) for r in old_rows]
    for cid in old_chunk_ids:
        conn.execute("DELETE FROM cloud_chunks_fts WHERE rowid = ?", (cid,))
    conn.execute("DELETE FROM cloud_chunk_vectors WHERE chunk_id IN (SELECT id FROM cloud_chunks WHERE document_id = ?)", (document_id,))
    conn.execute("DELETE FROM cloud_chunks WHERE document_id = ?", (document_id,))

    chunks = _split_chunks(content, chunk_chars=chunk_chars, chunk_overlap=chunk_overlap)
    filename = Path(file_row["path"]).name
    inserted = 0
    for c in chunks:
        cur = conn.execute(
            """
            INSERT INTO cloud_chunks (
                document_id, path, remote, filename, chunk_index, start_offset, end_offset, chunk_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document_id,
                file_row["path"],
                file_row["remote"],
                filename,
                int(c["index"]),
                int(c["start"]),
                int(c["end"]),
                c["text"],
            ),
        )
        chunk_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO cloud_chunks_fts(rowid, filename, chunk_text) VALUES (?, ?, ?)",
            (chunk_id, filename, c["text"]),
        )
        vector = embed_fn(c["text"])
        conn.execute(
            "INSERT OR REPLACE INTO cloud_chunk_vectors(chunk_id, vector_json) VALUES (?, ?)",
            (chunk_id, json.dumps(vector, separators=(",", ":"))),
        )
        inserted += 1
    return inserted


def query_cloud_chunk_fts(match_query: str, limit: int = 100):
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT rowid AS id, bm25(cloud_chunks_fts) AS score
            FROM cloud_chunks_fts
            WHERE cloud_chunks_fts MATCH ?
            ORDER BY score
            LIMIT ?
            """,
            (match_query, int(limit)),
        ).fetchall()
        return rows
    finally:
        conn.close()


def get_chunk_metadata_by_ids(ids: list[int]) -> dict[int, dict]:
    if not ids:
        return {}
    conn = get_conn()
    try:
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"""
            SELECT id, path, remote, filename, chunk_index, chunk_text
            FROM cloud_chunks
            WHERE id IN ({placeholders})
            """,
            ids,
        ).fetchall()
        return {int(r["id"]): dict(r) for r in rows}
    finally:
        conn.close()


def get_all_chunk_vectors() -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT c.id AS chunk_id, v.vector_json
            FROM cloud_chunks c
            JOIN cloud_chunk_vectors v ON v.chunk_id = c.id
            ORDER BY c.id ASC
            """
        ).fetchall()
        out = []
        for r in rows:
            try:
                vec = json.loads(r["vector_json"] or "[]")
                if isinstance(vec, list) and vec:
                    out.append({"chunk_id": int(r["chunk_id"]), "vector": [float(x) for x in vec]})
            except Exception:
                continue
        return out
    finally:
        conn.close()


def count_cloud_chunks() -> int:
    conn = get_conn()
    try:
        row = conn.execute("SELECT COUNT(*) AS c FROM cloud_chunks").fetchone()
        return int(row["c"] if row else 0)
    finally:
        conn.close()


def normalize_query_for_fts(query: str) -> str:
    tokens = re.findall(r"\b\w+\b", (query or "").lower())
    if not tokens:
        return ""
    return " OR ".join(f"{t}*" for t in tokens)


def manifest_counts(conn, remote: str) -> dict:
    total = int(
        conn.execute("SELECT COUNT(*) AS c FROM cloud_manifest WHERE remote = ?", (remote,)).fetchone()["c"]
    )
    queued = int(
        conn.execute(
            "SELECT COUNT(*) AS c FROM cloud_manifest WHERE remote = ? AND state = ?",
            (remote, STATE_QUEUED),
        ).fetchone()["c"]
    )
    fetching = int(
        conn.execute(
            "SELECT COUNT(*) AS c FROM cloud_manifest WHERE remote = ? AND state = ?",
            (remote, STATE_FETCHING),
        ).fetchone()["c"]
    )
    fetched = int(
        conn.execute(
            "SELECT COUNT(*) AS c FROM cloud_manifest WHERE remote = ? AND state = ?",
            (remote, STATE_FETCHED),
        ).fetchone()["c"]
    )
    failed = int(
        conn.execute(
            "SELECT COUNT(*) AS c FROM cloud_manifest WHERE remote = ? AND state = ?",
            (remote, STATE_FAILED),
        ).fetchone()["c"]
    )
    return {
        "total": total,
        "queued": queued,
        "fetching": fetching,
        "fetched": fetched,
        "failed": failed,
    }
