import json
import shutil
import subprocess
import threading
from pathlib import Path

from config import get_config
from cloud_text_search_implementation.db import (
    STATE_FAILED,
    STATE_FETCHED,
    claim_next_file,
    clear_buffer,
    get_conn,
    init_db,
    insert_buffer,
    insert_manifest,
    manifest_counts,
    read_buffered_content,
    upsert_document_chunks,
    update_manifest_state,
    upsert_document,
)
from cloud_text_search_implementation.embeddings import embed_text
from cloud_text_search_implementation.annoy_store import mark_dirty, rebuild_annoy_index

TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".json",
    ".csv",
    ".log",
    ".xml",
    ".html",
    ".htm",
    ".js",
    ".ts",
    ".py",
    ".java",
    ".c",
    ".cpp",
    ".go",
    ".rs",
    ".yaml",
    ".yml",
    ".pdf",
    ".doc",
    ".docx",
    ".ppt",
    ".pptx",
    ".xls",
    ".xlsx",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".rst",
    ".tsv",
    ".rtf",
    ".ods",
}

SMALL_FILE_MAX_BYTES = 5 * 1024 * 1024
STREAM_CHUNK_CHARS = 10_000


def _get_rclone_path() -> str:
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
    raise RuntimeError("rclone not found")


def _normalize_remote_name(remote_name: str) -> str:
    return remote_name[:-1] if remote_name.endswith(":") else remote_name


def _is_text_file(path: str) -> bool:
    return Path(path).suffix.lower() in TEXT_EXTENSIONS


def resolve_cloud_remote(remote_name: str | None = None) -> str:
    if remote_name and remote_name.strip():
        return _normalize_remote_name(remote_name.strip())

    cfg = get_config()
    storage = cfg.get("storage", {})
    if isinstance(storage, dict):
        cloud = storage.get("cloud", {})
        if isinstance(cloud, dict):
            remote = cloud.get("remote")
            if remote:
                return _normalize_remote_name(str(remote))

    flat_remote = cfg.get("storage.cloud.remote")
    if flat_remote:
        return _normalize_remote_name(str(flat_remote))

    raise RuntimeError("No cloud remote configured. Run contextcore cloudconnect first.")


def list_cloud_files(remote: str) -> list[dict]:
    rclone = _get_rclone_path()
    result = subprocess.run(
        [rclone, "lsjson", f"{remote}:", "--recursive"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"rclone lsjson failed: {result.stderr.strip()}")
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Failed to parse rclone lsjson output: {exc}") from exc
    if not isinstance(payload, list):
        raise RuntimeError("rclone lsjson returned unexpected payload type")
    return payload


def discover_text_files(remote: str) -> int:
    init_db()
    files = list_cloud_files(remote)
    conn = get_conn()
    inserted = 0
    try:
        with conn:
            for item in files:
                if item.get("IsDir"):
                    continue
                path = str(item.get("Path") or "")
                if not path or not _is_text_file(path):
                    continue
                if insert_manifest(conn, item, remote):
                    inserted += 1
    finally:
        conn.close()
    return inserted


def _fetch_small_file(file_row: dict) -> str:
    rclone = _get_rclone_path()
    result = subprocess.run(
        [rclone, "cat", f"{file_row['remote']}:{file_row['path']}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "rclone cat failed")
    return result.stdout or ""


def _stream_large_file(file_row: dict, emit_chunk) -> None:
    rclone = _get_rclone_path()
    process = subprocess.Popen(
        [rclone, "cat", f"{file_row['remote']}:{file_row['path']}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    if process.stdout is None:
        raise RuntimeError("Failed to open stream for rclone cat")

    chunk_idx = 0
    chunk = []
    current_len = 0
    for line in process.stdout:
        chunk.append(line)
        current_len += len(line)
        if current_len >= STREAM_CHUNK_CHARS:
            emit_chunk(chunk_idx, "".join(chunk))
            chunk_idx += 1
            chunk = []
            current_len = 0

    if chunk:
        emit_chunk(chunk_idx, "".join(chunk))

    stderr = (process.stderr.read() if process.stderr else "") or ""
    rc = process.wait()
    if rc != 0:
        raise RuntimeError(stderr.strip() or f"rclone cat failed with code {rc}")


def _worker(remote: str):
    conn = get_conn()
    try:
        while True:
            file_row = claim_next_file(conn, remote=remote)
            if not file_row:
                return

            try:
                clear_buffer(conn, file_row)
                size = int(file_row.get("size") or 0)
                if size <= SMALL_FILE_MAX_BYTES:
                    content = _fetch_small_file(file_row)
                    insert_buffer(conn, file_row, content, chunk_index=0)
                else:
                    def _emit(idx: int, chunk: str):
                        insert_buffer(conn, file_row, chunk, chunk_index=idx)
                        conn.commit()

                    _stream_large_file(file_row, _emit)

                content = read_buffered_content(conn, file_row)
                doc_id = upsert_document(conn, file_row, content)
                upsert_document_chunks(conn, file_row, doc_id, content, embed_fn=embed_text)
                mark_dirty()
                update_manifest_state(conn, file_row, STATE_FETCHED)
                conn.commit()
            except Exception as exc:
                update_manifest_state(conn, file_row, STATE_FAILED, error=str(exc))
                conn.commit()
    finally:
        conn.close()


def run_scan(remote_name: str | None = None, workers: int = 3) -> dict:
    remote = resolve_cloud_remote(remote_name)
    discovered = discover_text_files(remote)

    thread_count = max(1, min(int(workers), 8))
    threads = []
    for _ in range(thread_count):
        t = threading.Thread(target=_worker, args=(remote,), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()

    conn = get_conn()
    try:
        counts = manifest_counts(conn, remote)
    finally:
        conn.close()
    annoy_sync = rebuild_annoy_index()

    return {
        "remote": remote,
        "discovered_or_requeued": discovered,
        "workers": thread_count,
        "counts": counts,
        "annoy": annoy_sync,
    }
