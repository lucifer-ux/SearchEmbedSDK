from __future__ import annotations

import os
import platform
import subprocess
import sys
import json
import shutil
from pathlib import Path

from cli.constants import DEFAULT_PORT
from cli.env import build_runtime_env
from cli.lifecycle import get_port_usage, read_index_state, uninstall_autostart
from cli.paths import get_default_config, get_sdk_root
from cli.ui import console, error, header, info, section, success, warning

_SDK_ROOT = get_sdk_root()
if str(_SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(_SDK_ROOT))

from config import update_config_values, get_storage_dir
from config import get_code_directories, get_enable_code, add_watch_directory, get_watch_directories
from video_search_implementation_v2.runtime import (
    persist_resolved_video_tools,
    prewarm_clip_model,
    video_runtime_status,
)


def run_index(target: str | None = None) -> None:
    import sqlite3
    import time
    import urllib.parse
    import urllib.request

    from rich.live import Live
    from rich.table import Table

    header()
    from cli.server import ensure_server

    if not ensure_server(silent=True):
        error("Could not start server. Run  contextcore serve  manually.")
        console.print("  [dim]Copy/paste:[/dim] [bold]contextcore serve[/bold]")
        return

    target_label = target or "configured directories"
    info(f"Scanning [bold]{target_label}[/bold]...\n")

    params: dict[str, str] = {
        "run_text": "true",
        "run_image": "true",
        "run_video": "true",
        "run_audio": "true",
    }
    if get_enable_code():
        params["run_code"] = "true"
        code_dirs = get_code_directories()
        if code_dirs:
            params["code_path"] = str(code_dirs[0])
    if target:
        params["target_dir"] = target
        if params.get("run_code") == "true":
            params["code_path"] = target

    url = f"http://127.0.0.1:{DEFAULT_PORT}/index/scan?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            payload = json.loads(body) if body.strip() else {}
            if payload.get("status") == "busy":
                warning("Indexing is already running in another ContextCore job.")
                state = payload.get("state") or read_index_state()
                if state:
                    info(f"Source: {state.get('source', 'unknown')}")
                    targets = state.get("targets") or []
                    if targets:
                        info(f"Targets: {', '.join(str(t) for t in targets)}")
                    modalities = state.get("modalities") or []
                    if modalities:
                        info(f"Modalities: {', '.join(modalities)}")
                console.print("  [dim]Run  [bold]contextcore status[/bold]  to check progress.[/dim]")
                return
    except Exception as exc:
        error(f"Could not reach server: {exc}")
        console.print("  [dim]Retry the backend in a new terminal:[/dim] [bold]contextcore serve[/bold]")
        return

    storage_dir = get_storage_dir()
    text_db = storage_dir / "text_search_implementation_v2" / "storage" / "text_search_implementation_v2.db"
    image_db = storage_dir / "image_search_implementation_v2" / "storage" / "images_meta.db"
    video_db = storage_dir / "video_search_implementation_v2" / "storage" / "videos_meta.db"
    code_db = storage_dir / "storage" / "code_index_layer1.db"

    def count_rows(db: Path, query: str, fallback: int = -1) -> int:
        if not db.exists():
            return fallback
        try:
            with sqlite3.connect(str(db)) as conn:
                return int(conn.execute(query).fetchone()[0])
        except Exception:
            return fallback

    def make_table(counts: dict[str, int]) -> Table:
        runtime = video_runtime_status()
        table = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 2))
        table.add_column("Modality", style="bold", width=10)
        table.add_column("Indexed", width=10)
        table.add_column("", width=20)
        for name, value in counts.items():
            if value < 0:
                table.add_row(name, "[dim]-[/dim]", "[dim]waiting...[/dim]")
            elif value == 0 and name == "Video" and not runtime["ffmpeg_ready"]:
                table.add_row(name, "[yellow]0[/yellow]", "missing ffmpeg")
            elif value == 0 and name == "Video" and not runtime["clip_ready"]:
                table.add_row(name, "[yellow]0[/yellow]", "model unavailable")
            elif value == 0:
                table.add_row(name, "[green]0[/green]", "[dim]ready (empty)[/dim]")
            else:
                table.add_row(name, f"[green]{value:,}[/green]", "[green]ready[/green]")
        return table

    last_counts: dict[str, int] = {"Text": -1, "Code": -1, "Images": -1, "Video": -1, "Audio": -1}
    elapsed = 0.0
    interval = 2.0

    with Live(console=console, refresh_per_second=2) as live:
        while True:
            text_total = count_rows(text_db, "SELECT COUNT(*) FROM files")
            audio_total = count_rows(text_db, "SELECT COUNT(*) FROM files WHERE LOWER(category)='audio'")
            doc_total = max(0, text_total - max(0, audio_total)) if text_total >= 0 else -1
            code_total = count_rows(code_db, "SELECT COUNT(*) FROM project_files")
            image_total = count_rows(image_db, "SELECT COUNT(*) FROM images")
            video_total = count_rows(video_db, "SELECT COUNT(*) FROM videos")
            counts = {
                "Text": doc_total,
                "Code": code_total,
                "Images": image_total,
                "Video": video_total,
                "Audio": audio_total,
            }
            live.update(make_table(counts))
            if counts == last_counts and all(v >= 0 for v in counts.values()) and elapsed > 4:
                break
            last_counts = counts.copy()
            time.sleep(interval)
            elapsed += interval

    console.print()
    success("Indexing complete")
    console.print("  [dim]Run  [bold]contextcore status[/bold]  for detailed info.[/dim]")
    console.print()


def run_add_folder(path: str, index_now: bool = True) -> None:
    from cli.server import ensure_server

    header()
    folder = Path(path).expanduser().resolve()
    if not folder.exists() or not folder.is_dir():
        error(f"Directory not found: {folder}")
        console.print(f"  [dim]Create it first:[/dim] [bold]mkdir \"{folder}\"[/bold]")
        return

    cfg_path = add_watch_directory(folder)
    success(f"Added watch folder: [bold]{folder}[/bold]")
    if cfg_path:
        success(f"Updated config: [bold]{cfg_path}[/bold]")

    watch_dirs = get_watch_directories()
    console.print(f"  [dim]Now watching {len(watch_dirs)} folder{'s' if len(watch_dirs) != 1 else ''}.[/dim]")

    ensure_server(silent=True, force_restart=True)
    console.print("  [dim]Background watcher reloaded to include the new folder.[/dim]")

    if not index_now:
        console.print(f"  [dim]Index later with:[/dim] [bold]contextcore index \"{folder}\"[/bold]")
        return

    info(f"Indexing the new folder now: [bold]{folder}[/bold]")
    run_index(target=str(folder))


def reset_index_artifacts() -> None:
    sdk_root = get_sdk_root()
    paths = [
        sdk_root / "text_search_implementation_v2" / "storage" / "text_search_implementation_v2.db",
        sdk_root / "text_search_implementation_v2" / "storage" / "text_search_implementation_v2.db-shm",
        sdk_root / "text_search_implementation_v2" / "storage" / "text_search_implementation_v2.db-wal",
        sdk_root / "image_search_implementation_v2" / "storage" / "images_meta.db",
        sdk_root / "image_search_implementation_v2" / "storage" / "annoy_index.ann",
        sdk_root / "video_search_implementation_v2" / "storage" / "videos_meta.db",
        sdk_root / "video_search_implementation_v2" / "storage" / "videos_meta.db-shm",
        sdk_root / "video_search_implementation_v2" / "storage" / "videos_meta.db-wal",
        sdk_root / "video_search_implementation_v2" / "storage" / "runtime_state.json",
        sdk_root / "storage" / "code_index_layer1.db",
        sdk_root / "storage" / "code_index_layer1.db-shm",
        sdk_root / "storage" / "code_index_layer1.db-wal",
        sdk_root / "storage" / "code_index_analysis_latest.json",
    ]
    for path in paths:
        path.unlink(missing_ok=True)
    embed_dir = sdk_root / "image_search_implementation_v2" / "storage" / "embeddings"
    if embed_dir.exists():
        for child in embed_dir.iterdir():
            if child.is_file():
                child.unlink(missing_ok=True)


_INSTALL_GROUPS = {
    "clip": (
        "CLIP / Vision model",
        "torch torchvision transformers",
        "~800MB download  •  typically 5-8 minutes",
    ),
    "audio": (
        "Audio / Whisper model",
        "faster-whisper",
        "~150MB download  •  typically 1-2 minutes",
    ),
    "all": (
        "All optional models",
        "torch torchvision transformers faster-whisper",
        "~950MB download  •  typically 6-10 minutes",
    ),
}


def run_install(model: str) -> None:
    header()
    key = model.lower().strip()
    if key not in _INSTALL_GROUPS:
        error(f"Unknown model: {model}")
        info(f"Options: {', '.join(_INSTALL_GROUPS)}")
        return

    label, packages, estimate = _INSTALL_GROUPS[key]
    console.print(f"  Installing [bold]{label}[/bold]")
    console.print(f"  [dim]{estimate}[/dim]")
    console.print("  [dim]You'll see download progress below — this is normal for large packages.[/dim]\n")

    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--progress-bar", "on", "--no-cache-dir"] + packages.split()
    )
    console.print()
    if result.returncode != 0:
        error("Install failed")
        console.print("  [dim]Check your internet connection and try again.[/dim]")
        console.print(f"  [dim]Retry with:[/dim] [bold]{sys.executable} -m pip install --progress-bar on --no-cache-dir {packages}[/bold]")
        return

    ffmpeg_ready = True
    if key == "all":
        from cli.commands.init import _ensure_ffmpeg

        ffmpeg_ready = _ensure_ffmpeg()
        tool_paths = persist_resolved_video_tools()
        if tool_paths:
            update_config_values(tool_paths)

    if key in {"clip", "all"}:
        ok, err = prewarm_clip_model()
        if ok:
            success("CLIP model warmed and ready")
        else:
            warning(f"CLIP prewarm failed: {err}")

    if key in {"audio", "all"}:
        try:
            from audio_search_implementation_v2.audio_index import prewarm_whisper

            ok, err = prewarm_whisper()
        except Exception as exc:
            ok, err = False, str(exc)
        if ok:
            success("Whisper model warmed and ready")
        else:
            warning(f"Whisper prewarm failed: {err}")

    success(f"{label} installed successfully")
    if key == "all" and not ffmpeg_ready:
        warning("ffmpeg is still unavailable. Video indexing will remain disabled until ffmpeg is installed.")
        if platform.system().lower() == "windows":
            console.print("  [dim]Install it with:[/dim] [bold]winget install --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements[/bold]")

    from cli.server import ensure_server, is_server_running

    if is_server_running(DEFAULT_PORT):
        ensure_server(port=DEFAULT_PORT, silent=True, force_restart=True)


_TOOL_CONFIGS = {
    "claude-desktop": {
        "windows": Path(os.environ.get("APPDATA", "~")) / "Claude" / "claude_desktop_config.json",
        "darwin": Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
        "linux": Path.home() / ".config" / "Claude" / "claude_desktop_config.json",
    },
    "claude-code": {
        "windows": [
            Path.home() / ".claude.json",
            Path.home() / ".claude" / "config.json",
            Path(os.environ.get("APPDATA", "~")) / "Claude Code" / "config.json",
        ],
        "darwin": [
            Path.home() / ".claude.json",
            Path.home() / ".claude" / "config.json",
        ],
        "linux": [
            Path.home() / ".claude.json",
            Path.home() / ".claude" / "config.json",
        ],
    },
    "cline": {
        "windows": Path.home() / "AppData" / "Roaming" / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev" / "settings" / "cline_mcp_settings.json",
        "darwin": Path.home() / "Library" / "Application Support" / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev" / "settings" / "cline_mcp_settings.json",
        "linux": Path.home() / ".config" / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev" / "settings" / "cline_mcp_settings.json",
    },
    "cursor": {
        "windows": Path.home() / "AppData" / "Roaming" / "Cursor" / "User" / "globalStorage" / "saoudrizwan.claude-dev" / "settings" / "cline_mcp_settings.json",
        "darwin": Path.home() / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage" / "saoudrizwan.claude-dev" / "settings" / "cline_mcp_settings.json",
        "linux": Path.home() / ".config" / "Cursor" / "User" / "globalStorage" / "saoudrizwan.claude-dev" / "settings" / "cline_mcp_settings.json",
    },
    "opencode": {
        "windows": Path.home() / ".config" / "opencode" / "opencode.json",
        "darwin": Path.home() / ".config" / "opencode" / "opencode.json",
        "linux": Path.home() / ".config" / "opencode" / "opencode.json",
    },
    "windsurf": {
        "windows": Path.home() / "AppData" / "Roaming" / "Windsurf" / "User" / "globalStorage" / "saoudrizwan.claude-dev" / "settings" / "cline_mcp_settings.json",
        "darwin": Path.home() / "Library" / "Application Support" / "Windsurf" / "User" / "globalStorage" / "saoudrizwan.claude-dev" / "settings" / "cline_mcp_settings.json",
        "linux": Path.home() / ".config" / "Windsurf" / "User" / "globalStorage" / "saoudrizwan.claude-dev" / "settings" / "cline_mcp_settings.json",
    },
    "continue": {
        "windows": Path.home() / ".continue" / "config.json",
        "darwin": Path.home() / ".continue" / "config.json",
        "linux": Path.home() / ".continue" / "config.json",
    },
}


def run_register(tool: str) -> None:
    header()
    key = tool.lower().strip().replace(" ", "-")
    alias_map = {
        "claude-desktop": "claude-desktop",
        "claude-desktop-app": "claude-desktop",
        "claude-code": "claude-code",
        "cline": "cline",
        "cursor": "cursor",
        "opencode": "opencode",
        "windsurf": "windsurf",
        "continue": "continue",
        "continue-dev": "continue",
        "roo-code": "roo-code",
        "roocode": "roo-code",
    }

    repo_root = Path(__file__).resolve().parent.parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    try:
        from register_mcp import detect_installed_tools, get_tool_definitions, register_tool
        from detect_paths import get_mcp_server_path, get_python_path
    except Exception as exc:
        error(f"Could not load MCP registration helpers: {exc}")
        console.print("  [dim]Try reinstalling ContextCore and rerun this command.[/dim]")
        return

    tools = get_tool_definitions()

    if key == "list" or not key:
        detected = set(detect_installed_tools(tools))
        info("Available tools:")
        for t in sorted(tools.keys()):
            mark = "detected" if t in detected else "not found"
            console.print(f"  - [bold]{t}[/bold] ({mark})")
        return

    tool_key = alias_map.get(key)
    if not tool_key or tool_key not in tools:
        error(f"Unknown tool: {tool}")
        info(f"Available: {', '.join(sorted(tools.keys()))}")
        return

    python_path = get_python_path().get("path") or sys.executable
    mcp_info = get_mcp_server_path()
    mcp_path = mcp_info.get("path")
    if not mcp_path:
        error("mcp_server.py not found. Is ContextCore installed correctly?")
        detail = mcp_info.get("error")
        if detail:
            console.print(f"  [dim]{detail}[/dim]")
        return

    spec = tools[tool_key]
    console.print(f"  Registering ContextCore with [bold]{spec['display_name']}[/bold]...")
    ok = register_tool(tool_key, spec, python_path, mcp_path, dry_run=False)
    if ok:
        success(f"ContextCore added to {spec['display_name']}")
        console.print()
        console.print(f"  [bold yellow]Restart {spec['display_name']}[/bold yellow] for the changes to take effect.")
    else:
        error(f"Failed to update {spec['display_name']} config. Run  contextcore doctor  for help.")


def run_serve(port: int = DEFAULT_PORT, reload: bool = False) -> None:
    header()
    usage = get_port_usage(port)
    if usage.get("in_use") and not usage.get("is_contextcore"):
        pid = usage.get("pid")
        name = usage.get("process_name") or "unknown"
        error(f"Port {port} is already in use by {name}{f' (PID {pid})' if pid else ''}")
        if platform.system() == "Windows" and pid:
            console.print(f"  [dim]Inspect:[/dim] [bold]tasklist /FI \"PID eq {pid}\"[/bold]")
            console.print(f"  [dim]Stop it if appropriate:[/dim] [bold]taskkill /F /PID {pid}[/bold]")
        elif pid:
            console.print(f"  [dim]Inspect:[/dim] [bold]ps -p {pid} -o pid,comm,args[/bold]")
            console.print(f"  [dim]Stop it if appropriate:[/dim] [bold]kill {pid}[/bold]")
        console.print(f"  [dim]ContextCore stays on port {port} because MCP expects that endpoint.[/dim]")
        return
    info(f"Starting ContextCore server on http://127.0.0.1:{port}")
    info("Press Ctrl+C to stop.\n")

    env = build_runtime_env({"CONTEXTCORE_CONFIG": str(get_default_config())})
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "unimain:app",
        "--host",
        "0.0.0.0",
        "--port",
        str(port),
        "--log-level",
        "info",
    ]
    if reload:
        cmd.append("--reload")

    result = subprocess.run(cmd, cwd=str(get_sdk_root()), env=env)
    if result.returncode != 0:
        error("ContextCore server exited with an error")
        console.print("  [dim]Retry in a clean terminal:[/dim] [bold]contextcore serve[/bold]")
        console.print("  [dim]If the backend still fails, run:[/dim] [bold]contextcore doctor[/bold]")


def run_server(action: str, port: int = DEFAULT_PORT) -> None:
    header()
    mode = action.strip().lower()
    valid_modes = {"start", "stop", "restart", "status"}

    if mode not in valid_modes:
        error(f"Unknown server action: {action}")
        info("Use one of: start, stop, restart, status")
        return

    from cli.server import describe_port_conflict, ensure_server, is_server_running, stop_server

    if mode == "status":
        if is_server_running(port):
            success(f"ContextCore server is running on port {port}")
        else:
            warning(f"ContextCore server is not running on port {port}")
            conflict = describe_port_conflict(port)
            if conflict:
                info(conflict)
                usage = get_port_usage(port)
                pid = usage.get("pid")
                if platform.system() == "Windows" and pid:
                    console.print(f"  [dim]Stop conflicting process if appropriate:[/dim] [bold]taskkill /F /PID {pid}[/bold]")
                elif pid:
                    console.print(f"  [dim]Stop conflicting process if appropriate:[/dim] [bold]kill {pid}[/bold]")
        return

    if mode == "start":
        if ensure_server(port=port, silent=False):
            success(f"Server available on http://127.0.0.1:{port}")
        return

    if mode == "stop":
        if stop_server(port=port):
            success("ContextCore background server stopped")
        elif is_server_running(port):
            error("Could not stop the running server cleanly")
            console.print("  [dim]Try again or run:[/dim] [bold]contextcore doctor[/bold]")
        else:
            info("Server is already stopped")
        return

    # restart
    if is_server_running(port):
        info("Stopping ContextCore server...")
        if stop_server(port=port):
            success("Server stopped")
        else:
            warning("Could not confirm stop. Attempting start anyway.")

    if ensure_server(port=port, silent=False):
        success(f"ContextCore server restarted on http://127.0.0.1:{port}")


def run_uninstall(
    yes: bool = False,
    dry_run: bool = False,
    remove_package: bool = False,
    purge_model_cache: bool = False,
) -> None:
    header()
    section("Uninstall ContextCore", "This removes local ContextCore state and integrations from this machine.")

    if not yes and not dry_run:
        warning("This will stop ContextCore, remove indexes/config, and unregister MCP entries.")
        confirm = console.input("  Type [bold]DELETE[/bold] to continue: ").strip()
        if confirm != "DELETE":
            warning("Aborted. Nothing was removed.")
            return

    contextcore_home = Path.home() / ".contextcore"
    sdk_root = get_sdk_root()
    removed: list[str] = []
    skipped: list[str] = []

    def _remove_path(path: Path) -> None:
        resolved = path.expanduser().resolve()
        anchor = Path(resolved.anchor)
        if resolved == anchor:
            skipped.append(f"{resolved} (unsafe target)")
            return
        if not resolved.exists():
            skipped.append(f"{resolved} (not found)")
            return
        if dry_run:
            info(f"[dry-run] Would remove {resolved}")
            return
        try:
            if resolved.is_dir():
                shutil.rmtree(resolved, ignore_errors=False)
            else:
                resolved.unlink(missing_ok=True)
            removed.append(str(resolved))
        except Exception as exc:
            warning(f"Could not remove {resolved}: {exc}")

    section("Stop Server")
    from cli.server import is_server_running, stop_server

    if dry_run:
        info("[dry-run] Would stop background ContextCore server")
    else:
        if stop_server(port=DEFAULT_PORT):
            success("Stopped background server")
        elif is_server_running(DEFAULT_PORT):
            usage = get_port_usage(DEFAULT_PORT)
            pid = usage.get("pid")
            warning("Could not stop server cleanly")
            if pid and usage.get("is_contextcore"):
                from cli.lifecycle import stop_pid

                if stop_pid(int(pid)):
                    success(f"Stopped ContextCore PID {pid}")
                else:
                    warning(f"Failed to stop PID {pid}")
        else:
            info("Server already stopped")

    section("Unregister MCP")
    try:
        from register_mcp import get_tool_definitions, unregister_tool

        tools = get_tool_definitions()
        removed_tools = 0
        for name, spec in tools.items():
            if unregister_tool(name, spec, dry_run=dry_run):
                removed_tools += 1
        if removed_tools > 0:
            if dry_run:
                success(f"Would remove ContextCore MCP entries from {removed_tools} tool config(s)")
            else:
                success(f"Removed ContextCore MCP entries from {removed_tools} tool config(s)")
        else:
            info("No ContextCore MCP registrations found")
    except Exception as exc:
        warning(f"Could not run MCP unregister step: {exc}")

    section("Remove Autostart")
    if dry_run:
        info("[dry-run] Would remove autostart entry")
    else:
        ok, msg = uninstall_autostart()
        if ok:
            success(msg)
        else:
            warning(f"Autostart removal failed: {msg}")

    section("Delete Local Data")
    # Clean known index artifacts first, then remove full local state directories.
    if dry_run:
        info("[dry-run] Would remove index databases and cache directories")
    else:
        try:
            reset_index_artifacts()
        except Exception as exc:
            warning(f"Index artifact cleanup failed: {exc}")

    data_targets = [
        contextcore_home,
        sdk_root / "storage",
        sdk_root / "text_search_implementation_v2" / "storage",
        sdk_root / "image_search_implementation_v2" / "storage",
        sdk_root / "video_search_implementation_v2" / "storage",
        sdk_root / ".thumbnails",
        sdk_root / "thumbnails",
    ]
    for target in data_targets:
        _remove_path(target)

    if purge_model_cache:
        section("Purge Model Cache")
        model_cache_targets = [
            Path.home() / ".cache" / "huggingface" / "hub",
            Path.home() / ".cache" / "torch" / "hub",
        ]
        for target in model_cache_targets:
            _remove_path(target)

    if remove_package:
        section("Uninstall Pip Package")
        cmd = [sys.executable, "-m", "pip", "uninstall", "-y", "contextcore"]
        if dry_run:
            info(f"[dry-run] Would run: {' '.join(cmd)}")
        else:
            result = subprocess.run(cmd)
            if result.returncode == 0:
                success("Uninstalled pip package: contextcore")
            else:
                warning("Could not uninstall pip package automatically")
                console.print(f"  [dim]Run manually:[/dim] [bold]{' '.join(cmd)}[/bold]")

    console.print()
    if dry_run:
        success("Dry run complete")
    else:
        success("ContextCore local cleanup complete")
    if removed:
        info(f"Removed {len(removed)} path(s)")
    if skipped and dry_run:
        info(f"Skipped {len(skipped)} path(s)")
    if not remove_package:
        console.print(f"  [dim]Optional: remove package too with:[/dim] [bold]{sys.executable} -m pip uninstall -y contextcore[/bold]")
