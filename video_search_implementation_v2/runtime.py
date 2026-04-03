from __future__ import annotations

import json
import os
import platform
import shutil
from pathlib import Path
from typing import Any

from config import (
    get_ffmpeg_path,
    get_ffprobe_path,
    update_config_values,
    get_storage_dir,
)

STORAGE_DIR = get_storage_dir() / "video_search_implementation_v2" / "storage"
STORAGE_DIR.mkdir(parents=True, exist_ok=True)
RUNTIME_STATE_PATH = STORAGE_DIR / "runtime_state.json"
CLIP_MODEL_ID = "openai/clip-vit-base-patch32"


def _load_runtime_state() -> dict[str, Any]:
    try:
        return json.loads(RUNTIME_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_runtime_state(state: dict[str, Any]) -> None:
    RUNTIME_STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def mark_runtime_state(**updates: Any) -> None:
    state = _load_runtime_state()
    state.update(updates)
    _save_runtime_state(state)


def get_runtime_state() -> dict[str, Any]:
    return _load_runtime_state()


def _refresh_path_for_current_process() -> None:
    try:
        from cli.env import refresh_process_path

        refresh_process_path()
    except Exception:
        pass


def _known_binary_candidates(binary: str) -> list[Path]:
    system = platform.system()
    names = [binary]
    if system == "Windows" and not binary.lower().endswith(".exe"):
        names = [f"{binary}.exe"]

    candidates: list[Path] = []
    if system == "Windows":
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        user = Path.home()
        winget_root = local / "Microsoft" / "WinGet" / "Packages"
        if winget_root.exists():
            for package_dir in winget_root.glob("*FFmpeg*"):
                for name in names:
                    candidates.extend(package_dir.glob(f"**/bin/{name}"))

        direct = [
            user / "scoop" / "apps" / "ffmpeg" / "current" / "bin",
            Path(r"C:\ProgramData\chocolatey\bin"),
            Path(r"C:\Program Files\ffmpeg\bin"),
            Path(r"C:\Program Files\Gyan\FFmpeg\bin"),
            Path(r"C:\Program Files (x86)\ffmpeg\bin"),
        ]
        for root in direct:
            for name in names:
                candidate = root / name
                if candidate.exists():
                    candidates.append(candidate)
    else:
        roots = [Path("/usr/local/bin"), Path("/usr/bin"), Path("/opt/homebrew/bin"), Path("/snap/bin")]
        for root in roots:
            for name in names:
                p = root / name
                if p.exists():
                    candidates.append(p)

    ordered: list[Path] = []
    seen: set[str] = set()
    for p in candidates:
        key = str(p).lower() if system == "Windows" else str(p)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(p)
    return ordered


def _coerce_existing_path(raw: str | os.PathLike[str] | None) -> Path | None:
    if not raw:
        return None
    try:
        p = Path(raw).expanduser().resolve()
    except Exception:
        return None
    return p if p.exists() else None


def resolve_binary_path(
    binary_name: str,
    configured_path: Path | None = None,
    env_var: str | None = None,
) -> Path | None:
    """Resolve tool path without relying solely on inherited PATH."""
    _refresh_path_for_current_process()

    if configured_path and configured_path.exists():
        return configured_path.resolve()

    if env_var:
        env_path = _coerce_existing_path(os.getenv(env_var))
        if env_path:
            return env_path

    for candidate in _known_binary_candidates(binary_name):
        if candidate.exists():
            return candidate.resolve()

    which_hit = shutil.which(binary_name)
    if which_hit:
        return Path(which_hit).resolve()
    return None


def resolve_ffmpeg_path() -> Path | None:
    return resolve_binary_path(
        "ffmpeg",
        configured_path=get_ffmpeg_path(),
        env_var="CONTEXTCORE_FFMPEG_PATH",
    )


def resolve_ffprobe_path(ffmpeg_path: Path | None = None) -> Path | None:
    configured = get_ffprobe_path()
    if configured and configured.exists():
        return configured.resolve()

    env_path = _coerce_existing_path(os.getenv("CONTEXTCORE_FFPROBE_PATH"))
    if env_path:
        return env_path

    ffmpeg_path = ffmpeg_path or resolve_ffmpeg_path()
    if ffmpeg_path:
        sibling = ffmpeg_path.with_name("ffprobe.exe" if ffmpeg_path.suffix.lower() == ".exe" else "ffprobe")
        if sibling.exists():
            return sibling.resolve()

    return resolve_binary_path("ffprobe", env_var="CONTEXTCORE_FFPROBE_PATH")


def persist_resolved_video_tools() -> dict[str, str] | None:
    ffmpeg = resolve_ffmpeg_path()
    if not ffmpeg:
        return None

    updates: dict[str, str] = {"ffmpeg_path": str(ffmpeg)}
    ffprobe = resolve_ffprobe_path(ffmpeg)
    if ffprobe:
        updates["ffprobe_path"] = str(ffprobe)
    update_config_values(updates)
    return updates


def prewarm_clip_model() -> tuple[bool, str | None]:
    try:
        from transformers import CLIPModel, CLIPProcessor

        CLIPProcessor.from_pretrained(CLIP_MODEL_ID, use_fast=False)
        CLIPModel.from_pretrained(CLIP_MODEL_ID, use_safetensors=False)
        mark_runtime_state(clip_ready=True, clip_model_id=CLIP_MODEL_ID, clip_error=None)
        return True, None
    except Exception as exc:
        mark_runtime_state(clip_ready=False, clip_model_id=CLIP_MODEL_ID, clip_error=str(exc))
        return False, str(exc)


def clip_model_ready() -> tuple[bool, str | None]:
    state = get_runtime_state()
    if state.get("clip_ready"):
        return True, None

    try:
        from transformers import CLIPModel, CLIPProcessor

        CLIPProcessor.from_pretrained(CLIP_MODEL_ID, local_files_only=True, use_fast=False)
        CLIPModel.from_pretrained(CLIP_MODEL_ID, local_files_only=True, use_safetensors=False)
        mark_runtime_state(clip_ready=True, clip_model_id=CLIP_MODEL_ID, clip_error=None)
        return True, None
    except Exception as exc:
        mark_runtime_state(clip_ready=False, clip_model_id=CLIP_MODEL_ID, clip_error=str(exc))
        return False, str(exc)


def video_runtime_status() -> dict[str, Any]:
    ffmpeg = resolve_ffmpeg_path()
    ffprobe = resolve_ffprobe_path(ffmpeg)
    clip_ready, clip_error = clip_model_ready()
    state = get_runtime_state()
    return {
        "ffmpeg_ready": ffmpeg is not None,
        "ffmpeg_path": str(ffmpeg) if ffmpeg else None,
        "ffprobe_path": str(ffprobe) if ffprobe else None,
        "clip_ready": clip_ready,
        "clip_error": clip_error or state.get("clip_error"),
    }
