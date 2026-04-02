#!/usr/bin/env python3
"""
Sequential mixed-folder indexing pipeline.

Usage:
  python run_index_pipeline.py --path "C:/data"
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import signal
import sqlite3
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))
from config import get_storage_dir


TEXT_EXTS = {
    ".txt",
    ".md",
    ".pdf",
    ".doc",
    ".docx",
    ".ppt",
    ".pptx",
    ".csv",
    ".xlsx",
    ".xls",
    ".ods",
    ".json",
    ".xml",
    ".rtf",
}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}

COMPLETED_STATUSES = {"indexed", "skipped_unchanged", "skipped_unsupported"}

BACKEND_BY_MODALITY = {
    "text": "text_sqlite_fts",
    "image": "image_clip_annoy",
    "video": "video_clip_annoy",
    "audio": "audio_whisper_textdb",
    "unknown": "unknown",
}

STOP_REQUESTED = False


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_ext(ext: str) -> str:
    ext = ext.strip().lower()
    if not ext:
        return ""
    if not ext.startswith("."):
        ext = f".{ext}"
    return ext


def parse_csv_set(value: str | None, *, normalize_ext: bool = False) -> set[str]:
    if not value:
        return set()
    out: set[str] = set()
    for part in value.split(","):
        item = part.strip()
        if not item:
            continue
        out.add(_norm_ext(item) if normalize_ext else item)
    return out


def detect_modality(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in TEXT_EXTS:
        return "text"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    return "unknown"


def should_exclude(path: Path, patterns: set[str]) -> bool:
    if not patterns:
        return False
    path_posix = path.as_posix()
    path_native = str(path)
    for pattern in patterns:
        if fnmatch.fnmatch(path_posix, pattern) or fnmatch.fnmatch(path_native, pattern):
            return True
    return False


def enumerate_files(
    root: Path,
    include_ext: set[str],
    exclude_globs: set[str],
    max_files: int | None,
) -> list[Path]:
    files: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if should_exclude(p, exclude_globs):
            continue
        ext = p.suffix.lower()
        if include_ext and ext not in include_ext:
            continue
        files.append(p)

    files.sort(key=lambda x: str(x).lower())
    if max_files is not None and max_files >= 0:
        files = files[:max_files]
    return files


class TrackingStore:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def close(self) -> None:
        self.conn.close()

    def _init_db(self) -> None:
        with self.conn:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    root_path TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    status TEXT NOT NULL,
                    total_files INTEGER NOT NULL DEFAULT 0,
                    processed_files INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS file_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    file_path TEXT NOT NULL,
                    extension TEXT,
                    detected_modality TEXT,
                    file_mtime REAL,
                    status TEXT NOT NULL,
                    reason TEXT,
                    error_message TEXT,
                    started_at TEXT,
                    ended_at TEXT,
                    index_backend TEXT,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                )
                """
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_file_events_path_mtime_status ON file_events(file_path, file_mtime, status)"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_file_events_run_status ON file_events(run_id, status)"
            )

    def start_run(self, root_path: Path, total_files: int) -> int:
        started = utc_now_iso()
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO runs(root_path, started_at, status, total_files, processed_files) VALUES (?, ?, ?, ?, ?)",
                (str(root_path), started, "running", total_files, 0),
            )
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, processed_files: int) -> None:
        ended = utc_now_iso()
        with self.conn:
            self.conn.execute(
                "UPDATE runs SET ended_at = ?, status = ?, processed_files = ? WHERE run_id = ?",
                (ended, status, processed_files, run_id),
            )

    def insert_file_event(
        self,
        run_id: int,
        file_path: Path,
        extension: str,
        modality: str,
        file_mtime: float,
        backend: str,
    ) -> int:
        with self.conn:
            cur = self.conn.execute(
                """
                INSERT INTO file_events(
                    run_id, file_path, extension, detected_modality,
                    file_mtime, status, index_backend
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, str(file_path), extension, modality, file_mtime, "queued", backend),
            )
            return int(cur.lastrowid)

    def mark_processing(self, event_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE file_events SET status = ?, started_at = ? WHERE id = ?",
                ("processing", utc_now_iso(), event_id),
            )

    def mark_final(self, event_id: int, status: str, reason: str | None, error_message: str | None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE file_events SET status = ?, reason = ?, error_message = ?, ended_at = ? WHERE id = ?",
                (status, reason, error_message, utc_now_iso(), event_id),
            )

    def was_previously_completed(self, file_path: Path, file_mtime: float) -> bool:
        row = self.conn.execute(
            """
            SELECT status FROM file_events
            WHERE file_path = ?
              AND file_mtime = ?
              AND status IN ('indexed', 'skipped_unchanged', 'skipped_unsupported')
            ORDER BY id DESC
            LIMIT 1
            """,
            (str(file_path), file_mtime),
        ).fetchone()
        return row is not None


@dataclass
class IndexOutcome:
    status: str
    reason: str | None = None
    error_message: str | None = None


class ModalityIndexer:
    def __init__(self) -> None:
        self.image_dirty = False
        self.video_dirty = False

    def index_text_file(self, file_path: Path) -> IndexOutcome:
        from text_search_implementation_v2.db import get_file_mtime, init_db, upsert_file
        from text_search_implementation_v2.extract import extract_text

        init_db()
        current_mtime = file_path.stat().st_mtime
        existing_mtime = get_file_mtime(str(file_path))
        if existing_mtime is not None and abs(existing_mtime - current_mtime) < 0.001:
            return IndexOutcome(status="skipped_unchanged", reason="mtime_unchanged")

        content = extract_text(file_path)
        if not content:
            return IndexOutcome(status="failed", reason="empty_or_unextractable_text", error_message="No text extracted")

        changed = upsert_file(
            str(file_path),
            file_path.name,
            file_path.parent.name,
            current_mtime,
            content,
        )
        if changed:
            return IndexOutcome(status="indexed", reason="text_upserted")
        return IndexOutcome(status="skipped_unchanged", reason="db_reported_unchanged")

    def index_image_file(self, file_path: Path) -> IndexOutcome:
        import numpy as np
        import unimain as um

        um.init_image_meta_db()
        known = um.get_known_images()
        abs_path = str(file_path.resolve())
        current_mtime = file_path.stat().st_mtime

        info = known.get(abs_path)
        if info and abs(float(info.get("mtime", 0.0)) - current_mtime) < 0.001:
            return IndexOutcome(status="skipped_unchanged", reason="mtime_unchanged")

        vec = um.embed_image_file(file_path.resolve())

        annoy_id = None
        if info:
            annoy_id = info.get("annoy_id")
        if not annoy_id:
            annoy_id = um.get_next_annoy_id()

        np.save(str(um.IMAGE_EMBED_DIR / f"{int(annoy_id)}.npy"), vec)
        um.add_or_update_image(abs_path, current_mtime, int(annoy_id))
        self.image_dirty = True
        return IndexOutcome(status="indexed", reason="image_embedding_saved")

    def index_audio_file(self, file_path: Path) -> IndexOutcome:
        from audio_search_implementation_v2.audio_index import transcribe_audio
        from text_search_implementation_v2.db import get_file_mtime, init_db, upsert_file

        init_db()
        current_mtime = file_path.stat().st_mtime
        existing_mtime = get_file_mtime(str(file_path))
        if existing_mtime is not None and abs(existing_mtime - current_mtime) < 0.001:
            return IndexOutcome(status="skipped_unchanged", reason="mtime_unchanged")

        transcript = transcribe_audio(file_path)
        if not transcript:
            return IndexOutcome(status="failed", reason="empty_transcript", error_message="No transcript produced")

        changed = upsert_file(str(file_path), file_path.name, "audio", current_mtime, transcript)
        if changed:
            return IndexOutcome(status="indexed", reason="audio_transcript_upserted")
        return IndexOutcome(status="skipped_unchanged", reason="db_reported_unchanged")

    def index_video_file(self, file_path: Path) -> IndexOutcome:
        import numpy as np
        import video_search_implementation_v2.video_index as vi
        from unimain import embed_image_file

        vi.init_video_db()
        known = vi.get_known_videos()
        abs_path = str(file_path.resolve())
        current_mtime = file_path.stat().st_mtime

        info = known.get(abs_path)
        if info and abs(float(info.get("mtime", 0.0)) - current_mtime) < 0.001:
            return IndexOutcome(status="skipped_unchanged", reason="mtime_unchanged")

        tmpdir, frames = vi.extract_frames_scene_or_sample(abs_path)
        if not frames:
            vi.add_or_update_video(abs_path, current_mtime)
            try:
                Path(tmpdir).rmdir()
            except Exception:
                pass
            return IndexOutcome(status="failed", reason="no_frames_extracted", error_message="No video frames extracted")

        conn = vi._get_conn()
        row = conn.execute("SELECT id FROM videos WHERE path = ?", (abs_path,)).fetchone()
        if row:
            video_id = int(row["id"])
        else:
            conn.execute("INSERT INTO videos (path, mtime) VALUES (?, ?)", (abs_path, current_mtime))
            conn.commit()
            row = conn.execute("SELECT id FROM videos WHERE path = ?", (abs_path,)).fetchone()
            video_id = int(row["id"])
        conn.close()

        last_vec = None
        conn = vi._get_conn()
        rv = conn.execute(
            "SELECT annoy_id FROM frames WHERE video_id = ? ORDER BY id DESC LIMIT 1",
            (video_id,),
        ).fetchone()
        conn.close()

        if rv and rv["annoy_id"]:
            emb_path = vi.VIDEO_EMBED_DIR / f"{rv['annoy_id']}.npy"
            if emb_path.exists():
                try:
                    last_vec = np.load(str(emb_path))
                except Exception:
                    last_vec = None

        next_annoy_id = vi.get_next_annoy_id()
        new_vectors = 0

        for img_path, ts in frames:
            vec = embed_image_file(Path(img_path))
            if last_vec is not None:
                sim = vi.cosine_sim(vec, last_vec)
                if sim >= 0.985:
                    continue

            aid = int(next_annoy_id)
            np.save(str(vi.VIDEO_EMBED_DIR / f"{aid}.npy"), vec)
            vi.add_frame(video_id, ts if ts is not None else -1.0, aid)
            last_vec = vec
            next_annoy_id += 1
            new_vectors += 1

        try:
            for frame_file in Path(tmpdir).glob("*"):
                try:
                    frame_file.unlink()
                except Exception:
                    pass
            Path(tmpdir).rmdir()
        except Exception:
            pass

        vi.add_or_update_video(abs_path, current_mtime)

        if new_vectors > 0:
            self.video_dirty = True
            return IndexOutcome(status="indexed", reason=f"video_vectors_added:{new_vectors}")

        return IndexOutcome(status="indexed", reason="video_processed_no_new_vectors")

    def finalize_indexes(self) -> dict[str, str]:
        finalize: dict[str, str] = {}

        if self.image_dirty:
            try:
                import unimain as um

                um.rebuild_annoy_index(um.all_vectors_iterator)
                finalize["image"] = "rebuilt"
            except Exception as exc:
                finalize["image"] = f"rebuild_failed:{exc}"

        if self.video_dirty:
            try:
                import video_search_implementation_v2.video_index as vi

                vi.rebuild_video_annoy_index(vi.all_video_vectors_iterator)
                finalize["video"] = "rebuilt"
            except Exception as exc:
                finalize["video"] = f"rebuild_failed:{exc}"

        return finalize


def handle_sigint(_signum: int, _frame: Any) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True


def run_pipeline(args: argparse.Namespace) -> dict[str, Any]:
    global STOP_REQUESTED

    root = Path(args.path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"Invalid path: {root}")

    include_ext = parse_csv_set(args.include_ext, normalize_ext=True)
    exclude_globs = parse_csv_set(args.exclude_glob, normalize_ext=False)

    files = enumerate_files(root, include_ext, exclude_globs, args.max_files)

    tracking_db = Path(__file__).resolve().parent / "storage" / "index_pipeline_runs.db"
    store = TrackingStore(tracking_db)
    indexer = ModalityIndexer()

    run_id = store.start_run(root, len(files))

    summary: dict[str, Any] = {
        "run_id": run_id,
        "root_path": str(root),
        "tracking_db": str(tracking_db),
        "total_files": len(files),
        "processed_files": 0,
        "status_counts": {"indexed": 0, "skipped_unchanged": 0, "skipped_unsupported": 0, "failed": 0},
        "modality_counts": {"text": 0, "image": 0, "video": 0, "audio": 0, "unknown": 0},
        "run_status": "running",
        "finalize": {},
    }

    try:
        for file_path in files:
            if STOP_REQUESTED:
                break

            extension = file_path.suffix.lower()
            modality = detect_modality(file_path)
            summary["modality_counts"][modality] = summary["modality_counts"].get(modality, 0) + 1

            try:
                mtime = file_path.stat().st_mtime
            except Exception as exc:
                mtime = -1.0
                modality = "unknown"
                extension = extension or ""
                event_id = store.insert_file_event(
                    run_id,
                    file_path,
                    extension,
                    modality,
                    mtime,
                    BACKEND_BY_MODALITY[modality],
                )
                store.mark_processing(event_id)
                store.mark_final(event_id, "failed", "stat_failed", str(exc))
                summary["status_counts"]["failed"] += 1
                summary["processed_files"] += 1
                continue

            event_id = store.insert_file_event(
                run_id,
                file_path,
                extension,
                modality,
                mtime,
                BACKEND_BY_MODALITY[modality],
            )
            store.mark_processing(event_id)

            if args.resume_latest and store.was_previously_completed(file_path, mtime):
                store.mark_final(event_id, "skipped_unchanged", "resume_latest_previously_completed", None)
                summary["status_counts"]["skipped_unchanged"] += 1
                summary["processed_files"] += 1
                continue

            try:
                if modality == "text":
                    outcome = indexer.index_text_file(file_path)
                elif modality == "image":
                    outcome = indexer.index_image_file(file_path)
                elif modality == "video":
                    outcome = indexer.index_video_file(file_path)
                elif modality == "audio":
                    outcome = indexer.index_audio_file(file_path)
                else:
                    outcome = IndexOutcome(status="skipped_unsupported", reason="unsupported_extension")
            except Exception:
                outcome = IndexOutcome(
                    status="failed",
                    reason="exception",
                    error_message=traceback.format_exc(limit=20),
                )

            store.mark_final(event_id, outcome.status, outcome.reason, outcome.error_message)

            if outcome.status not in summary["status_counts"]:
                summary["status_counts"][outcome.status] = 0
            summary["status_counts"][outcome.status] += 1
            summary["processed_files"] += 1

        if STOP_REQUESTED:
            run_status = "aborted"
        elif summary["status_counts"].get("failed", 0) > 0:
            run_status = "completed_with_errors"
        else:
            run_status = "completed"

        summary["finalize"] = indexer.finalize_indexes()
        summary["run_status"] = run_status
        store.finish_run(run_id, run_status, summary["processed_files"])
        return summary
    finally:
        store.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sequential mixed-folder indexing pipeline")
    parser.add_argument("--path", required=True, help="Root folder path to index recursively")
    parser.add_argument(
        "--include-ext",
        default="",
        help="Comma-separated extensions to include, e.g. '.txt,.pdf,.jpg'",
    )
    parser.add_argument(
        "--exclude-glob",
        default="",
        help="Comma-separated glob patterns to exclude, e.g. '*/node_modules/*,*/.git/*'",
    )
    parser.add_argument(
        "--resume-latest",
        action="store_true",
        help="Skip files already completed in prior runs for identical path+mtime",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=None,
        help="Limit number of files for debug/partial runs",
    )
    parser.add_argument(
        "--json-pretty",
        action="store_true",
        help="Pretty-print JSON summary",
    )
    return parser


def main() -> None:
    signal.signal(signal.SIGINT, handle_sigint)

    parser = build_parser()
    args = parser.parse_args()

    try:
        summary = run_pipeline(args)
    except Exception as exc:
        payload = {"run_status": "failed_to_start", "error": str(exc)}
        print(json.dumps(payload, indent=2))
        sys.exit(1)

    if args.json_pretty:
        print(json.dumps(summary, indent=2))
    else:
        print(json.dumps(summary))

    if summary.get("run_status") in {"completed", "completed_with_errors", "aborted"}:
        sys.exit(0)
    sys.exit(1)


if __name__ == "__main__":
    main()
