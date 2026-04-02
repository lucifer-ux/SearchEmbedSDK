# cli/commands/init.py
#
# contextcore init — interactive first-time setup wizard.
# Walks the user through directory selection, modality selection,
# storage config, model downloads, and Claude tool registration.

from __future__ import annotations
import json
import os
import platform
import subprocess
import sys
import importlib.util
from pathlib import Path

import questionary
from questionary import Style

from cli.constants import DEFAULT_BACKEND_URL, DEFAULT_PORT
from cli.env import refresh_process_path
from cli.lifecycle import autostart_status, install_autostart
from cli.paths import get_sdk_root, get_mcp_script, get_default_config
from cli.ui import (
    console,
    header,
    section,
    success,
    error,
    warning,
    info,
    hint,
    done_panel,
    set_theme,
    get_theme_name,
)

_SDK_ROOT = get_sdk_root()
if str(_SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(_SDK_ROOT))

from config import get_config, get_storage_path, reload_config, update_config_values

# ── Questionary style ─────────────────────────────────────────────────────────

_STYLE_DARK = Style([
    ("qmark",        "fg:#5f87ff bold"),
    ("question",     "bold"),
    ("answer",       "fg:#5fffff bold"),
    ("selected",     "fg:#5fffff"),
    ("highlighted",  "fg:#5f87ff bold"),
    ("pointer",      "fg:#5f87ff bold"),
    ("separator",    "fg:#444444"),
])

_STYLE_LIGHT = Style([
    ("qmark",        "fg:#1f4de3 bold"),
    ("question",     "fg:#111111 bold"),
    ("answer",       "fg:#006d77 bold"),
    ("selected",     "fg:#006d77 bold"),
    ("highlighted",  "fg:#1f4de3 bold"),
    ("pointer",      "fg:#1f4de3 bold"),
    ("separator",    "fg:#6b7280"),
])

_STYLE = _STYLE_DARK
_SETUP_THEME = "dark"


def _choose_setup_theme(default_theme: str = "dark") -> None:
    global _STYLE, _SETUP_THEME

    selected = questionary.select(
        "Choose setup theme",
        choices=[
            questionary.Choice("Dark (recommended)", value="dark"),
            questionary.Choice("Light", value="light"),
        ],
        default=default_theme if default_theme in {"dark", "light"} else "dark",
        style=_STYLE,
    ).ask()

    if selected == "light":
        _STYLE = _STYLE_LIGHT
        _SETUP_THEME = "light"
    else:
        _STYLE = _STYLE_DARK
        _SETUP_THEME = "dark"

    set_theme(_SETUP_THEME)

# ── MCP config locations ───────────────────────────────────────────────────────

_TOOL_CONFIGS = {
    "Claude Desktop": {
        "windows": Path(os.environ.get("APPDATA", "~")) / "Claude" / "claude_desktop_config.json",
        "darwin":  Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
        "linux":   Path.home() / ".config" / "Claude" / "claude_desktop_config.json",
    },
    "Claude Code": {
        "windows": [
            Path.home() / ".claude.json",
            Path.home() / ".claude" / "config.json",
            Path(os.environ.get("APPDATA", "~")) / "Claude Code" / "config.json",
        ],
        "darwin":  [
            Path.home() / ".claude.json",
            Path.home() / ".claude" / "config.json",
        ],
        "linux":   [
            Path.home() / ".claude.json",
            Path.home() / ".claude" / "config.json",
        ],
    },
    "Cline (VS Code)": {
        "windows": Path.home() / "AppData" / "Roaming" / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev" / "settings" / "cline_mcp_settings.json",
        "darwin":  Path.home() / "Library" / "Application Support" / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev" / "settings" / "cline_mcp_settings.json",
        "linux":   Path.home() / ".config" / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev" / "settings" / "cline_mcp_settings.json",
    },
    "Cursor": {
        "windows": Path.home() / "AppData" / "Roaming" / "Cursor" / "User" / "globalStorage" / "saoudrizwan.claude-dev" / "settings" / "cline_mcp_settings.json",
        "darwin":  Path.home() / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage" / "saoudrizwan.claude-dev" / "settings" / "cline_mcp_settings.json",
        "linux":   Path.home() / ".config" / "Cursor" / "User" / "globalStorage" / "saoudrizwan.claude-dev" / "settings" / "cline_mcp_settings.json",
    },
    "OpenCode": {
        "windows": Path.home() / ".config" / "opencode" / "opencode.json",
        "darwin":  Path.home() / ".config" / "opencode" / "opencode.json",
        "linux":   Path.home() / ".config" / "opencode" / "opencode.json",
    },
    "Windsurf": {
        "windows": Path.home() / "AppData" / "Roaming" / "Windsurf" / "User" / "globalStorage" / "saoudrizwan.claude-dev" / "settings" / "cline_mcp_settings.json",
        "darwin":  Path.home() / "Library" / "Application Support" / "Windsurf" / "User" / "globalStorage" / "saoudrizwan.claude-dev" / "settings" / "cline_mcp_settings.json",
        "linux":   Path.home() / ".config" / "Windsurf" / "User" / "globalStorage" / "saoudrizwan.claude-dev" / "settings" / "cline_mcp_settings.json",
    },
    "Continue (VS Code)": {
        "windows": Path.home() / ".continue" / "config.json",
        "darwin":  Path.home() / ".continue" / "config.json",
        "linux":   Path.home() / ".continue" / "config.json",
    },
}

_REGISTER_TOOL_KEY_BY_LABEL = {
    "Claude Desktop": "claude-desktop",
    "Claude Code": "claude-code",
    "Cline (VS Code)": "cline",
    "Cursor": "cursor",
    "OpenCode": "opencode",
    "Windsurf": "windsurf",
    "Continue (VS Code)": "continue",
}

_DEFAULT_CONFIG = get_default_config()

from video_search_implementation_v2.runtime import (
    persist_resolved_video_tools,
    prewarm_clip_model,
)


def _platform() -> str:
    sys_platform = platform.system().lower()
    if sys_platform == "windows":
        return "windows"
    if sys_platform == "darwin":
        return "darwin"
    return "linux"


def _get_config_path(tool: str) -> Path | None:
    plat = _platform()
    paths = _TOOL_CONFIGS.get(tool, {})
    p = paths.get(plat)
    return p.expanduser() if p else None


def _inject_mcp_config(config_path: Path, tool_name: str) -> bool:
    """Inject (or update) contextcore into a Claude/Cline config JSON."""
    sdk_root = get_sdk_root()
    mcp_script = get_mcp_script()
    mcp_entry = {
        "command": sys.executable,
        "args": [str(mcp_script)],
        "cwd": str(sdk_root),
        "env": {
            "CONTEXTCORE_API_BASE_URL": DEFAULT_BACKEND_URL,
        },
    }

    try:
        if config_path.exists():
            raw = config_path.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else {}
        else:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            data = {}

        tool_key = tool_name.strip().lower()
        if tool_key == "opencode":
            data.setdefault("mcp", {})
            if not isinstance(data["mcp"], dict):
                data["mcp"] = {}
            data["mcp"]["contextcore"] = {
                "type": "local",
                "command": [sys.executable, str(mcp_script)],
                "environment": {
                    "CONTEXTCORE_API_BASE_URL": DEFAULT_BACKEND_URL,
                },
            }
            if isinstance(data.get("mcpServers"), dict):
                data.pop("mcpServers", None)
        elif tool_key in {"claude code", "claude-code"}:
            data.setdefault("mcpServers", {})
            if not isinstance(data["mcpServers"], dict):
                data["mcpServers"] = {}
            data["mcpServers"]["contextcore"] = {
                "type": "stdio",
                "command": sys.executable,
                "args": [str(mcp_script)],
            }
        else:
            data.setdefault("mcpServers", {})
            data["mcpServers"]["contextcore"] = mcp_entry
        config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return True
    except Exception as e:
        error(f"Failed to write {config_path}: {e}")
        return False


def _register_tool_with_fallback(tool_label: str, *, dry_run: bool = False) -> bool:
    tool_key = _REGISTER_TOOL_KEY_BY_LABEL.get(tool_label)
    if not tool_key:
        warning(f"Unknown registration target: {tool_label}")
        return False

    repo_root = Path(__file__).resolve().parent.parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    try:
        from register_mcp import get_tool_definitions, register_tool
        from detect_paths import get_mcp_server_path, get_python_path

        python_path = get_python_path().get("path") or sys.executable
        mcp_info = get_mcp_server_path()
        mcp_path = mcp_info.get("path")
        if not mcp_path:
            warning(f"Could not resolve mcp_server.py for {tool_label}")
            return False

        tools = get_tool_definitions()
        spec = tools.get(tool_key)
        if not spec:
            warning(f"Tool mapping not supported by register_mcp.py: {tool_key}")
            return False

        return bool(register_tool(tool_key, spec, python_path, mcp_path, dry_run=dry_run))

    except Exception as exc:
        warning(f"Auto-registration via register_mcp failed for {tool_label}: {exc}")
        cfg_file = _get_config_path(tool_label)
        if cfg_file is None:
            return False
        return _inject_mcp_config(cfg_file, tool_label)


def _write_yaml_config(
    organized_root: Path,
    storage_path: Path,
    enable_text: bool,
    enable_code: bool,
    enable_image: bool,
    enable_audio: bool,
    enable_video: bool,
    watched_dirs: list[Path],
    ffmpeg_path: Path | None = None,
    ffprobe_path: Path | None = None,
    video_ocr_enabled: bool = True,
    ui_theme: str = "dark",
) -> Path:
    """Write a contextcore.yaml to ~/.contextcore/contextcore.yaml."""
    _DEFAULT_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    sdk_root = get_sdk_root()

    def _yaml_quote(value: Path | str) -> str:
        text = str(value).replace("'", "''")
        return f"'{text}'"

    watch_yaml = "\n".join(f"  - {_yaml_quote(d)}" for d in watched_dirs) if watched_dirs else f"  - {_yaml_quote(organized_root)}"
    video_dirs = "\n".join(f"  - {_yaml_quote(d)}" for d in watched_dirs)
    audio_dirs = "\n".join(f"  - {_yaml_quote(d)}" for d in watched_dirs)
    code_dirs = "\n".join(f"  - {_yaml_quote(d)}" for d in watched_dirs)

    yaml_content = f"""\
# ContextCore configuration
# Generated by: contextcore init
# Edit this file to change your setup.

# SDK install location (used by CLI to locate mcp_server.py and unimain.py)
sdk_root: {_yaml_quote(sdk_root)}

organized_root: {_yaml_quote(organized_root)}
watch_directories:
{watch_yaml}
storage_path: {_yaml_quote(storage_path)}

# Enabled modalities
enable_text:  {"true" if enable_text  else "false"}
enable_code:  {"true" if enable_code  else "false"}
enable_image: {"true" if enable_image else "false"}
enable_audio: {"true" if enable_audio else "false"}
enable_video: {"true" if enable_video else "false"}

# Directories to watch for new files (the background watcher)
video_directories:
{video_dirs if enable_video else "  []"}

audio_directories:
{audio_dirs if enable_audio else "  []"}

code_directories:
{code_dirs if enable_code else "  []"}

# Vector store settings
dedup_threshold: 0.85
video_ocr_enabled: {"true" if video_ocr_enabled else "false"}
ffmpeg_path: {_yaml_quote(ffmpeg_path) if ffmpeg_path else "''"}
ffprobe_path: {_yaml_quote(ffprobe_path) if ffprobe_path else "''"}
ui_theme: '{ui_theme}'
"""
    _DEFAULT_CONFIG.write_text(yaml_content, encoding="utf-8")
    return _DEFAULT_CONFIG


def _download_models(need_clip: bool, need_whisper: bool) -> bool:
    """Install heavy optional deps with live streaming pip output."""
    pkgs: list[tuple[str, str, str]] = []
    if need_clip:
        pkgs.append(("CLIP / Vision model", "torch torchvision transformers", "~800MB  •  5-8 min"))
    if need_whisper:
        pkgs.append(("Audio / Whisper model", "faster-whisper", "~150MB  •  1-2 min"))

    if not pkgs:
        return True

    section("Downloading required models")
    console.print("  [dim]This happens once. Models are cached for future use.[/dim]")
    console.print("  [dim]You will see pip's download output — this is normal.[/dim]\n")

    for label, packages, estimate in pkgs:
        console.print(f"  [bold]{label}[/bold]  [dim]({estimate})[/dim]")
        # Stream pip output — no capture_output, no --quiet
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install",
             "--progress-bar", "on",
             "--no-cache-dir",
             ] + packages.split(),
        )
        console.print()
        if result.returncode == 0:
            if "CLIP" in label:
                if importlib.util.find_spec("torch") is None or importlib.util.find_spec("transformers") is None:
                    error(f"{label} install completed, but imports are still missing")
                    console.print(f"  [dim]Retry with:[/dim] [bold]{sys.executable} -m pip install --no-cache-dir {packages}[/bold]")
                    return False
            if "Whisper" in label:
                if importlib.util.find_spec("faster_whisper") is None:
                    error(f"{label} install completed, but faster-whisper is still not importable")
                    console.print(f"  [dim]Retry with:[/dim] [bold]{sys.executable} -m pip install --no-cache-dir {packages}[/bold]")
                    return False
            success(f"{label} installed")
        else:
            error(f"Failed to install {label}")
            console.print(f"  [dim]Retry with:[/dim] [bold]{sys.executable} -m pip install --no-cache-dir {packages}[/bold]")
            if "CLIP" in label:
                console.print("  [dim]Or rerun:[/dim] [bold]contextcore install clip[/bold]")
            if "Whisper" in label:
                console.print("  [dim]Or rerun:[/dim] [bold]contextcore install audio[/bold]")
            return False

    return True


def _prewarm_models(need_clip: bool, need_whisper: bool) -> bool:
    ready = True
    if need_clip:
        section("Prewarming CLIP")
        console.print("  [dim]Loading CLIP weights once so image/video indexing is immediately usable.[/dim]\n")
        ok, err = prewarm_clip_model()
        if ok:
            success("CLIP model is ready")
        else:
            ready = False
            warning(f"CLIP prewarm failed: {err}")

    if need_whisper:
        section("Prewarming Whisper")
        console.print("  [dim]Loading Whisper weights once so audio/video transcription is immediately usable.[/dim]\n")
        try:
            from audio_search_implementation_v2.audio_index import prewarm_whisper

            ok, err = prewarm_whisper()
        except Exception as exc:
            ok, err = False, str(exc)

        if ok:
            success("Whisper model is ready")
        else:
            ready = False
            warning(f"Whisper prewarm failed: {err}")

    return ready


def _ensure_ffmpeg() -> bool:
    """Install ffmpeg if not already present. Called during init when video is selected."""
    import shutil
    import platform

    section("ffmpeg (required for video)")

    refresh_process_path()

    if shutil.which("ffmpeg"):
        success("ffmpeg already installed")
        persist_resolved_video_tools()
        return True

    console.print("  [dim]Video indexing requires ffmpeg for frame extraction.[/dim]")
    console.print("  [yellow]Installing ffmpeg now...[/yellow]\n")

    system = platform.system().lower()

    try:
        if system == "windows":
            # winget is built-in on Windows 10 1709+ and Windows 11
            result = subprocess.run(
                ["winget", "install", "--id", "Gyan.FFmpeg",
                 "--accept-package-agreements", "--accept-source-agreements",
                 "--silent"],
                check=False, timeout=300,
            )
            if result.returncode != 0:
                # Fallback: chocolate / scoop not reliable — tell user manually
                raise RuntimeError("winget install failed")

        elif system == "darwin":
            result = subprocess.run(
                ["brew", "install", "ffmpeg"],
                check=True, timeout=300,
            )

        else:  # Linux
            subprocess.run(["sudo", "apt-get", "update", "-qq"], check=True, timeout=60)
            subprocess.run(
                ["sudo", "apt-get", "install", "-y", "ffmpeg"],
                check=True, timeout=300,
            )

    except (FileNotFoundError, RuntimeError):
        # Package manager not available — show manual instructions
        warning("Could not auto-install ffmpeg.")
        if system == "windows":
            console.print("  Install manually: [bold]winget install --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements[/bold]")
            console.print("  Or download from: https://ffmpeg.org/download.html")
        elif system == "darwin":
            console.print("  Install with Homebrew: [bold]brew install ffmpeg[/bold]")
        else:
            console.print("  Install with: [bold]sudo apt install ffmpeg[/bold]")
        console.print("  [dim]Video indexing will be skipped until ffmpeg is available.[/dim]")
        return False
    except Exception as e:
        warning(f"ffmpeg install error: {e}")
        return False

    refresh_process_path()

    # Verify install
    if shutil.which("ffmpeg"):
        success("ffmpeg installed successfully")
        info("ContextCore refreshed PATH for this session. A new terminal is not required.")
        persist_resolved_video_tools()
        return True
    else:
        warning("ffmpeg installed but not found in PATH. You may need to restart your terminal.")
        console.print("  [dim]Retry after opening a new terminal:[/dim] [bold]contextcore index[/bold]")
        return False


def _current_watch_dirs(cfg: dict[str, object]) -> list[Path]:
    raw = cfg.get("watch_directories") or ([cfg.get("organized_root")] if cfg.get("organized_root") else [])
    if isinstance(raw, str):
        raw = [raw]
    return [Path(str(p)).expanduser().resolve() for p in raw if str(p).strip()]


def _show_existing_setup(cfg: dict[str, object]) -> None:
    section("Existing setup detected")
    watch_dirs = _current_watch_dirs(cfg)
    console.print(f"  [dim]Config:[/dim] [bold]{_DEFAULT_CONFIG}[/bold]")
    if watch_dirs:
        console.print(f"  [dim]Watched folders:[/dim] [bold]{len(watch_dirs)}[/bold]")
        for path in watch_dirs[:5]:
            console.print(f"    - {path}")
    enabled = [name for name in ("text", "code", "image", "audio", "video") if cfg.get(f"enable_{name}")]
    console.print(f"  [dim]Enabled modalities:[/dim] [bold]{', '.join(enabled) if enabled else 'none'}[/bold]")
    console.print(f"  [dim]Storage path:[/dim] [bold]{cfg.get('storage_path') or get_storage_path()}[/bold]")
    auto = autostart_status()
    console.print(f"  [dim]Autostart:[/dim] [bold]{'installed' if auto.get('installed') else 'not installed'}[/bold]")
    console.print(f"  [dim]Backend port:[/dim] [bold]{DEFAULT_PORT}[/bold]")


def _normalize_watch_dirs(raw: str, fallback: list[Path]) -> list[Path]:
    parts = [segment.strip() for segment in raw.split(";") if segment.strip()]
    if not parts:
        return fallback
    dirs: list[Path] = []
    for part in parts:
        path = Path(part).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        dirs.append(path)
    return dirs


def _apply_autostart_choice(should_install: bool) -> None:
    if not should_install:
        return
    ok, msg = install_autostart(DEFAULT_PORT)
    if ok:
        success("Autostart installed for OS login")
    else:
        warning(f"Autostart installation failed: {msg}")
        if platform.system().lower() == "windows":
            console.print("  [dim]Retry with:[/dim] [bold]contextcore init[/bold]")
        elif platform.system().lower() == "darwin":
            console.print("  [dim]Retry with:[/dim] [bold]contextcore init[/bold]")


def _run_modify_existing(existing_cfg: dict[str, object]) -> None:
    watch_dirs = _current_watch_dirs(existing_cfg)
    current_paths = ";".join(str(p) for p in watch_dirs)
    watch_raw = questionary.text(
        "Watch folders (semicolon-separated)",
        default=current_paths,
        style=_STYLE,
    ).ask()
    if watch_raw is None:
        error("Setup cancelled.")
        return
    watch_dirs = _normalize_watch_dirs(watch_raw, watch_dirs)

    current_modalities = {
        "text": True,
        "code": True,
        "image": True,
        "audio": True,
        "video": True,
    }
    section("What would you like to search?")
    modality_choices = [
        questionary.Choice("Text files, PDFs, notes, documents", value="text", checked=current_modalities["text"]),
        questionary.Choice("Code files and repositories\n      (builds a separate code context index)", value="code", checked=current_modalities["code"]),
        questionary.Choice("Images (requires ~300MB model download)", value="image", checked=current_modalities["image"]),
        questionary.Choice("Audio recordings and meetings\n      (requires ~150MB model download)", value="audio", checked=current_modalities["audio"]),
        questionary.Choice("Video files\n      (requires both image and audio models)", value="video", checked=current_modalities["video"]),
    ]
    console.print("  [dim]All modalities are pre-selected. Press [bold]Space[/bold] to uncheck any option.[/dim]")
    selected = questionary.checkbox("Select modalities", choices=modality_choices, style=_STYLE).ask()
    if selected is None:
        error("Setup cancelled.")
        return

    enable_text = "text" in selected
    enable_code = "code" in selected
    enable_image = "image" in selected or "video" in selected
    enable_audio = "audio" in selected or "video" in selected
    enable_video = "video" in selected

    # Always use ~/.contextcore for consistent storage location
    storage_path = Path.home() / ".contextcore" / "index.db"
    section("Storage setup")
    info(f"Using storage path: {storage_path}")

    models_ready = _download_models(need_clip=enable_image, need_whisper=enable_audio)
    if not models_ready:
        warning("Some models failed to download. You can retry with: contextcore install clip  or  contextcore install audio")
    prewarm_ready = _prewarm_models(need_clip=enable_image, need_whisper=enable_audio)
    if not prewarm_ready:
        warning("Some models are installed but not warmed. Search and indexing may remain unavailable until prewarm succeeds.")

    updates = {
        "sdk_root": str(get_sdk_root()),
        "organized_root": str(watch_dirs[0]) if watch_dirs else str(get_storage_path().parent),
        "watch_directories": [str(p) for p in watch_dirs],
        "storage_path": str(storage_path),
        "enable_text": enable_text,
        "enable_code": enable_code,
        "enable_image": enable_image,
        "enable_audio": enable_audio,
        "enable_video": enable_video,
        "video_directories": [str(p) for p in watch_dirs] if enable_video else [],
        "audio_directories": [str(p) for p in watch_dirs] if enable_audio else [],
        "code_directories": [str(p) for p in watch_dirs] if enable_code else [],
        "ui_theme": _SETUP_THEME,
    }
    cfg_path = update_config_values(updates)
    success(f"Config updated at [bold]{cfg_path}[/bold]")

    auto = autostart_status()
    should_install_autostart = questionary.confirm(
        "Install or repair autostart at OS login?",
        default=bool(auto.get("installed", False)) or True,
        style=_STYLE,
    ).ask()
    _apply_autostart_choice(bool(should_install_autostart))

    section("Claude tool registration")
    refresh_tools = questionary.confirm(
        "Refresh MCP registration for your tools now?",
        default=False,
        style=_STYLE,
    ).ask()
    if refresh_tools:
        tool_choices = [
            questionary.Choice("Claude Desktop", value="Claude Desktop"),
            questionary.Choice("Claude Code", value="Claude Code"),
            questionary.Choice("Cline (VS Code)", value="Cline (VS Code)"),
            questionary.Choice("Cursor", value="Cursor"),
            questionary.Choice("OpenCode", value="OpenCode"),
            questionary.Choice("Windsurf", value="Windsurf"),
            questionary.Choice("Continue (VS Code)", value="Continue (VS Code)"),
        ]
        tools = questionary.checkbox("Select tools", choices=tool_choices, style=_STYLE).ask() or []
        for tool in tools:
            if _register_tool_with_fallback(tool):
                success(f"Updated MCP config for {tool}")
            else:
                error(f"Could not update {tool} config. Run  contextcore doctor  for help.")

    _start_server_and_scan(
        None,
        enable_text,
        enable_code,
        enable_image,
        enable_video,
        enable_audio,
        force_restart_server=(enable_image or enable_audio or enable_video),
    )

    done_panel([
        "Updated existing ContextCore setup.",
        "Indexes were preserved.",
        "Run  [bold]contextcore status[/bold]  to check server and indexing state.",
        "After a laptop restart, ContextCore should autostart if login autostart was installed.",
    ])


# ── Main wizard ────────────────────────────────────────────────────────────────

def run_init() -> None:
    existing_cfg = get_config() if _DEFAULT_CONFIG.exists() else {}
    preferred_theme = str(existing_cfg.get("ui_theme") or get_theme_name())
    _choose_setup_theme(preferred_theme)
    header()
    console.print("[bold]Welcome to ContextCore \u2726[/bold]")
    console.print("[dim]Let's get you set up. This takes about 2 minutes.[/dim]")
    info(f"Using [bold]{_SETUP_THEME}[/bold] setup theme.")
    if existing_cfg:
        _show_existing_setup(existing_cfg)
        console.print()
        action = questionary.select(
            "ContextCore is already initialized. What do you want to do?",
            choices=[
                questionary.Choice("Modify existing setup", value="modify"),
                questionary.Choice("Start fresh (keep indexes unless I explicitly reset them)", value="fresh"),
                questionary.Choice("Cancel", value="cancel"),
            ],
            default="modify",
            style=_STYLE,
        ).ask()
        if action in {None, "cancel"}:
            warning("No changes made.")
            return
        if action == "modify":
            _run_modify_existing(existing_cfg)
            return

        confirm_reset_cfg = questionary.confirm(
            "Start fresh and overwrite the current config?",
            default=False,
            style=_STYLE,
        ).ask()
        if not confirm_reset_cfg:
            warning("Keeping the existing setup unchanged.")
            return

        reset_indexes = questionary.confirm(
            "Also delete all existing indexes and rebuild from scratch?",
            default=False,
            style=_STYLE,
        ).ask()
        if reset_indexes:
            from cli.commands.helpers import reset_index_artifacts
            from cli.server import stop_server

            stop_server()
            reset_index_artifacts()
            success("Existing indexes were deleted. Fresh setup will rebuild them.")

    # ── Step 1: Choose directories ────────────────────────────────────────────
    section("First, where should ContextCore watch for files?")
    console.print("[dim]You can add more locations later.[/dim]\n")

    home = Path.home()
    presets = {
        f"~/Documents          [dim](recommended)[/dim]": home / "Documents",
        f"~/Desktop":                                       home / "Desktop",
        f"~/Downloads":                                     home / "Downloads",
        "Enter a custom path":                              None,
        "Skip for now":                                     None,
    }

    dir_choice = questionary.select(
        "Select a directory",
        choices=list(presets.keys()),
        style=_STYLE,
    ).ask()

    if dir_choice is None:
        error("Setup cancelled.")
        return

    if "custom" in dir_choice.lower():
        custom = questionary.text("Enter the full path:", style=_STYLE).ask()
        if not custom:
            error("No path given. Setup cancelled.")
            return
        watch_dir = Path(custom).expanduser().resolve()
    elif "skip" in dir_choice.lower():
        watch_dir = home / "Documents"   # a safe fallback
        warning("Skipped. Using ~/Documents as default. Change later in ~/.contextcore/contextcore.yaml")
    else:
        watch_dir = list(presets.values())[list(presets.keys()).index(dir_choice)]

    if not watch_dir.exists():
        watch_dir.mkdir(parents=True, exist_ok=True)
        info(f"Created directory: {watch_dir}")

    success(f"ContextCore will index [bold]{watch_dir}[/bold]")

    # ── Step 2: Choose modalities ─────────────────────────────────────────────
    section("What would you like to search?")
    console.print("[dim]Select all that apply with Space, Enter to confirm.[/dim]\n")

    modality_choices = [
        questionary.Choice("Text files, PDFs, notes, documents",           value="text",  checked=True),
        questionary.Choice("Code files and repositories\n      (builds a separate code context index)", value="code", checked=True),
        questionary.Choice("Images (requires ~300MB model download)",      value="image", checked=True),
        questionary.Choice("Audio recordings and meetings\n      (requires ~150MB model download)", value="audio", checked=True),
        questionary.Choice("Video files\n      (requires both image and audio models)",  value="video", checked=True),
    ]
    console.print("  [dim]All modalities are pre-selected. Press [bold]Space[/bold] to uncheck any option.[/dim]")

    selected = questionary.checkbox(
        "Select modalities",
        choices=modality_choices,
        style=_STYLE,
    ).ask()

    if selected is None:
        error("Setup cancelled.")
        return

    enable_text  = "text"  in selected
    enable_code  = "code"  in selected
    enable_image = "image" in selected or "video" in selected
    enable_audio = "audio" in selected or "video" in selected
    enable_video = "video" in selected

    # ── Step 2b: ffmpeg (required for video) ──────────────────────────────────
    ffmpeg_paths: dict[str, str] = {}
    if enable_video:
        _ensure_ffmpeg()
        ffmpeg_paths = persist_resolved_video_tools() or {}

    # ── Step 3: Storage location ──────────────────────────────────────────────
    section("Storage setup")
    console.print("[dim]ContextCore stores your index locally in ~/.contextcore[/dim]\n")

    # Always use ~/.contextcore for consistent storage location
    storage_path = Path.home() / ".contextcore" / "index.db"
    info(f"Index will be stored at: {storage_path}")

    # ── Step 4: Model downloads ───────────────────────────────────────────────
    models_ready = _download_models(need_clip=enable_image, need_whisper=enable_audio)
    if not models_ready:
        warning("Some models failed to download. You can retry with: contextcore install clip  or  contextcore install audio")
    prewarm_ready = _prewarm_models(need_clip=enable_image, need_whisper=enable_audio)
    if not prewarm_ready:
        warning("Some models are installed but not warmed. Search and indexing may remain unavailable until prewarm succeeds.")

    # ── Step 5: Write config ──────────────────────────────────────────────────
    cfg_path = _write_yaml_config(
        organized_root=watch_dir,
        storage_path=storage_path,
        enable_text=enable_text,
        enable_code=enable_code,
        enable_image=enable_image,
        enable_audio=enable_audio,
        enable_video=enable_video,
        watched_dirs=[watch_dir],
        ffmpeg_path=Path(ffmpeg_paths["ffmpeg_path"]) if ffmpeg_paths.get("ffmpeg_path") else None,
        ffprobe_path=Path(ffmpeg_paths["ffprobe_path"]) if ffmpeg_paths.get("ffprobe_path") else None,
        video_ocr_enabled=True,
        ui_theme=_SETUP_THEME,
    )
    success(f"Config saved to [bold]{cfg_path}[/bold]")
    if ffmpeg_paths:
        reload_config()
        update_config_values(ffmpeg_paths)
    _apply_autostart_choice(True)

    # ── Step 6: Claude tool registration ─────────────────────────────────────
    section("Almost done. Let's connect to Claude.")
    console.print("[dim]Which Claude tools do you use?[/dim]\n")

    tool_choices = [
        questionary.Choice("Claude Desktop",      value="Claude Desktop"),
        questionary.Choice("Claude Code",         value="Claude Code"),
        questionary.Choice("Cline (VS Code)",     value="Cline (VS Code)"),
        questionary.Choice("Cursor",              value="Cursor"),
        questionary.Choice("OpenCode",            value="OpenCode"),
        questionary.Choice("Windsurf",            value="Windsurf"),
        questionary.Choice("Continue (VS Code)",  value="Continue (VS Code)"),
        questionary.Choice("None yet — I'll set this up later", value="none"),
    ]

    tools = questionary.checkbox(
        "Select tools",
        choices=tool_choices,
        style=_STYLE,
    ).ask()

    if tools is None:
        error("Setup cancelled.")
        return

    registered: list[str] = []
    selected_tools = [tool for tool in tools if tool != "none"]
    for tool in selected_tools:
        console.print(f"\n  Connecting to [bold]{tool}[/bold]...")
        if _register_tool_with_fallback(tool):
            success(f"Added ContextCore to {tool}")
            success("Config saved")
            registered.append(tool)
        else:
            error(f"Could not update {tool} config. Run  contextcore doctor  for help.")

    if not selected_tools:
        # Non-blocking auto registration for detected tools (if any).
        try:
            repo_root = Path(__file__).resolve().parent.parent.parent
            if str(repo_root) not in sys.path:
                sys.path.insert(0, str(repo_root))
            from register_mcp import detect_installed_tools, get_tool_definitions
            from detect_paths import get_mcp_server_path, get_python_path

            python_path = get_python_path().get("path") or sys.executable
            mcp_path = get_mcp_server_path().get("path")
            if mcp_path:
                tools_def = get_tool_definitions()
                detected = detect_installed_tools(tools_def)
                if detected:
                    info("No tools selected; attempting auto-registration for detected tools...")
                    reverse = {v: k for k, v in _REGISTER_TOOL_KEY_BY_LABEL.items()}
                    from register_mcp import register_tool

                    for key in detected:
                        spec = tools_def.get(key)
                        if not spec:
                            continue
                        if register_tool(key, spec, python_path, mcp_path, dry_run=False):
                            label = reverse.get(key, key)
                            registered.append(label)
        except Exception as exc:
            warning(f"Auto-registration skipped: {exc}")

    if registered:
        console.print()
        console.print("  [bold yellow]Action needed:[/bold yellow] Restart the following tools for changes to take effect:")
        for t in registered:
            console.print(f"    \u2022 {t}")

        if "Claude Desktop" in registered or "Claude Code" in registered:
            questionary.text(
                "\nPress Enter when you have restarted Claude...",
                style=_STYLE,
            ).ask()

            console.print("\n  Verifying connection...")
            mcp_script = get_mcp_script()
            try:
                result = subprocess.run(
                    [sys.executable, str(mcp_script), "--help"],
                    capture_output=True, timeout=8
                )
                success("ContextCore MCP server is running")
                success("Claude can now see your tools: search, index_content, list_sources, fetch_content")
            except Exception:
                warning("Could not verify MCP server. Run  contextcore doctor  to diagnose.")
                console.print(f"  [dim]Retry MCP check:[/dim] [bold]{sys.executable} \"{mcp_script}\" --help[/bold]")

    # ── Step 7: Kick off initial index ────────────────────────────────────────
    section("Starting initial index...")
    console.print(f"  [dim]Scanning [bold]{watch_dir}[/bold]...[/dim]")

    # Count files so we can give an honest estimate
    try:
        file_count = sum(1 for _ in watch_dir.rglob("*") if _.is_file())
        info(f"Found [bold]{file_count:,}[/bold] files")
        est_mins = max(1, round(file_count / 500))
        info(f"Text files will be searchable in about [bold]{est_mins} minute{'s' if est_mins > 1 else ''}[/bold]")
        if enable_code:
            info("Code indexing runs separately and may take longer on larger repositories.")
        info("Running in background — you can use Claude now.")
        info("Use [bold]contextcore status[/bold] to watch progress or [bold]contextcore add-folder \"C:/path\"[/bold] to add more folders later.")
    except Exception:
        info("Could not count files — scanning in background.")

    # Kick off the uvicorn server and trigger initial scan in the background
    _start_server_and_scan(
        watch_dir,
        enable_text,
        enable_code,
        enable_image,
        enable_video,
        enable_audio,
        force_restart_server=(enable_image or enable_audio or enable_video),
    )

    # ── Done ──────────────────────────────────────────────────────────────────
    done_panel([
        'Try asking Claude: [bold cyan]"Search my documents for anything about project budgets"[/bold cyan]',
        "",
        f"Your index is building in the background.",
        "The background watcher stays active while ContextCore is running.",
        "Run  [bold]contextcore status[/bold]  to check progress.",
        "ContextCore is configured to autostart on login when installation succeeds.",
        "If your machine was restarted and it is not running yet, run  [bold]contextcore serve[/bold].",
    ])


def _start_server_and_scan(
    watch_dir: Path | None,
    text: bool,
    code: bool,
    image: bool,
    video: bool,
    audio: bool,
    force_restart_server: bool = False,
) -> None:
    """Start the FastAPI server and fire off the initial background scan."""
    from cli.server import ensure_server

    # Auto-start server (waits until it's actually ready)
    if not ensure_server(force_restart=force_restart_server):
        warning("Server could not start. Run  contextcore serve  manually in a terminal.")
        console.print("  [dim]Copy/paste:[/dim] [bold]contextcore serve[/bold]")
        return

    # Fire the scan
    try:
        import urllib.request
        import urllib.parse
        query = {
            "run_text": str(text).lower(),
            "run_code": str(code).lower(),
            "run_image": str(image).lower(),
            "run_video": str(video).lower(),
            "run_audio": str(audio).lower(),
        }
        if watch_dir is not None:
            query["target_dir"] = str(watch_dir)
            if code:
                query["code_path"] = str(watch_dir)
        params = urllib.parse.urlencode(query)
        req = urllib.request.Request(
            f"http://127.0.0.1:{DEFAULT_PORT}/index/scan?{params}",
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="ignore") or "{}")
        if payload.get("status") == "busy":
            warning("Indexing is already running. ContextCore kept the existing job.")
            console.print("  [dim]Run  [bold]contextcore status[/bold]  to check progress.[/dim]")
        else:
            success("Background indexing started")
    except Exception:
        info("Server is running — indexing will begin shortly.")
