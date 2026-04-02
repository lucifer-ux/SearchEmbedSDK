from __future__ import annotations

import math
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import numpy as np

from config import get_dedup_threshold, get_video_ocr_enabled, get_storage_dir
from video_search_implementation_v2.runtime import (
    clip_model_ready,
    resolve_ffmpeg_path,
    resolve_ffprobe_path,
)

STORAGE_DIR = get_storage_dir() / "video_search_implementation_v2" / "storage"
STORAGE_DIR.mkdir(parents=True, exist_ok=True)

VIDEO_META_DB = STORAGE_DIR / "videos_meta.db"
EMBED_DIM = 512
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm"}

DEFAULT_DESCRIPTION_LABELS = [
    "a person speaking",
    "a meeting room",
    "a whiteboard with writing",
    "a presentation slide",
    "a code editor",
    "a terminal or console",
    "an outdoor scene",
    "a building or architecture",
    "a document or text",
    "a chart or graph",
    "a product or object",
    "a screen recording",
    "a video call",
    "an animal",
    "food or cooking",
    "a vehicle or transportation",
    "nature or landscape",
    "a diagram or flowchart",
    "a logo or branding",
    "blurry or transitional frame",
]


def _load_sqlite_vec(conn: sqlite3.Connection) -> None:
    try:
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except ImportError as exc:
        raise ImportError("sqlite-vec is required. Install it with: pip install sqlite-vec") from exc
    except AttributeError:
        # Some Python builds don't support extensions (e.g., macOS system Python)
        # Try alternative loading method
        try:
            import sqlite_vec
            sqlite_vec.load(conn)
        except Exception:
            pass  # Continue without sqlite-vec if it fails


def _serialize_f32(vec: np.ndarray) -> bytes:
    return vec.astype(np.float32).tobytes()


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(VIDEO_META_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    _load_sqlite_vec(conn)
    return conn


def init_video_db() -> None:
    conn = _get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT UNIQUE,
            mtime REAL
        );

        CREATE TABLE IF NOT EXISTS frames (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id INTEGER NOT NULL,
            timestamp REAL,
            description TEXT,
            ocr_text TEXT,
            FOREIGN KEY(video_id) REFERENCES videos(id) ON DELETE CASCADE
        );
        """
    )

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(frames)").fetchall()}
    if "ocr_text" not in cols:
        conn.execute("ALTER TABLE frames ADD COLUMN ocr_text TEXT")

    try:
        conn.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS frame_vectors
            USING vec0(embedding float[{EMBED_DIM}])
            """
        )
    except Exception:
        pass

    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS frames_fts USING fts5(
            description, ocr_text, content='frames', content_rowid='id', tokenize='porter'
        )
        """
    )
    conn.commit()
    conn.close()


def get_known_videos() -> dict[str, dict[str, float]]:
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT
            v.id,
            v.path,
            v.mtime,
            (SELECT COUNT(*) FROM frames f WHERE f.video_id = v.id) AS frame_count,
            (
                SELECT COUNT(*)
                FROM frames_fts ft
                JOIN frames f2 ON f2.id = ft.rowid
                WHERE f2.video_id = v.id
            ) AS fts_count
        FROM videos v
        """
    ).fetchall()
    conn.close()
    return {
        r["path"]: {
            "id": r["id"],
            "mtime": r["mtime"],
            "frame_count": r["frame_count"],
            "fts_count": r["fts_count"],
        }
        for r in rows
    }


def _ffmpeg_bin() -> Optional[str]:
    path = resolve_ffmpeg_path()
    return str(path) if path else None


def _ffprobe_bin() -> Optional[str]:
    path = resolve_ffprobe_path()
    return str(path) if path else None


def extract_frames_scene_or_sample(
    video_path: str, max_frames: int = 80, scene_thresh: float = 0.4
) -> tuple[Path, list[tuple[str, Optional[float]]]]:
    tmpdir = Path(tempfile.mkdtemp(prefix="video_frames_"))
    frames: list[tuple[str, Optional[float]]] = []
    ffmpeg = _ffmpeg_bin()
    ffprobe = _ffprobe_bin()
    if not ffmpeg:
        return tmpdir, frames

    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        video_path,
        "-vf",
        f"select='gt(scene,{scene_thresh})',scale=640:-1",
        "-vsync",
        "vfr",
        str(tmpdir / "frame_%06d.jpg"),
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for img in sorted(tmpdir.glob("frame_*.jpg")):
            frames.append((str(img), None))
    except subprocess.CalledProcessError:
        pass

    if not frames:
        duration = 60.0
        if ffprobe:
            try:
                probe = subprocess.run(
                    [
                        ffprobe,
                        "-v",
                        "error",
                        "-show_entries",
                        "format=duration",
                        "-of",
                        "default=noprint_wrappers=1:nokey=1",
                        video_path,
                    ],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                duration = float(probe.stdout.decode().strip())
            except Exception:
                duration = 60.0

        step = max(1.0, duration / max_frames)
        timestamps = [i * step for i in range(int(min(max_frames, math.ceil(duration / step))))]
        for idx, ts in enumerate(timestamps):
            outp = tmpdir / f"sample_{idx:06d}.jpg"
            cmd = [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                str(ts),
                "-i",
                video_path,
                "-frames:v",
                "1",
                "-vf",
                "scale=640:-1",
                str(outp),
            ]
            try:
                subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                frames.append((str(outp), float(ts)))
            except subprocess.CalledProcessError:
                continue

    if len(frames) > max_frames:
        frames = frames[:max_frames]
    return tmpdir, frames


def _extract_audio_track(video_path: str, tmpdir: Path) -> Optional[Path]:
    ffmpeg = _ffmpeg_bin()
    if not ffmpeg:
        return None

    audio_path = tmpdir / "audio.wav"
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        video_path,
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        "-ac",
        "1",
        str(audio_path),
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if audio_path.exists() and audio_path.stat().st_size > 1000:
            return audio_path
    except subprocess.CalledProcessError:
        pass
    return None


def _transcribe_video_audio(video_path: str, tmpdir: Path) -> Optional[str]:
    audio_file = _extract_audio_track(video_path, tmpdir)
    if audio_file is None:
        return None
    try:
        from audio_search_implementation_v2.audio_index import transcribe_audio

        transcript = transcribe_audio(audio_file)
        return transcript.strip() or None
    except Exception as exc:
        print(f"video transcription failed: {exc}")
        return None


def cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    a = a.astype("float32")
    b = b.astype("float32")
    na = a / (np.linalg.norm(a) + 1e-12)
    nb = b / (np.linalg.norm(b) + 1e-12)
    return float(np.dot(na, nb))


def mmr_is_unique(candidate: np.ndarray, selected_vecs: list[np.ndarray], threshold: float = 0.85) -> bool:
    if not selected_vecs:
        return True
    return max(cosine_sim(candidate, sv) for sv in selected_vecs) < threshold


def _describe_frame(img_path: str) -> str:
    try:
        from unimain import lazy_load_clip
        import torch
        from PIL import Image

        model, processor = lazy_load_clip()
        img = Image.open(img_path).convert("RGB")
        inputs = processor(text=DEFAULT_DESCRIPTION_LABELS, images=img, return_tensors="pt", padding=True)
        inputs = {k: v.to("cpu") for k, v in inputs.items()}
        with torch.no_grad():
            outputs = model(**inputs)
            probs = outputs.logits_per_image.softmax(dim=1)

        best_idx = probs[0].argmax().item()
        confidence = probs[0][best_idx].item()
        label = DEFAULT_DESCRIPTION_LABELS[best_idx]
        return f"{label} ({confidence:.0%} confidence)"
    except Exception as exc:
        print(f"frame description failed: {exc}")
        return "unknown content"


def _extract_ocr_text(img_path: str) -> str:
    if not get_video_ocr_enabled():
        return ""
    try:
        from image_search_implementation_v2.ocr import extract_ocr_from_image

        return (extract_ocr_from_image(Path(img_path)) or "").strip()
    except Exception:
        return ""


def _remove_existing_frame_rows(conn: sqlite3.Connection, video_id: int) -> None:
    old_frames = conn.execute("SELECT id FROM frames WHERE video_id = ?", (video_id,)).fetchall()
    for old in old_frames:
        frame_id = old["id"]
        try:
            conn.execute("DELETE FROM frame_vectors WHERE rowid = ?", (frame_id,))
        except Exception:
            pass
        try:
            conn.execute("DELETE FROM frames_fts WHERE rowid = ?", (frame_id,))
        except Exception:
            pass
    conn.execute("DELETE FROM frames WHERE video_id = ?", (video_id,))


def _normalize_fts_score(score: float, max_boost: float) -> float:
    return min(max_boost, abs(float(score)) / 20.0)


def _apply_video_hit(
    hits: dict[str, dict[str, object]],
    *,
    path: str,
    score: float,
    description: str,
    timestamp: float,
    transcript_match: bool = False,
    context_match: bool = False,
    ocr_text: str = "",
    additive: bool = False,
) -> None:
    if path not in hits:
        hits[path] = {
            "video_path": path,
            "score": score,
            "description": description,
            "best_timestamp": timestamp,
            "transcript_match": transcript_match,
            "context_match": context_match,
            "ocr_text": ocr_text,
        }
        return

    current = hits[path]
    current["transcript_match"] = bool(current.get("transcript_match")) or transcript_match
    current["context_match"] = bool(current.get("context_match")) or context_match

    if additive:
        current["score"] = min(0.99, float(current.get("score", 0.0)) + score)
    elif score > float(current.get("score", 0.0)):
        current["score"] = score

    if description and (additive or not current.get("description")):
        current["description"] = description
    if ocr_text and not current.get("ocr_text"):
        current["ocr_text"] = ocr_text
    if timestamp is not None and timestamp >= 0:
        current_ts = current.get("best_timestamp")
        if current_ts is None or float(current_ts) < 0 or score >= float(current.get("score", 0.0)):
            current["best_timestamp"] = timestamp


def scan_video_index(
    video_root: Path,
    max_frames_per_video: int = 80,
    dedup_threshold: Optional[float] = None,
):
    init_video_db()

    ffmpeg = resolve_ffmpeg_path()
    if not ffmpeg:
        return {"status": "skipped", "reason": "ffmpeg not installed", "new_vectors": 0}

    clip_ready, clip_error = clip_model_ready()
    if not clip_ready:
        return {"status": "skipped", "reason": f"clip model unavailable: {clip_error}", "new_vectors": 0}

    if dedup_threshold is None:
        dedup_threshold = get_dedup_threshold()

    from text_search_implementation_v2.db import delete_file_by_path_category, init_db, upsert_file
    from unimain import embed_image_file

    init_db()
    known = get_known_videos()
    new_count = 0

    for p in video_root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in VIDEO_EXTS:
            continue
        try:
            mtime = p.stat().st_mtime
        except Exception:
            continue

        video_path = str(p)
        info = known.get(video_path)
        if (
            info
            and abs(info["mtime"] - mtime) < 0.001
            and int(info.get("frame_count", 0)) > 0
            and int(info.get("fts_count", 0)) > 0
        ):
            continue

        tmpdir, frames = extract_frames_scene_or_sample(video_path, max_frames_per_video)
        if not frames:
            _cleanup_tmpdir(tmpdir)
            continue

        transcript = _transcribe_video_audio(video_path, tmpdir)
        selected_vecs: list[np.ndarray] = []
        frame_data: list[dict[str, object]] = []

        for img_path, timestamp in frames:
            try:
                vec = embed_image_file(Path(img_path))
            except Exception as exc:
                print(f"frame embedding failed for {img_path}: {exc}")
                continue

            if not mmr_is_unique(vec, selected_vecs, dedup_threshold):
                continue

            description = _describe_frame(img_path)
            ocr_text = _extract_ocr_text(img_path)
            frame_data.append(
                {
                    "timestamp": timestamp if timestamp is not None else -1.0,
                    "vec": vec,
                    "description": description,
                    "ocr_text": ocr_text,
                }
            )
            selected_vecs.append(vec)

        conn = _get_conn()
        try:
            row = conn.execute("SELECT id FROM videos WHERE path = ?", (video_path,)).fetchone()
            if row:
                video_id = row["id"]
                _remove_existing_frame_rows(conn, video_id)
                conn.execute("UPDATE videos SET mtime = ? WHERE id = ?", (mtime, video_id))
            else:
                conn.execute("INSERT INTO videos (path, mtime) VALUES (?, ?)", (video_path, mtime))
                video_id = conn.execute("SELECT id FROM videos WHERE path = ?", (video_path,)).fetchone()["id"]

            for fd in frame_data:
                conn.execute(
                    "INSERT INTO frames (video_id, timestamp, description, ocr_text) VALUES (?, ?, ?, ?)",
                    (video_id, fd["timestamp"], fd["description"], fd["ocr_text"]),
                )
                frame_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    "INSERT INTO frame_vectors (rowid, embedding) VALUES (?, ?)",
                    (frame_id, _serialize_f32(fd["vec"])),
                )
                conn.execute(
                    "INSERT INTO frames_fts (rowid, description, ocr_text) VALUES (?, ?, ?)",
                    (frame_id, fd["description"], fd["ocr_text"]),
                )
            conn.commit()
            new_count += len(frame_data)
        except Exception as exc:
            conn.rollback()
            print(f"video db update failed for {video_path}: {exc}")
        finally:
            conn.close()

        if transcript:
            try:
                upsert_file(video_path, p.name, "video_transcript", mtime, transcript)
            except Exception as exc:
                print(f"transcript storage failed for {video_path}: {exc}")
        else:
            delete_file_by_path_category(video_path, "video_transcript")

        _cleanup_tmpdir(tmpdir)

    return {"status": "ok", "new_vectors": new_count}


def search_videos(query: str, top_k: int = 5):
    init_video_db()

    clip_ready, clip_error = clip_model_ready()
    if not clip_ready:
        return {"error": f"clip model unavailable: {clip_error}"}

    try:
        from unimain import embed_text_with_clip

        qvec = embed_text_with_clip(query)
    except Exception as exc:
        return {"error": f"embed_failed: {exc}"}

    conn = _get_conn()
    frame_count = conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
    if frame_count == 0:
        conn.close()
        return {"hits": []}

    hits: dict[str, dict[str, object]] = {}

    try:
        rows = conn.execute(
            """
            SELECT fv.rowid AS frame_id, fv.distance AS distance
            FROM frame_vectors fv
            WHERE fv.embedding MATCH ?
              AND k = ?
            ORDER BY fv.distance
            """,
            (_serialize_f32(qvec), top_k * 4),
        ).fetchall()
    except Exception as exc:
        conn.close()
        return {"error": f"vec_query_failed: {exc}"}

    for row in rows:
        frame = conn.execute(
            """
            SELECT f.description, f.ocr_text, f.timestamp, v.path
            FROM frames f
            JOIN videos v ON f.video_id = v.id
            WHERE f.id = ?
            """,
            (row["frame_id"],),
        ).fetchone()
        if not frame:
            continue
        score = 1.0 - (float(row["distance"]) / 2.0)
        if score < 0.15:
            continue
        _apply_video_hit(
            hits,
            path=frame["path"],
            score=score,
            description=frame["description"] or "",
            timestamp=frame["timestamp"],
            ocr_text=frame["ocr_text"] or "",
        )

    try:
        context_rows = conn.execute(
            """
            SELECT rowid AS frame_id, bm25(frames_fts) AS score
            FROM frames_fts
            WHERE frames_fts MATCH ?
            ORDER BY score
            LIMIT ?
            """,
            (query, top_k * 4),
        ).fetchall()
    except Exception:
        context_rows = []

    for row in context_rows:
        frame = conn.execute(
            """
            SELECT f.description, f.ocr_text, f.timestamp, v.path
            FROM frames f
            JOIN videos v ON f.video_id = v.id
            WHERE f.id = ?
            """,
            (row["frame_id"],),
        ).fetchone()
        if not frame:
            continue
        boost = _normalize_fts_score(float(row["score"]), max_boost=0.35)
        context_text = (frame["description"] or "").strip()
        if frame["ocr_text"]:
            context_text = context_text or frame["ocr_text"]
        _apply_video_hit(
            hits,
            path=frame["path"],
            score=(0.2 + boost) if frame["path"] not in hits else boost,
            description=context_text,
            timestamp=frame["timestamp"],
            context_match=True,
            ocr_text=frame["ocr_text"] or "",
            additive=frame["path"] in hits,
        )

    try:
        from text_search_implementation_v2.db import get_file_metadata_by_ids, init_db, query_fts

        init_db()
        fts_rows = query_fts(query, limit=top_k * 4)
        meta = get_file_metadata_by_ids([row["id"] for row in fts_rows]) if fts_rows else {}
        for row in fts_rows:
            item = meta.get(row["id"])
            if not item or item.get("category") != "video_transcript":
                continue
            boost = _normalize_fts_score(float(row["score"]), max_boost=0.30)
            path = item["path"]
            _apply_video_hit(
                hits,
                path=path,
                score=(0.2 + boost) if path not in hits else boost,
                description="transcript match",
                timestamp=-1.0,
                transcript_match=True,
                additive=path in hits,
            )
    except Exception:
        pass

    conn.close()
    ordered = sorted(hits.values(), key=lambda item: float(item.get("score", 0.0)), reverse=True)
    ordered = [h for h in ordered if float(h.get("score", 0)) > 0]
    return {"hits": ordered[:top_k]}


def _cleanup_tmpdir(tmpdir: Path) -> None:
    try:
        shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception:
        pass
