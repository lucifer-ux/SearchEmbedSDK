# config.py — Central configuration loader for ContextCore
#
# Resolution order for every path:
#   1. contextcore.yaml  (if present next to this file or at CWD)
#   2. Environment variable
#   3. Sensible default (current working directory)

import os
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).parent.resolve()

# ---------------------------------------------------------------------------
# YAML loader (optional dependency — pure-Python fallback if PyYAML absent)
# ---------------------------------------------------------------------------
_config_cache: Optional[Dict[str, Any]] = None


def _find_config_file() -> Optional[Path]:
    """Search for config in env override, user config, project root, then CWD."""
    env_path = os.getenv("CONTEXTCORE_CONFIG")
    if env_path:
        p = Path(env_path).expanduser().resolve()
        if p.is_file():
            return p

    candidates = [
        Path.home() / ".contextcore" / "contextcore.yaml",
        ROOT / "contextcore.yaml",
        Path.cwd() / "contextcore.yaml",
    ]
    for p in candidates:
        if p.is_file():
            return p
    return None


def _load_config() -> Dict[str, Any]:
    global _config_cache
    if _config_cache is not None:
        return _config_cache

    cfg_file = _find_config_file()
    if cfg_file is None:
        _config_cache = {}
        return _config_cache

    try:
        import yaml  # PyYAML
        with open(cfg_file, "r", encoding="utf-8") as fh:
            _config_cache = yaml.safe_load(fh) or {}
    except ImportError:
        # Minimal key: value parser for flat / shallow YAML
        _config_cache = _fallback_yaml_parse(cfg_file)
    except Exception:
        _config_cache = _fallback_yaml_parse(cfg_file)
    return _config_cache


def _fallback_yaml_parse(path: Path) -> Dict[str, Any]:
    """Simple parser for the small top-level YAML shape used by ContextCore."""
    result: Dict[str, Any] = {}
    current_list_key: str | None = None

    def _parse_scalar(raw: str) -> Any:
        value = raw.strip().strip("'\"")
        lower = value.lower()
        if lower == "true":
            return True
        if lower == "false":
            return False
        if lower in {"[]", ""}:
            return [] if lower == "[]" else ""
        try:
            if "." in value:
                return float(value)
            return int(value)
        except ValueError:
            return value

    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue

            stripped = line.strip()

            if current_list_key and stripped.startswith("- "):
                result.setdefault(current_list_key, [])
                result[current_list_key].append(_parse_scalar(stripped[2:]))
                continue

            if line[:1].isspace() and current_list_key:
                continue

            current_list_key = None
            if ":" in stripped:
                key, _, value = stripped.partition(":")
                key = key.strip()
                value = value.strip()
                if value == "":
                    result[key] = []
                    current_list_key = key
                else:
                    result[key] = _parse_scalar(value)
    except Exception:
        pass
    return result


def _nested_get(cfg: Dict[str, Any], dotted_key: str, default: Any = None) -> Any:
    """Retrieve a nested value using dot notation.

    Example: ``media.video.watch_directories``.
    """
    keys = dotted_key.split(".")
    current: Any = cfg
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return default
        if current is None:
            return default
    return current


# ---------------------------------------------------------------------------
# Public helpers — used by unimain.py, video_index.py, audio worker, etc.
# ---------------------------------------------------------------------------

def get_video_directories() -> list[Path]:
    """
    Return video watch directories.
    Precedence: CONTEXTCORE_VIDEO_DIR env -> contextcore.yaml -> CWD.
    """
    env = os.getenv("CONTEXTCORE_VIDEO_DIR")
    if env:
        return [Path(env).expanduser().resolve()]

    cfg = _load_config()
    yaml_dirs = cfg.get("video_directories")
    if yaml_dirs:
        if isinstance(yaml_dirs, list):
            return [Path(d).expanduser().resolve() for d in yaml_dirs]
        if isinstance(yaml_dirs, str):
            return [Path(yaml_dirs).expanduser().resolve()]

    return [Path.cwd()]


def get_audio_directories() -> list[Path]:
    """
    Return audio watch directories.
    Precedence: contextcore.yaml → CONTEXTCORE_AUDIO_DIR env → CWD.
    """
    cfg = _load_config()
    yaml_dirs = cfg.get("audio_directories")
    if yaml_dirs:
        if isinstance(yaml_dirs, list):
            return [Path(d).expanduser().resolve() for d in yaml_dirs]
        if isinstance(yaml_dirs, str):
            return [Path(yaml_dirs).expanduser().resolve()]

    env = os.getenv("CONTEXTCORE_AUDIO_DIR")
    if env:
        return [Path(env).expanduser().resolve()]

    return [Path.cwd()]


def get_image_directory() -> Path:
    """
    Return image root directory.
    Precedence: CONTEXTCORE_IMAGE_DIR env -> contextcore.yaml -> CWD.
    """
    env = os.getenv("CONTEXTCORE_IMAGE_DIR")
    if env:
        return Path(env).expanduser().resolve()

    cfg = _load_config()
    yaml_dir = cfg.get("organized_root")
    if yaml_dir:
        return Path(yaml_dir).expanduser().resolve()

    return Path.cwd()


def get_organized_root() -> Path:
    """
    Return the organized-files root.
    Precedence: contextcore.yaml → CONTEXTCORE_ORGANIZED_ROOT env → CWD.
    """
    cfg = _load_config()
    yaml_dir = cfg.get("organized_root")
    if yaml_dir:
        return Path(yaml_dir).expanduser().resolve()

    env = os.getenv("CONTEXTCORE_ORGANIZED_ROOT")
    if env:
        return Path(env).expanduser().resolve()

    watch_dirs = get_watch_directories()
    if watch_dirs:
        return watch_dirs[0]

    return Path.cwd()


def get_storage_dir() -> Path:
    """
    Return the global storage directory where index.db lives.
    Precedence: CONTEXTCORE_STORAGE_DIR env -> contextcore.yaml -> ~/.contextcore.
    """
    env = os.getenv("CONTEXTCORE_STORAGE_DIR")
    if env:
        return Path(env).expanduser().resolve()

    cfg = _load_config()
    yaml_dir = cfg.get("storage_dir")
    if yaml_dir:
        return Path(yaml_dir).expanduser().resolve()

    return Path.home() / ".contextcore"


def get_storage_path() -> Path:
    cfg = _load_config()
    yaml_path = cfg.get("storage_path")
    if yaml_path:
        return Path(yaml_path).expanduser().resolve()

    env = os.getenv("CONTEXTCORE_STORAGE_PATH")
    if env:
        return Path(env).expanduser().resolve()

    return get_storage_dir() / "index.db"


def get_dedup_threshold() -> float:
    """
    Return frame dedup cosine-similarity threshold.
    Precedence: contextcore.yaml → CONTEXTCORE_DEDUP_THRESHOLD env → 0.85.
    """
    cfg = _load_config()
    val = cfg.get("dedup_threshold")
    if val is not None:
        try:
            return float(val)
        except (ValueError, TypeError):
            pass

    env = os.getenv("CONTEXTCORE_DEDUP_THRESHOLD")
    if env:
        try:
            return float(env)
        except ValueError:
            pass

    return 0.85


def get_ffmpeg_path() -> Optional[Path]:
    """Return configured ffmpeg path, if any."""
    cfg = _load_config()
    raw = cfg.get("ffmpeg_path")
    if raw:
        p = Path(str(raw)).expanduser().resolve()
        if p.exists():
            return p

    env = os.getenv("CONTEXTCORE_FFMPEG_PATH")
    if env:
        p = Path(env).expanduser().resolve()
        if p.exists():
            return p
    return None


def get_ffprobe_path() -> Optional[Path]:
    """Return configured ffprobe path, if any."""
    cfg = _load_config()
    raw = cfg.get("ffprobe_path")
    if raw:
        p = Path(str(raw)).expanduser().resolve()
        if p.exists():
            return p

    env = os.getenv("CONTEXTCORE_FFPROBE_PATH")
    if env:
        p = Path(env).expanduser().resolve()
        if p.exists():
            return p
    return None


def get_video_ocr_enabled() -> bool:
    """Return whether OCR should be attempted for video frames."""
    cfg = _load_config()
    val = cfg.get("video_ocr_enabled")
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        lowered = val.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False

    env = os.getenv("CONTEXTCORE_VIDEO_OCR_ENABLED")
    if env:
        lowered = env.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    return True


def _get_config_bool(key: str, env_key: str | None = None, default: bool = False) -> bool:
    cfg = _load_config()
    val = cfg.get(key)
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        lowered = val.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False

    if env_key:
        env = os.getenv(env_key)
        if env:
            lowered = env.strip().lower()
            if lowered in {"true", "1", "yes", "on"}:
                return True
            if lowered in {"false", "0", "no", "off"}:
                return False
    return default


def get_code_directories() -> list[Path]:
    """
    Return code watch directories.
    Precedence: contextcore.yaml -> CONTEXTCORE_CODE_DIR env -> organized_root.
    """
    cfg = _load_config()
    yaml_dirs = cfg.get("code_directories")
    if yaml_dirs:
        if isinstance(yaml_dirs, list):
            return [Path(d).expanduser().resolve() for d in yaml_dirs]
        if isinstance(yaml_dirs, str):
            return [Path(yaml_dirs).expanduser().resolve()]

    env = os.getenv("CONTEXTCORE_CODE_DIR")
    if env:
        return [Path(env).expanduser().resolve()]

    return [get_organized_root()]


def get_enable_code() -> bool:
    """Return whether code indexing is enabled."""
    return _get_config_bool("enable_code", "CONTEXTCORE_ENABLE_CODE", default=False)


def get_enable_text() -> bool:
    return _get_config_bool("enable_text", "CONTEXTCORE_ENABLE_TEXT", default=True)


def get_enable_image() -> bool:
    return _get_config_bool("enable_image", "CONTEXTCORE_ENABLE_IMAGE", default=False)


def get_enable_audio() -> bool:
    return _get_config_bool("enable_audio", "CONTEXTCORE_ENABLE_AUDIO", default=False)


def get_enable_video() -> bool:
    return _get_config_bool("enable_video", "CONTEXTCORE_ENABLE_VIDEO", default=False)


def get_watch_directories() -> list[Path]:
    """
    Return the configured top-level watch directories.
    Precedence: contextcore.yaml -> watch_directories -> organized_root -> CWD.
    """
    cfg = _load_config()
    yaml_dirs = cfg.get("watch_directories")
    if yaml_dirs:
        if isinstance(yaml_dirs, list):
            return [Path(d).expanduser().resolve() for d in yaml_dirs]
        if isinstance(yaml_dirs, str):
            return [Path(yaml_dirs).expanduser().resolve()]

    yaml_dir = cfg.get("organized_root")
    if yaml_dir:
        return [Path(yaml_dir).expanduser().resolve()]

    env = os.getenv("CONTEXTCORE_WATCH_DIR")
    if env:
        return [Path(env).expanduser().resolve()]

    return [Path.cwd()]


def add_watch_directory(path: Path | str) -> Optional[Path]:
    """Append a directory to the configured watch lists."""
    new_dir = Path(path).expanduser().resolve()
    current = _load_config().copy()

    def _append_unique(key: str, value: str) -> None:
        raw = current.get(key, [])
        if isinstance(raw, str):
            items = [raw] if raw else []
        elif isinstance(raw, list):
            items = [str(v) for v in raw]
        else:
            items = []
        if value not in items:
            items.append(value)
        current[key] = items

    new_dir_str = str(new_dir)
    _append_unique("watch_directories", new_dir_str)
    if current.get("enable_audio"):
        _append_unique("audio_directories", new_dir_str)
    if current.get("enable_video"):
        _append_unique("video_directories", new_dir_str)
    if current.get("enable_code"):
        _append_unique("code_directories", new_dir_str)
    if not current.get("organized_root"):
        current["organized_root"] = new_dir_str

    return update_config_values(current)


def get_config() -> Dict[str, Any]:
    """Return a copy of the current loaded config."""
    return _load_config().copy()


def update_config_values(updates: Dict[str, Any]) -> Optional[Path]:
    """Merge top-level config values into contextcore.yaml."""
    cfg_path = _find_config_file() or (Path.home() / ".contextcore" / "contextcore.yaml")
    cfg_path.parent.mkdir(parents=True, exist_ok=True)

    current = _load_config().copy()
    current.update(updates)

    serializable: Dict[str, Any] = {}
    for key, value in current.items():
        if isinstance(value, Path):
            serializable[key] = str(value)
        elif isinstance(value, list):
            serializable[key] = [str(v) if isinstance(v, Path) else v for v in value]
        else:
            serializable[key] = value

    try:
        import yaml  # type: ignore

        cfg_path.write_text(
            yaml.safe_dump(serializable, sort_keys=False, allow_unicode=False),
            encoding="utf-8",
        )
    except Exception:
        lines: list[str] = []
        for key, value in serializable.items():
            if isinstance(value, bool):
                lines.append(f"{key}: {'true' if value else 'false'}")
            elif isinstance(value, (int, float)):
                lines.append(f"{key}: {value}")
            elif isinstance(value, list):
                lines.append(f"{key}:")
                if value:
                    for list_item in value:
                        text = str(list_item).replace("'", "''")
                        lines.append(f"  - '{text}'")
                else:
                    lines.append("  []")
            elif value is None:
                lines.append(f"{key}: ''")
            else:
                text = str(value).replace("'", "''")
                lines.append(f"{key}: '{text}'")
        cfg_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    reload_config()
    return cfg_path


def reload_config() -> Dict[str, Any]:
    """Force-reload contextcore.yaml (useful after editing the file)."""
    global _config_cache
    _config_cache = None
    return _load_config()
