from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from rich.table import Table

from cli.constants import DEFAULT_PORT
from cli.lifecycle import autostart_status, get_port_usage, index_lock_active, read_index_state
from cli.paths import get_sdk_root
from cli.ui import console, error, header, section, success, warning

_SDK_ROOT = get_sdk_root()
if str(_SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(_SDK_ROOT))

from video_search_implementation_v2.runtime import video_runtime_status
from config import get_enable_code, get_watch_directories, get_storage_dir


def _count(db: Path, query: str) -> int:
    if not db.exists():
        return -1
    try:
        with sqlite3.connect(str(db)) as conn:
            return int(conn.execute(query).fetchone()[0])
    except Exception:
        return -1


def _count_total_bytes(db: Path, table: str, col: str, where_clause: str = "") -> int:
    if not db.exists():
        return -1
    try:
        with sqlite3.connect(str(db)) as conn:
            query = f"SELECT SUM(LENGTH({col})) FROM {table}"
            if where_clause:
                query += f" {where_clause}"
            result = conn.execute(query).fetchone()[0]
            return int(result) if result else 0
    except Exception:
        return -1


def _count_code_tokens(db: Path) -> tuple[int, int]:
    if not db.exists():
        return -1, -1
    try:
        with sqlite3.connect(str(db)) as conn:
            line_count = conn.execute("SELECT SUM(line_count) FROM project_files").fetchone()[0] or 0
            symbol_count = conn.execute("SELECT COUNT(*) FROM code_symbols").fetchone()[0] or 0
            return int(line_count), int(symbol_count)
    except Exception:
        return -1, -1


def _estimate_naive_tokens(text_bytes: int) -> int:
    return max(1, text_bytes // 4)


def _estimate_optimized_tokens(text_bytes: int) -> int:
    return max(1, text_bytes // 8)


def _print_port_conflict(port: int) -> None:
    usage = get_port_usage(port)
    pid = usage.get("pid")
    name = usage.get("process_name") or "unknown"
    error(f"Port {port} is already in use by {name}{f' (PID {pid})' if pid else ''}")
    if pid:
        if sys.platform.startswith("win"):
            console.print(f"  [dim]Inspect:[/dim] [bold]tasklist /FI \"PID eq {pid}\"[/bold]")
            console.print(f"  [dim]Stop if appropriate:[/dim] [bold]taskkill /F /PID {pid}[/bold]")
        else:
            console.print(f"  [dim]Inspect:[/dim] [bold]ps -p {pid} -o pid,comm,args[/bold]")
            console.print(f"  [dim]Stop if appropriate:[/dim] [bold]kill {pid}[/bold]")
    console.print(f"  [dim]ContextCore remains pinned to port {port} because MCP expects that endpoint.[/dim]")


def run_status(port: int = DEFAULT_PORT) -> None:
    header()
    sdk_root = get_sdk_root()
    runtime = video_runtime_status()

    from cli.server import ensure_server, is_server_running

    ensure_server(port=port, silent=True)
    server_ok = is_server_running(port)

    section("Server", "Runtime process status and MCP entrypoint health.")
    if server_ok:
        success(f"Running on port [bold]{port}[/bold]")
        success("Background watcher active while the server is running")
    else:
        usage = get_port_usage(port)
        if usage.get("in_use") and not usage.get("is_contextcore"):
            _print_port_conflict(port)
        else:
            error(f"Not running on port {port}")
            console.print(f"  [dim]Start with:[/dim] [bold]contextcore serve[/bold]")
            console.print("  [dim]If it still fails, run:[/dim] [bold]contextcore doctor[/bold]")

    mcp_script = sdk_root / "mcp_server.py"
    if mcp_script.exists():
        success("MCP server script found")
    else:
        error("MCP server script not found")

    section("Index Activity", "Current indexing lock state and last full-index outcome.")
    active_lock, state = index_lock_active()
    if active_lock:
        success("Indexing is currently running")
        source = state.get("source") or "unknown"
        success(f"Source: [bold]{source}[/bold]")
        targets = state.get("targets") or []
        if targets:
            success(f"Targets: [bold]{', '.join(str(t) for t in targets)}[/bold]")
        modalities = state.get("modalities") or []
        if modalities:
            success(f"Modalities: [bold]{', '.join(modalities)}[/bold]")
        progress = state.get("progress")
        if isinstance(progress, dict) and progress.get("current_modality"):
            success(f"In progress: [bold]{progress.get('current_modality')}[/bold]")
        if state.get("started_at"):
            success(f"Started: [bold]{state['started_at']}[/bold]")
    else:
        state = read_index_state()
        if state.get("started_at"):
            success("No active full index job")
            if state.get("result"):
                success(f"Last full index result: [bold]{state.get('result')}[/bold]")
            if state.get("completed_at"):
                success(f"Last full index completed: [bold]{state.get('completed_at')}[/bold]")
            if state.get("stale_lock_recovered_at"):
                warning(f"Recovered stale index lock at {state.get('stale_lock_recovered_at')}")
        else:
            warning("No full index job has been recorded yet")

    section("Index Progress", "Indexed item counts by modality.")

    storage_dir = get_storage_dir()
    text_db = storage_dir / "text_search_implementation_v2" / "storage" / "text_search_implementation_v2.db"
    image_db = storage_dir / "image_search_implementation_v2" / "storage" / "images_meta.db"
    video_db = storage_dir / "video_search_implementation_v2" / "storage" / "videos_meta.db"
    code_db = storage_dir / "storage" / "code_index_layer1.db"

    text_total = _count(text_db, "SELECT COUNT(*) FROM files WHERE LOWER(category) NOT IN ('audio', 'video_transcript')")
    audio_total = _count(text_db, "SELECT COUNT(*) FROM files WHERE LOWER(category)='audio'")
    doc_total = text_total
    code_total = _count(code_db, "SELECT COUNT(*) FROM project_files")
    image_total = _count(image_db, "SELECT COUNT(*) FROM images")
    video_total = _count(video_db, "SELECT COUNT(*) FROM videos")

    table = Table(show_header=True, header_style="title", box=None, padding=(0, 2))
    table.add_column("Modality", style="bold", width=12)
    table.add_column("Indexed", width=10)
    table.add_column("Status", width=24)

    def add_row(name: str, count: int) -> None:
        if count < 0:
            table.add_row(name, "[dim]-[/dim]", "[dim]no database[/dim]")
            return
        if count > 0:
            table.add_row(name, f"[green]{count:,}[/green]", "[green]ready[/green]")
            return
        if name == "Video" and not runtime["ffmpeg_ready"]:
            table.add_row(name, "[yellow]0[/yellow]", "[yellow]missing ffmpeg[/yellow]")
            return
        if name == "Video" and not runtime["clip_ready"]:
            table.add_row(name, "[yellow]0[/yellow]", "[yellow]model unavailable[/yellow]")
            return
        table.add_row(name, "[green]0[/green]", "[dim]ready (empty)[/dim]")

    add_row("Text", doc_total)
    if get_enable_code() or code_total >= 0:
        add_row("Code", code_total)
    add_row("Images", image_total)
    add_row("Audio", audio_total)
    add_row("Video", video_total)

    console.print()
    console.print(table)

    section("Watch Folders", "Configured directories and filesystem availability.")
    watch_dirs = get_watch_directories()
    if watch_dirs:
        watch_table = Table(show_header=True, header_style="title", box=None, padding=(0, 2))
        watch_table.add_column("#", width=4, style="dim")
        watch_table.add_column("Path", style="bold")
        watch_table.add_column("Status", width=20)

        for idx, path in enumerate(watch_dirs, 1):
            exists = path.exists()
            status = "[green]exists[/green]" if exists else "[red]missing[/red]"
            watch_table.add_row(str(idx), str(path), status)

        console.print()
        console.print(watch_table)
    else:
        warning("No watch folders configured. Run 'contextcore init' to set up.")

    section("Config", "Active configuration, startup hooks, and runtime media tooling.")
    cfg = Path.home() / ".contextcore" / "contextcore.yaml"
    if cfg.exists():
        success(f"Config: [bold]{cfg}[/bold]")
    else:
        warning("No config found. Run  contextcore init  to set up.")

    auto = autostart_status()
    if auto.get("installed"):
        success(f"Autostart: [bold]{auto.get('target', 'installed')}[/bold]")
    else:
        warning("Autostart not installed")

    if runtime["ffmpeg_path"]:
        success(f"ffmpeg: [bold]{runtime['ffmpeg_path']}[/bold]")
    else:
        warning("ffmpeg is not resolved in the current runtime")

    if not runtime["clip_ready"]:
        warning("CLIP model is not warmed yet for image/video search")

    console.print()
    console.print("[dim]Run  [bold]contextcore index[/bold]  to scan for new files now.[/dim]")
    console.print()
