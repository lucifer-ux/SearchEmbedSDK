from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from config import get_enable_code, get_storage_dir, get_watch_directories
from video_search_implementation_v2.runtime import video_runtime_status
from activity.search_analytics import top_searched_files

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, ScrollableContainer, VerticalScroll
from textual.widgets import (
    Label,
    Static,
    DataTable,
    Rule,
    Footer,
    Header,
    Sparkline,
)
from textual.widget import Widget

from cli.constants import DEFAULT_PORT
from cli.lifecycle import (
    autostart_status,
    get_port_usage,
    index_lock_active,
    read_index_state,
)
from cli.paths import get_sdk_root
from cli.ui import get_theme_name, get_setup_theme

_SDK_ROOT = get_sdk_root()
if str(_SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(_SDK_ROOT))

from video_search_implementation_v2.runtime import video_runtime_status  # noqa: E402
from config import get_enable_code, get_watch_directories, get_storage_dir  # noqa: E402

@dataclass(frozen=True)
class ModalitySnapshot:
    name: str
    count: int
    status_label: str
    level: str
    ratio: float


@dataclass(frozen=True)
class WatchFolderSnapshot:
    path: str
    exists: bool


@dataclass(frozen=True)
class TrendPoint:
    label: str
    naive_tokens: int
    saved_tokens: int
    indexed_files: int


@dataclass(frozen=True)
class TopSearchedFile:
    path: str
    hit_count: int


@dataclass(frozen=True)
class StatusSnapshot:
    port: int
    server_ok: bool
    server_label: str
    server_detail: str
    mcp_ok: bool
    mcp_label: str
    index_label: str
    index_detail: str
    autostart_ok: bool
    autostart_label: str
    modalities: list[ModalitySnapshot]
    token_naive: int
    token_optimized: int
    token_reduction_pct: float
    token_health: str
    ffmpeg_ok: bool
    ffmpeg_label: str
    clip_ok: bool
    clip_label: str
    config_ok: bool
    config_label: str
    watch_folders: list[WatchFolderSnapshot]
    trend_points: list[TrendPoint]
    indexed_today: int
    indexed_yesterday: int
    indexed_delta: int
    token_vs_graph: str
    indexed_trend_graph: str
    token_sparkline_data: list[int]
    indexed_sparkline_data: list[int]
    total_tokens_saved: int
    total_files_indexed: int
    top_searched_files: list[TopSearchedFile]
    next_action: str


# ─────────────────────────── DB helpers ─────────────────────────────

# ── DB helpers ────────────────────────────────────────────────────────────────

def _count(db: Path, query: str) -> int:
    if not db.exists():
        return -1
    try:
        with sqlite3.connect(str(db)) as conn:
            return int(conn.execute(query).fetchone()[0])
    except Exception:
        return -1


def _count_code_tokens(db: Path) -> tuple[int, int]:
    if not db.exists():
        return -1, -1
    try:
        with sqlite3.connect(str(db)) as conn:
            line_count = (
                conn.execute("SELECT SUM(line_count) FROM project_files")
                .fetchone()[0] or 0
            )
            symbol_count = (
                conn.execute("SELECT COUNT(*) FROM code_symbols")
                .fetchone()[0] or 0
            )
            return int(line_count), int(symbol_count)
    except Exception:
        return -1, -1


def _count_total_bytes(db: Path, table: str, column: str, where: str = "") -> int:
    if not db.exists():
        return -1
    try:
        with sqlite3.connect(str(db)) as conn:
            query = f"SELECT SUM(LENGTH({column})) FROM {table}"
            if where:
                query += f" {where}"
            result = conn.execute(query).fetchone()[0]
            return int(result) if result else 0
    except Exception:
        return -1


# ── CSS ───────────────────────────────────────────────────────────────────────

LIGHT_CSS = """
Screen {
    background: #f8f8fa;
    color: #1a1a2e;
}

.dashboard-title {
    text-style: bold;
    color: #6b21e8;
    margin: 1 0 0 2;
}

.system-status-online {
    color: #00c896;
    text-align: right;
    margin: 1 2 0 0;
}

.system-status-offline {
    color: #e84040;
    text-align: right;
    margin: 1 2 0 0;
}

.section-label {
    color: #6b21e8;
    text-style: bold;
    margin: 1 0 0 2;
}

.metric-label {
    color: #6b21e8;
    text-style: bold;
    margin: 0 0 0 2;
}

.metric-value {
    color: #00c896;
    text-style: bold;
    margin: 0 0 0 2;
}

.metric-value-warn {
    color: #e8a020;
    text-style: bold;
    margin: 0 0 0 2;
}

.metric-value-error {
    color: #e84040;
    text-style: bold;
    margin: 0 0 0 2;
}

.metric-dim {
    color: #9090a0;
    margin: 0 0 0 2;
}

.divider {
    color: #d0d0e0;
    margin: 1 2;
}

.card {
    border: none;
    padding: 1 2;
    margin: 0 1;
    background: #f0f0f8;
}

.ops-timestamp {
    color: #b0b0c0;
    margin: 0 0 0 2;
}

.footer-hint {
    color: #9090a0;
    margin: 1 2;
}

.progress-bar-outer {
    background: #e0e0ee;
    width: 1fr;
    height: 1;
    margin: 0 2;
}

#top-bar {
    height: 3;
}

#main-grid {
    layout: grid;
    grid-size: 2;
    grid-gutter: 1;
    margin: 0 1;
}

#index-table {
    margin: 0 2 1 2;
}

#watch-table {
    margin: 0 2 1 2;
}
"""

DARK_CSS = """
Screen {
    background: #0e0e16;
    color: #e8e8f0;
}

.dashboard-title {
    text-style: bold;
    color: #a855f7;
    margin: 1 0 0 2;
}

.system-status-online {
    color: #00ffb3;
    text-align: right;
    margin: 1 2 0 0;
}

.system-status-offline {
    color: #ff4444;
    text-align: right;
    margin: 1 2 0 0;
}

.section-label {
    color: #a855f7;
    text-style: bold;
    margin: 1 0 0 2;
}

.metric-label {
    color: #a855f7;
    text-style: bold;
    margin: 0 0 0 2;
}

.metric-value {
    color: #00ffb3;
    text-style: bold;
    margin: 0 0 0 2;
}

.metric-value-warn {
    color: #ffb020;
    text-style: bold;
    margin: 0 0 0 2;
}

.metric-value-error {
    color: #ff4444;
    text-style: bold;
    margin: 0 0 0 2;
}

.metric-dim {
    color: #5a5a7a;
    margin: 0 0 0 2;
}

.divider {
    color: #2a2a3e;
    margin: 1 2;
}

.card {
    border: none;
    padding: 1 2;
    margin: 0 1;
    background: #16162a;
}

.ops-timestamp {
    color: #4a4a6a;
    margin: 0 0 0 2;
}

.footer-hint {
    color: #5a5a7a;
    margin: 1 2;
}

.progress-bar-outer {
    background: #1e1e32;
    width: 1fr;
    height: 1;
    margin: 0 2;
}

#top-bar {
    height: 3;
}

#main-grid {
    layout: grid;
    grid-size: 2;
    grid-gutter: 1;
    margin: 0 1;
}

#index-table {
    margin: 0 2 1 2;
}

#watch-table {
    margin: 0 2 1 2;
}
"""


# ── Widgets ───────────────────────────────────────────────────────────────────

class MetricCard(Widget):
    """A labelled metric with a value."""

    DEFAULT_CSS = ""

    def __init__(
        self,
        label: str,
        value: str,
        value_style: str = "metric-value",
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._label = label
        self._value = value
        self._value_style = value_style

    def compose(self) -> ComposeResult:
        yield Label(self._label, classes="metric-label")
        yield Label(self._value, classes=self._value_style)


class StatusBadge(Static):
    """Inline ■ SYSTEM ONLINE / OFFLINE badge."""
def _estimate_naive_tokens(text_bytes: int) -> int:
    """~4 bytes per token is standard GPT-style BPE approximation."""
    return max(0, text_bytes // 4)


def _estimate_optimized_tokens(text_bytes: int) -> int:
    """ContextCore typically achieves ~50% reduction via chunk dedup + summarisation."""
    return max(0, text_bytes // 8)


def _ascii_safe() -> bool:
    encoding = (getattr(sys.stdout, "encoding", None) or "").lower()
    return "utf" in encoding or encoding in {"cp65001"}


def _mini_bar(
    value: int, max_value: int, width: int = 12, unicode_ok: bool = True
) -> str:
    if width < 1:
        width = 1
    if max_value <= 0:
        max_value = 1
    ratio = max(0.0, min(1.0, float(value) / float(max_value)))
    filled = int(round(ratio * width))
    empty = max(0, width - filled)
    if unicode_ok:
        full = "\u2588"
        empty_char = "\u2591"
        return f"{full * filled}{empty_char * empty}"
    return f"{'#' * filled}{'-' * empty}"


def _token_health_label(reduction_pct: float) -> str:
    if reduction_pct >= 45.0:
        return "good"
    if reduction_pct >= 20.0:
        return "warn"
    return "critical"


def _level_from_count(
    name: str, count: int, runtime: dict[str, Any]
) -> tuple[str, str]:
    if count < 0:
        return "warn", "no database"
    if count > 0:
        return "ok", "ready"
    if name == "Video" and not runtime.get("ffmpeg_ready", False):
        return "warn", "missing ffmpeg"
    if name == "Video" and not runtime.get("clip_ready", False):
        return "warn", "model unavailable"
    return "ok", "ready (empty)"


def _truncate_path(value: str, width: int = 72) -> str:
    if len(value) <= width:
        return value
    if width <= 3:
        return value[:width]
    return f"…{value[-(width - 1):]}"


def _config_path() -> Path:
    return Path.home() / ".contextcore" / "contextcore.yaml"


def _local_day_labels(days: int = 7) -> list[str]:
    today = datetime.now().date()
    return [(today - timedelta(days=i)).isoformat() for i in reversed(range(days))]


def _query_day_counts(db: Path, sql: str, days: int = 7) -> dict[str, int]:
    labels = _local_day_labels(days)
    out = {label: 0 for label in labels}
    if not db.exists():
        return out
    try:
        with sqlite3.connect(str(db)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql).fetchall()
        for row in rows:
            day = str(row["day"])
            if day in out:
                out[day] = int(row["value"] or 0)
    except Exception:
        return out
    return out


def _sparkline(values: list[int], unicode_ok: bool = True) -> str:
    if not values:
        return ""
    if all(v == values[0] for v in values):
        if values[0] == 0:
            return "·" * len(values)
        if unicode_ok:
            return "█" * len(values)
        else:
            return "#" * len(values)
    if unicode_ok:
        ticks = "▁▂▃▄▅▆▇█"
    else:
        ticks = "._-*#@"
    lo = min(values)
    hi = max(values)
    if hi == lo:
        return "█" * len(values) if values[0] > 0 else "·" * len(values)
    span = hi - lo
    chars: list[str] = []
    top = len(ticks) - 1
    for val in values:
        idx = int(round(((val - lo) / span) * top))
        idx = max(0, min(top, idx))
        chars.append(ticks[idx])
    return "".join(chars)

    def __init__(self, online: bool, **kwargs):
        symbol = "■ SYSTEM ONLINE" if online else "■ SYSTEM OFFLINE"
        cls = "system-status-online" if online else "system-status-offline"
        super().__init__(symbol, classes=cls, **kwargs)


# ── Main App ──────────────────────────────────────────────────────────────────

class StatusDashboard(App):
    """ContextCore status dashboard with Textual UI."""

    CSS = DARK_CSS  # Default, will be replaced in compose based on theme

    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
        Binding("escape", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit"),
    ]

    def __init__(self, port: int = DEFAULT_PORT, **kwargs):
        self._port = port
        super().__init__(**kwargs)

    # ── data gathering ────────────────────────────────────────────────────────

    def _gather_data(self) -> dict:
        from cli.server import ensure_server, is_server_running
        ensure_server(port=self._port, silent=True)
        server_ok = is_server_running(self._port)
        runtime = video_runtime_status()

        storage_dir = get_storage_dir()
        text_db = (
            storage_dir
            / "text_search_implementation_v2"
            / "storage"
            / "text_search_implementation_v2.db"
        )
        image_db = (
            storage_dir
            / "image_search_implementation_v2"
            / "storage"
            / "images_meta.db"
        )
        video_db = (
            storage_dir
            / "video_search_implementation_v2"
            / "storage"
            / "videos_meta.db"
        )
        code_db = storage_dir / "storage" / "code_index_layer1.db"

        text_total = _count(
            text_db,
            "SELECT COUNT(*) FROM files"
            " WHERE LOWER(category) NOT IN ('audio', 'video_transcript')",
        )
        audio_total = _count(
            text_db,
            "SELECT COUNT(*) FROM files WHERE LOWER(category)='audio'",
        )
        code_total = _count(code_db, "SELECT COUNT(*) FROM project_files")
        image_total = _count(image_db, "SELECT COUNT(*) FROM images")
        video_total = _count(video_db, "SELECT COUNT(*) FROM videos")

        totals = [text_total, audio_total, code_total, image_total, video_total]
        total_indexed = sum(t for t in totals if t > 0)

        active_lock, lock_state = index_lock_active()
        if not active_lock:
            lock_state = read_index_state()

        last_index_date = (
            lock_state.get("completed_at") or lock_state.get("started_at")
        )

        watch_dirs = get_watch_directories()
        cfg = Path.home() / ".contextcore" / "contextcore.yaml"
        auto = autostart_status()

        cloud_connected = False
        try:
            from config import get_config
            cloud_cfg = get_config()
            cloud_connected = bool(
                cloud_cfg.get("storage", {}).get("cloud")
            )
        except Exception:
            pass

        return dict(
            server_ok=server_ok,
            runtime=runtime,
            text_total=text_total,
            audio_total=audio_total,
            code_total=code_total,
            image_total=image_total,
            video_total=video_total,
            total_indexed=total_indexed,
            last_index_date=last_index_date,
            active_lock=active_lock,
            lock_state=lock_state,
            watch_dirs=watch_dirs,
            cfg=cfg,
            auto=auto,
            cloud_connected=cloud_connected,
            enable_code=get_enable_code(),
            port=self._port,
        )

    # ── compose ───────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        data = self._gather_data()

        # ── Top bar ───────────────────────────────────────────────────────────
        with Horizontal(id="top-bar"):
            yield Label("STATUS DASHBOARD", classes="dashboard-title")
            yield StatusBadge(data["server_ok"])

        yield Rule(classes="divider")

        with ScrollableContainer():

            # ── System Info ───────────────────────────────────────────────────
            with Vertical(classes="card"):
                yield Label("SYSTEM INFO", classes="section-label")
                server = data["server_ok"]
                port_status = "Running" if server else "Stopped"
                yield Label(
                    f"MCP Port: {data['port']} | {port_status}",
                    classes="metric-value" if server else "metric-value-error",
                )

                mcp_script = get_sdk_root() / "mcp_server.py"
                yield Label(
                    "MCP: Found" if mcp_script.exists() else "MCP: Missing",
                    classes=(
                        "metric-value" if mcp_script.exists()
                        else "metric-value-error"
                    ),
                )

                runtime = data["runtime"]
                yield Label(
                    "FFmpeg: Ready" if runtime["ffmpeg_path"]
                    else "FFmpeg: Not Found",
                    classes=(
                        "metric-value" if runtime["ffmpeg_path"]
                        else "metric-value-warn"
                    ),
                )

                cloud_conn = data["cloud_connected"]
                yield Label(
                    f"Cloud: {'Connected' if cloud_conn else 'Not Connected'}",
                    classes="metric-value" if cloud_conn else "metric-dim",
                )

                auto = data["auto"]
                autostart_status_str = (
                    "Enabled" if auto.get("installed") else "Disabled"
                )
                yield Label(
                    f"Autostart: {autostart_status_str}",
                    classes=(
                        "metric-value" if auto.get("installed")
                        else "metric-dim"
                    ),
                )

            # ── Index Stats ───────────────────────────────────────────────────
            with Vertical(classes="card"):
                yield Label("INDEX STATS", classes="section-label")
                total_idx = data["total_indexed"]
                yield Label(f"Total Files: {total_idx:,}", classes="metric-value")

                last_date = data["last_index_date"]
                if last_date:
                    yield Label(
                        f"Last Index: {last_date[:19]}",
                        classes="metric-dim",
                    )
                else:
                    yield Label("No Index Recorded", classes="metric-dim")

                active = data["active_lock"]
                if active:
                    yield Label("Status: Indexing...", classes="metric-value-warn")
                else:
                    yield Label("Status: Idle", classes="metric-dim")

            # ── Index Progress table ──────────────────────────────────────────
            yield Label("INDEX PROGRESS", classes="section-label")

            table: DataTable = DataTable(id="index-table", show_cursor=False)
            table.add_columns("Modality", "Indexed", "Status")

            def _row(name: str, count: int, runtime_data: dict) -> None:
                if count < 0:
                    table.add_row(name, "-", "no database")
                elif count > 0:
                    table.add_row(name, f"{count:,}", "ready")
                elif name == "Video" and not runtime_data["ffmpeg_ready"]:
                    table.add_row(name, "0", "missing ffmpeg")
                elif name == "Video" and not runtime_data["clip_ready"]:
                    table.add_row(name, "0", "model unavailable")
                else:
                    table.add_row(name, "0", "ready (empty)")

            rt = data["runtime"]
            _row("Text", data["text_total"], rt)
            if data["enable_code"] or data["code_total"] >= 0:
                _row("Code", data["code_total"], rt)
            _row("Images", data["image_total"], rt)
            _row("Audio", data["audio_total"], rt)
            _row("Video", data["video_total"], rt)

            yield table

            # ── Watch folders table ───────────────────────────────────────────
            yield Label("WATCH FOLDERS", classes="section-label")

            watch_dirs = data["watch_dirs"]
            if watch_dirs:
                wtable: DataTable = DataTable(
                    id="watch-table", show_cursor=False
                )
                wtable.add_columns("#", "Path", "Status")
                for idx, path in enumerate(watch_dirs, 1):
                    status = "exists" if path.exists() else "missing"
                    wtable.add_row(str(idx), str(path), status)
                yield wtable
            else:
                yield Label(
                    "No watch folders configured."
                    " Run 'contextcore init' to set up.",
                    classes="metric-value-warn",
                )

            yield Rule(classes="divider")

            yield Label(
                "Press [R] to refresh   Press [Q] to quit"
                "   Run [contextcore index] to scan now.",
                classes="footer-hint",
            )

    # ── actions ───────────────────────────────────────────────────────────────

    def action_refresh(self) -> None:
        port = self._port
        self.set_timer(0.1, self.exit)
        self.set_timer(0.2, lambda: run_status(port))

    async def action_quit(self) -> None:
        self.exit()


# ── Public entry point ────────────────────────────────────────────────────────

def _fmt_tokens(n: int) -> str:
    """Format token count with K/M suffix."""
    if n < 0:
        return "N/A"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _fmt_files(n: int) -> str:
    if n < 0:
        return "N/A"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


# ─────────────────────────── Snapshot collector ─────────────────────

def _collect_status_snapshot(port: int = DEFAULT_PORT) -> StatusSnapshot:
    sdk_root = get_sdk_root()
    runtime = video_runtime_status()

    from cli.server import ensure_server, is_server_running

    ensure_server(port=port, silent=True)
    server_ok = is_server_running(port)

    if server_ok:
        server_label = f"Running on :{port}"
        server_detail = "Background watcher active"
    else:
        usage = get_port_usage(port)
        if usage.get("in_use") and not usage.get("is_contextcore"):
            pid = usage.get("pid")
            name = usage.get("process_name") or "unknown"
            server_label = f"Port {port} → {name}{f' ({pid})' if pid else ''}"
            server_detail = "Port taken by another process"
        else:
            server_label = f"Not running on :{port}"
            server_detail = "Run: contextcore serve"

    mcp_ok = (sdk_root / "mcp_server.py").exists()
    mcp_label = "Script found" if mcp_ok else "Script missing"

    active_lock, state = index_lock_active()
    if active_lock:
        source = state.get("source") or "unknown"
        progress = (
            state.get("progress") if isinstance(state.get("progress"), dict) else {}
        )
        current = (
            progress.get("current_modality") if isinstance(progress, dict) else None
        )
        index_label = "Indexing…"
        index_detail = f"src={source}" + (f", {current}" if current else "")
    else:
        latest = read_index_state()
        if latest.get("started_at"):
            result = latest.get("result") or "unknown"
            completed = latest.get("completed_at") or "pending"
            index_label = "Idle"
            index_detail = f"{result} @ {completed}"
        else:
            index_label = "Never indexed"
            index_detail = "Run: contextcore index"

    storage_dir = get_storage_dir()
    text_db = (
        storage_dir
        / "text_search_implementation_v2"
        / "storage"
        / "text_search_implementation_v2.db"
    )
    image_db = (
        storage_dir / "image_search_implementation_v2" / "storage" / "images_meta.db"
    )
    video_db = (
        storage_dir / "video_search_implementation_v2" / "storage" / "videos_meta.db"
    )
    code_db = storage_dir / "storage" / "code_index_layer1.db"

    text_total = _count(
        text_db,
        "SELECT COUNT(*) FROM files WHERE LOWER(category) NOT IN ('audio', 'video_transcript')",
    )
    audio_total = _count(
        text_db, "SELECT COUNT(*) FROM files WHERE LOWER(category)='audio'"
    )
    code_total = _count(code_db, "SELECT COUNT(*) FROM project_files")
    image_total = _count(image_db, "SELECT COUNT(*) FROM images")
    video_total = _count(video_db, "SELECT COUNT(*) FROM videos")

    modality_counts: list[tuple[str, int]] = [
        ("Text", text_total),
        ("Images", image_total),
        ("Audio", audio_total),
        ("Video", video_total),
    ]
    if get_enable_code() or code_total >= 0:
        modality_counts.insert(1, ("Code", code_total))

    positive_counts = [c for _, c in modality_counts if c > 0]
    max_count = max(positive_counts) if positive_counts else 1

    modalities: list[ModalitySnapshot] = []
    for name, count in modality_counts:
        level, label = _level_from_count(name, count, runtime)
        ratio = 0.0 if count <= 0 else min(1.0, float(count) / float(max_count))
        modalities.append(
            ModalitySnapshot(
                name=name, count=count, status_label=label, level=level, ratio=ratio
            )
        )

    # ── Token estimation ──────────────────────────────────────────────
    # Gather raw bytes from all text content
    text_bytes = _count_total_bytes(
        text_db,
        "files",
        "content",
        "WHERE LOWER(category) NOT IN ('audio', 'video_transcript')",
    )
    if text_bytes < 0:
        text_bytes = 0

    code_lines, code_symbols = _count_code_tokens(code_db)
    if code_lines < 0:
        code_lines = 0
    if code_symbols < 0:
        code_symbols = 0

    # Code: ~24 bytes/line avg, symbols carry ~12 bytes each
    code_estimated_bytes = (code_lines * 24) + (code_symbols * 12)
    estimated_payload = text_bytes + code_estimated_bytes

    # FIX: when DBs are present but SUM returns 0, fall back to a row-count
    # estimate so naive tokens are never misleadingly zero.
    if estimated_payload == 0:
        # Rough: text avg 2 KB/file, code avg 200 bytes/file
        estimated_payload = (
            max(0, text_total) * 2048
            + max(0, code_total) * 200
            + max(0, image_total) * 64   # minimal metadata
            + max(0, audio_total) * 128
            + max(0, video_total) * 256
        )

    token_naive = _estimate_naive_tokens(estimated_payload)
    token_optimized = _estimate_optimized_tokens(estimated_payload)
    reduction = 0.0
    if token_naive > 0:
        reduction = (
            (float(token_naive) - float(token_optimized)) / float(token_naive)
        ) * 100.0

    token_health = _token_health_label(reduction)

    cfg = _config_path()
    config_ok = cfg.exists()
    config_label = (
        str(cfg) if config_ok else "No config — run contextcore init"
    )

    auto = autostart_status()
    autostart_ok = bool(auto.get("installed"))
    autostart_label = (
        auto.get("target", "installed") if autostart_ok else "Not installed"
    )

    ffmpeg_path = runtime.get("ffmpeg_path")
    ffmpeg_ok = bool(ffmpeg_path)
    ffmpeg_label = str(ffmpeg_path) if ffmpeg_ok else "Not found"

    clip_ok = bool(runtime.get("clip_ready"))
    clip_label = "Warmed" if clip_ok else "Not warmed"

    watch_folders = [
        WatchFolderSnapshot(path=str(path), exists=path.exists())
        for path in get_watch_directories()
    ]

    # ── 7-day trend data ──────────────────────────────────────────────
    day_labels = _local_day_labels(7)
    text_day_counts = _query_day_counts(
        text_db,
        """
        SELECT date(datetime(mtime, 'unixepoch', 'localtime')) AS day, COUNT(*) AS value
        FROM files
        GROUP BY day
        """,
        days=7,
    )
    image_day_counts = _query_day_counts(
        image_db,
        """
        SELECT date(datetime(mtime, 'unixepoch', 'localtime')) AS day, COUNT(*) AS value
        FROM images
        GROUP BY day
        """,
        days=7,
    )
    video_day_counts = _query_day_counts(
        video_db,
        """
        SELECT date(datetime(mtime, 'unixepoch', 'localtime')) AS day, COUNT(*) AS value
        FROM videos
        GROUP BY day
        """,
        days=7,
    )
    code_day_counts = _query_day_counts(
        code_db,
        """
        SELECT date(datetime(last_modified, 'unixepoch', 'localtime')) AS day, COUNT(*) AS value
        FROM project_files
        GROUP BY day
        """,
        days=7,
    )
    text_token_bytes_day = _query_day_counts(
        text_db,
        """
        SELECT date(datetime(mtime, 'unixepoch', 'localtime')) AS day, SUM(LENGTH(content)) AS value
        FROM files
        WHERE LOWER(category) NOT IN ('audio', 'video_transcript')
        GROUP BY day
        """,
        days=7,
    )
    code_line_day = _query_day_counts(
        code_db,
        """
        SELECT date(datetime(last_modified, 'unixepoch', 'localtime')) AS day, SUM(line_count) AS value
        FROM project_files
        GROUP BY day
        """,
        days=7,
    )

    trend_points: list[TrendPoint] = []
    indexed_totals: list[int] = []
    token_naive_series: list[int] = []
    token_saved_series: list[int] = []

    for day in day_labels:
        indexed_files = (
            text_day_counts.get(day, 0)
            + image_day_counts.get(day, 0)
            + video_day_counts.get(day, 0)
            + code_day_counts.get(day, 0)
        )
        day_payload_bytes = text_token_bytes_day.get(day, 0) + (
            code_line_day.get(day, 0) * 24
        )

        # Same fallback: if SUM(content) is 0 but files exist, estimate from count
        if day_payload_bytes == 0 and indexed_files > 0:
            day_payload_bytes = indexed_files * 2048

        if day_payload_bytes > 0:
            day_naive = _estimate_naive_tokens(day_payload_bytes)
            day_opt = _estimate_optimized_tokens(day_payload_bytes)
        else:
            day_naive = 0
            day_opt = 0
        day_saved = max(0, day_naive - day_opt)
        trend_points.append(
            TrendPoint(
                label=day,
                naive_tokens=day_naive,
                saved_tokens=day_saved,
                indexed_files=indexed_files,
            )
        )
        indexed_totals.append(indexed_files)
        token_naive_series.append(day_naive)
        token_saved_series.append(day_saved)

    indexed_today = indexed_totals[-1] if indexed_totals else 0
    indexed_yesterday = indexed_totals[-2] if len(indexed_totals) > 1 else 0
    indexed_delta = indexed_today - indexed_yesterday

    total_tokens_saved = token_naive - token_optimized
    total_files_indexed = max(
        0,
        (text_total if text_total >= 0 else 0)
        + (code_total if code_total >= 0 else 0)
        + (image_total if image_total >= 0 else 0)
        + (video_total if video_total >= 0 else 0),
    )

    # Sparkline data: use saved-token series for token chart; daily indexed for files
    # If all zeros (new install with no daily activity), spread the lifetime total
    # evenly so the sparkline isn't blank.
    _tok_spark = token_saved_series if any(token_saved_series) else [total_tokens_saved] * 7
    _idx_spark = indexed_totals if any(indexed_totals) else [total_files_indexed] * 7

    top_files = [
        TopSearchedFile(path=p, hit_count=h) for p, h in top_searched_files(limit=3)
    ]

    # Short ASCII line graph for the Rich console fallback
    unicode_ok = _ascii_safe()

    def _line_graph_simple(
        title: str,
        labels: list[str],
        a_name: str,
        a_values: list[int],
        b_name: str,
        b_values: list[int],
        a_lifetime: int = 0,
        b_lifetime: int = 0,
    ) -> str:
        axis = " ".join(label[5:] for label in labels)
        a_line = _sparkline(a_values, unicode_ok=unicode_ok)
        b_line = _sparkline(b_values, unicode_ok=unicode_ok)
        a_display = a_lifetime if a_lifetime > 0 else (a_values[-1] if a_values else 0)
        b_display = b_lifetime if b_lifetime > 0 else (b_values[-1] if b_values else 0)
        return "\n".join([
            f"[b]{title}[/b]",
            f"{a_name}: {a_line}  ({a_display:,})",
            f"{b_name}: {b_line}  ({b_display:,})",
            f"[dim]Days: {axis}[/dim]",
        ])

    token_vs_graph = _line_graph_simple(
        title="Token savings vs naive (7d)",
        labels=day_labels,
        a_name="Naive ",
        a_values=token_naive_series,
        b_name="Saved ",
        b_values=token_saved_series,
        b_lifetime=total_tokens_saved,
    )
    indexed_trend_graph = _line_graph_simple(
        title="Files indexed per day (7d)",
        labels=day_labels,
        a_name="Today    ",
        a_values=indexed_totals,
        b_name="Yesterday",
        b_values=[indexed_yesterday] * len(indexed_totals),
        a_lifetime=total_files_indexed,
    )

    return StatusSnapshot(
        port=port,
        server_ok=server_ok,
        server_label=server_label,
        server_detail=server_detail,
        mcp_ok=mcp_ok,
        mcp_label=mcp_label,
        index_label=index_label,
        index_detail=index_detail,
        autostart_ok=autostart_ok,
        autostart_label=autostart_label,
        modalities=modalities,
        token_naive=token_naive,
        token_optimized=token_optimized,
        token_reduction_pct=reduction,
        token_health=token_health,
        ffmpeg_ok=ffmpeg_ok,
        ffmpeg_label=ffmpeg_label,
        clip_ok=clip_ok,
        clip_label=clip_label,
        config_ok=config_ok,
        config_label=config_label,
        watch_folders=watch_folders,
        trend_points=trend_points,
        indexed_today=indexed_today,
        indexed_yesterday=indexed_yesterday,
        indexed_delta=indexed_delta,
        token_vs_graph=token_vs_graph,
        indexed_trend_graph=indexed_trend_graph,
        token_sparkline_data=_tok_spark,
        indexed_sparkline_data=_idx_spark,
        total_tokens_saved=total_tokens_saved,
        total_files_indexed=total_files_indexed,
        top_searched_files=top_files,
        next_action="Tip: run  contextcore index  to scan for new files.",
    )


# ─────────────────────────── TUI ────────────────────────────────────

_HEALTH_COLOR = {
    "good":     "green",
    "warn":     "yellow",
    "critical": "red",
}

_LEVEL_COLOR = {
    "ok":       "green",
    "warn":     "yellow",
    "critical": "red",
}

_STATUS_ICON = {
    "ok":       "●",
    "warn":     "◐",
    "critical": "✖",
}


class StatusDashboardApp(App[None]):

    CSS = """
    Screen {
        background: $surface;
        color: $text;
    }

    /* ── outer scroll container ── */
    #root {
        padding: 0 1 1 1;
    }

    /* ── hero banner ── */
    #hero {
        height: auto;
        margin: 0 0 1 0;
        padding: 1 2;
        border: heavy $primary;
        background: $primary 10%;
    }

    #hero-title {
        text-style: bold;
        color: $primary;
    }

    #hero-subtitle {
        color: $text-muted;
    }

    /* ── section headings ── */
    .section-heading {
        margin: 1 0 0 0;
        padding: 0 1;
        text-style: bold;
        color: $accent;
        border-bottom: solid $accent 20%;
        height: 2;
    }

    /* ── KPI row ── */
    .kpi-row {
        height: 7;
        width: 100%;
        margin: 0 0 1 0;
    }

    .kpi-card {
        width: 1fr;
        height: 100%;
        margin: 0 1 0 0;
        padding: 0 1;
        border: round $primary 40%;
        background: $surface-darken-1;
    }

    .kpi-label {
        color: $text-muted;
        margin-top: 1;
    }

    .kpi-value {
        text-style: bold;
        color: $accent;
    }

    .kpi-sub {
        color: $text-muted;
    }

    /* ── sparkline panels ── */
    .spark-row {
        height: 6;
        width: 100%;
        margin: 0 0 1 0;
    }

    .spark-panel {
        width: 1fr;
        height: 100%;
        margin: 0 1 0 0;
        padding: 0 1;
        border: round $accent 40%;
        background: $surface-darken-1;
    }

    .spark-label {
        color: $text-muted;
        height: 1;
        margin-bottom: 0;
    }

    .spark-widget {
        height: 3;
        width: 100%;
    }

    .spark-stat {
        color: $accent;
        height: 1;
    }

    /* ── status cards ── */
    .status-row {
        height: auto;
        width: 100%;
        margin: 0 0 1 0;
    }

    .status-card {
        width: 1fr;
        min-height: 5;
        margin: 0 1 0 0;
        padding: 0 1;
        border: round $primary 30%;
    }

    /* ── detail panels (modalities, token, runtime, config) ── */
    .detail-row {
        height: auto;
        width: 100%;
        margin: 0 0 1 0;
    }

    .detail-panel {
        width: 1fr;
        min-height: 8;
        margin: 0 1 0 0;
        padding: 0 1;
        border: round $accent 30%;
    }

    .panel-heading {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
        height: 2;
        border-bottom: dashed $accent 20%;
    }

    /* ── tables ── */
    .table-panel {
        margin: 0 0 1 0;
        padding: 0 1;
        border: round $accent 30%;
        height: auto;
    }

    #modalities-table {
        height: auto;
        max-height: 10;
    }

    #watch-table {
        height: auto;
        max-height: 14;
    }

    #top-search-table {
        height: auto;
        max-height: 7;
    }

    /* ── colour helpers ── */
    .ok       { color: #40c057; }
    .warn     { color: #fab005; }
    .critical { color: #fa5252; }

    /* ── footer note ── */
    #footer-note {
        margin: 0 0 1 0;
        height: 2;
        color: $text-muted;
        border-top: dashed $primary 20%;
        padding-top: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "quit", "Quit"),
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
    ]

    def __init__(self, snapshot: StatusSnapshot) -> None:
        super().__init__()
        self.snapshot = snapshot

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll(id="root"):

            # ── Hero ────────────────────────────────────────────────
            with Vertical(id="hero"):
                yield Static("ContextCore  ·  Status Dashboard", id="hero-title")
                yield Static(id="hero-subtitle")

            # ── KPI row ─────────────────────────────────────────────
            yield Static("  Overview", classes="section-heading")
            with Horizontal(classes="kpi-row"):
                yield Static(id="kpi-files",   classes="kpi-card")
                yield Static(id="kpi-naive",   classes="kpi-card")
                yield Static(id="kpi-saved",   classes="kpi-card")
                yield Static(id="kpi-reduced", classes="kpi-card")

            # ── Sparkline row ───────────────────────────────────────
            yield Static("  Trends  (7 days)", classes="section-heading")
            with Horizontal(classes="spark-row"):
                with Vertical(classes="spark-panel"):
                    yield Static("Token savings / day", classes="spark-label")
                    yield Sparkline(id="token-sparkline", classes="spark-widget")
                    yield Static(id="token-spark-stat", classes="spark-stat")
                with Vertical(classes="spark-panel"):
                    yield Static("Files indexed / day", classes="spark-label")
                    yield Sparkline(id="indexed-sparkline", classes="spark-widget")
                    yield Static(id="indexed-spark-stat", classes="spark-stat")

            # ── Status cards ────────────────────────────────────────
            yield Static("  Services", classes="section-heading")
            with Horizontal(classes="status-row"):
                yield Static(id="card-server",    classes="status-card")
                yield Static(id="card-mcp",       classes="status-card")
                yield Static(id="card-index",     classes="status-card")
                yield Static(id="card-autostart", classes="status-card")

            # ── Modalities table ────────────────────────────────────
            yield Static("  Indexed Content", classes="section-heading")
            with Vertical(classes="table-panel"):
                yield DataTable(id="modalities-table")

            # ── Token / Runtime / Config detail panels ──────────────
            yield Static("  System Detail", classes="section-heading")
            with Horizontal(classes="detail-row"):
                with Vertical(classes="detail-panel"):
                    yield Static("Token Health", classes="panel-heading")
                    yield Static(id="token-detail")
                with Vertical(classes="detail-panel"):
                    yield Static("Runtime", classes="panel-heading")
                    yield Static(id="runtime-detail")
                with Vertical(classes="detail-panel"):
                    yield Static("Config & Autostart", classes="panel-heading")
                    yield Static(id="config-detail")

            # ── Top searched files ──────────────────────────────────
            yield Static("  Top Searched Files", classes="section-heading")
            with Vertical(classes="table-panel"):
                yield DataTable(id="top-search-table")

            # ── Watch folders ───────────────────────────────────────
            yield Static("  Watch Folders", classes="section-heading")
            with Vertical(classes="table-panel"):
                yield DataTable(id="watch-table")

            yield Static(id="footer-note")

        yield Footer()

    # ── Lifecycle ───────────────────────────────────────────────────

    def on_mount(self) -> None:
        self._apply_theme_class()
        self._render_hero()
        self._render_kpis()
        self._render_sparklines()
        self._render_status_cards()
        self._render_modality_table()
        self._render_detail_panels()
        self._render_top_search_table()
        self._render_watch_table()
        self.query_one("#footer-note", Static).update(
            f"[dim]{self.snapshot.next_action}   Press [bold]r[/bold] to refresh · [bold]q[/bold] to quit[/dim]"
        )

    def action_refresh(self) -> None:
        """Re-collect data and re-render everything."""
        self.snapshot = _collect_status_snapshot(self.snapshot.port)
        self._render_hero()
        self._render_kpis()
        self._render_sparklines()
        self._render_status_cards()
        self._render_modality_table()
        self._render_detail_panels()
        self._render_top_search_table()
        self._render_watch_table()

    # ── Helpers ─────────────────────────────────────────────────────

    def _apply_theme_class(self) -> None:
        theme = get_theme_name()
        if theme == "light":
            self.add_class("light-theme")
            self.remove_class("dark-theme")
        else:
            self.add_class("dark-theme")
            self.remove_class("light-theme")

    def _lc(self, ok: bool) -> str:
        return "ok" if ok else "warn"

    def _icon(self, level: str) -> str:
        return _STATUS_ICON.get(level, "?")

    # ── Render helpers ───────────────────────────────────────────────

    def _render_hero(self) -> None:
        snap = self.snapshot
        health_color = _HEALTH_COLOR.get(snap.token_health, "yellow")
        self.query_one("#hero-subtitle", Static).update(
            f"[dim]Port [bold]{snap.port}[/bold]  ·  "
            f"Files [bold]{_fmt_files(snap.total_files_indexed)}[/bold]  ·  "
            f"Tokens saved [{health_color}][bold]{_fmt_tokens(snap.total_tokens_saved)}[/bold] "
            f"({snap.token_reduction_pct:.0f}% reduction)[/{health_color}][/dim]"
        )

    def _render_kpis(self) -> None:
        snap = self.snapshot

        def _kpi(widget_id: str, label: str, value: str, sub: str = "") -> None:
            w = self.query_one(f"#{widget_id}", Static)
            lines = [
                f"[dim]{label}[/dim]",
                f"[bold cyan]{value}[/bold cyan]",
            ]
            if sub:
                lines.append(f"[dim]{sub}[/dim]")
            w.update("\n".join(lines))

        _kpi(
            "kpi-files",
            "Total files indexed",
            f"{snap.total_files_indexed:,}",
            f"Today +{snap.indexed_today:,}  ·  Yesterday +{snap.indexed_yesterday:,}",
        )

        health_color = _HEALTH_COLOR.get(snap.token_health, "yellow")
        self.query_one("#kpi-naive", Static).update(
            "\n".join([
                "[dim]Naive tokens (unoptimised)[/dim]",
                f"[bold]{_fmt_tokens(snap.token_naive)}[/bold]",
                "[dim]Without ContextCore[/dim]",
            ])
        )
        self.query_one("#kpi-saved", Static).update(
            "\n".join([
                "[dim]Tokens saved[/dim]",
                f"[bold {health_color}]{_fmt_tokens(snap.total_tokens_saved)}[/bold {health_color}]",
                f"[dim]→ {_fmt_tokens(snap.token_optimized)} optimised[/dim]",
            ])
        )
        self.query_one("#kpi-reduced", Static).update(
            "\n".join([
                "[dim]Reduction[/dim]",
                f"[bold {health_color}]{snap.token_reduction_pct:.1f}%[/bold {health_color}]",
                f"[dim][{health_color}]{snap.token_health.upper()}[/{health_color}][/dim]",
            ])
        )

    def _render_sparklines(self) -> None:
        snap = self.snapshot
        unicode_ok = _ascii_safe()

        # Token savings sparkline
        tok_spark = self.query_one("#token-sparkline", Sparkline)
        tok_spark.data = snap.token_sparkline_data if snap.token_sparkline_data else [0]

        tok_ascii = _sparkline(snap.token_sparkline_data, unicode_ok=unicode_ok)
        self.query_one("#token-spark-stat", Static).update(
            f"[dim]7d: [/dim][cyan]{tok_ascii}[/cyan]  [dim]total [/dim][bold]{_fmt_tokens(snap.total_tokens_saved)}[/bold]"
        )

        # Files indexed sparkline
        idx_spark = self.query_one("#indexed-sparkline", Sparkline)
        idx_spark.data = snap.indexed_sparkline_data if snap.indexed_sparkline_data else [0]

        idx_ascii = _sparkline(snap.indexed_sparkline_data, unicode_ok=unicode_ok)
        delta_sign = "+" if snap.indexed_delta >= 0 else ""
        delta_color = "green" if snap.indexed_delta >= 0 else "yellow"
        self.query_one("#indexed-spark-stat", Static).update(
            f"[dim]7d: [/dim][cyan]{idx_ascii}[/cyan]  "
            f"[{delta_color}]{delta_sign}{snap.indexed_delta:,} vs yesterday[/{delta_color}]"
        )

    def _render_status_cards(self) -> None:
        snap = self.snapshot

        def _card(widget_id: str, title: str, level: str, label: str, detail: str = "") -> None:
            icon = self._icon(level)
            color = _LEVEL_COLOR.get(level, "yellow")
            lines = [
                f"[bold]{title}[/bold]",
                f"[{color}]{icon}  {label}[/{color}]",
            ]
            if detail:
                lines.append(f"[dim]{detail}[/dim]")
            self.query_one(f"#{widget_id}", Static).update("\n".join(lines))

        _card("card-server",    "Server",    self._lc(snap.server_ok),    snap.server_label,    snap.server_detail)
        _card("card-mcp",       "MCP",       self._lc(snap.mcp_ok),       snap.mcp_label)
        _card("card-index",     "Index",     "ok" if "progress" in snap.index_label.lower() or snap.index_label == "Idle" else "warn",
              snap.index_label, snap.index_detail)
        _card("card-autostart", "Autostart", self._lc(snap.autostart_ok), snap.autostart_label)

    def _render_modality_table(self) -> None:
        snap = self.snapshot
        unicode_ok = _ascii_safe()
        table = self.query_one("#modalities-table", DataTable)
        table.clear(columns=True)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("Modality", "Files", "Share", "Bar", "Status")

        max_count = max((m.count for m in snap.modalities if m.count > 0), default=1)
        total_files = sum(m.count for m in snap.modalities if m.count > 0)

        for m in snap.modalities:
            display_count = "-" if m.count < 0 else f"{m.count:,}"
            pct = f"{100 * m.count / total_files:.0f}%" if total_files > 0 and m.count > 0 else "-"
            bar_val = 0 if m.count < 0 else m.count
            bar = _mini_bar(bar_val, max_count, width=14, unicode_ok=unicode_ok)
            icon = self._icon(m.level)
            color = _LEVEL_COLOR.get(m.level, "yellow")
            table.add_row(
                m.name,
                display_count,
                pct,
                bar,
                f"[{color}]{icon}  {m.status_label}[/{color}]",
            )

    def _render_detail_panels(self) -> None:
        snap = self.snapshot
        health_color = _HEALTH_COLOR.get(snap.token_health, "yellow")

        # Token health
        self.query_one("#token-detail", Static).update(
            "\n".join([
                f"Naive      [bold]{_fmt_tokens(snap.token_naive)}[/bold]  [dim](unoptimised)[/dim]",
                f"Optimised  [bold cyan]{_fmt_tokens(snap.token_optimized)}[/bold cyan]",
                f"Saved      [bold {health_color}]{_fmt_tokens(snap.total_tokens_saved)}[/bold {health_color}]",
                f"Reduction  [bold {health_color}]{snap.token_reduction_pct:.1f}%[/bold {health_color}]  [{health_color}]({snap.token_health.upper()})[/{health_color}]",
            ])
        )

        # Runtime
        ff_level  = self._lc(snap.ffmpeg_ok)
        cl_level  = self._lc(snap.clip_ok)
        ff_icon   = self._icon(ff_level)
        cl_icon   = self._icon(cl_level)
        ff_color  = _LEVEL_COLOR.get(ff_level, "yellow")
        cl_color  = _LEVEL_COLOR.get(cl_level, "yellow")
        self.query_one("#runtime-detail", Static).update(
            "\n".join([
                f"[{ff_color}]{ff_icon}  ffmpeg   {snap.ffmpeg_label}[/{ff_color}]",
                f"[{cl_color}]{cl_icon}  CLIP     {snap.clip_label}[/{cl_color}]",
            ])
        )

        # Config & autostart
        cfg_level  = self._lc(snap.config_ok)
        auto_level = self._lc(snap.autostart_ok)
        cfg_icon   = self._icon(cfg_level)
        auto_icon  = self._icon(auto_level)
        cfg_color  = _LEVEL_COLOR.get(cfg_level, "yellow")
        auto_color = _LEVEL_COLOR.get(auto_level, "yellow")
        self.query_one("#config-detail", Static).update(
            "\n".join([
                f"[{cfg_color}]{cfg_icon}  Config      {snap.config_label}[/{cfg_color}]",
                f"[{auto_color}]{auto_icon}  Autostart   {snap.autostart_label}[/{auto_color}]",
            ])
        )

    def _render_top_search_table(self) -> None:
        table = self.query_one("#top-search-table", DataTable)
        table.clear(columns=True)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("#", "Path", "Hits")

        if not self.snapshot.top_searched_files:
            table.add_row("-", "No search history yet", "0")
            return

        for idx, item in enumerate(self.snapshot.top_searched_files, 1):
            table.add_row(str(idx), _truncate_path(item.path, 70), f"{item.hit_count:,}")

    def _render_watch_table(self) -> None:
        table = self.query_one("#watch-table", DataTable)
        table.clear(columns=True)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("#", "Path", "Status")

        if not self.snapshot.watch_folders:
            table.add_row("-", "No watch folders configured", "[warn]◐ missing[/warn]")
            return

        for idx, item in enumerate(self.snapshot.watch_folders, 1):
            level = "ok" if item.exists else "critical"
            icon  = self._icon(level)
            color = _LEVEL_COLOR.get(level, "yellow")
            table.add_row(
                str(idx),
                _truncate_path(item.path, 70),
                f"[{color}]{icon}  {'exists' if item.exists else 'missing'}[/{color}]",
            )


# ─────────────────────────── CLI entry point ────────────────────────

def run_status(port: int = DEFAULT_PORT) -> None:
    snapshot = _collect_status_snapshot(port=port)

    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    unicode_ok = _ascii_safe()
    console = Console(force_terminal=unicode_ok)

    console.print()
    console.print(
        Panel.fit(
            Text.assemble(
                ("[bold cyan]ContextCore Status Dashboard[/bold cyan]\n\n", "bold"),
                "Lifetime Token Savings: [green bold]",
                f"{snapshot.total_tokens_saved:,}[/green bold] tokens\n",
                f"  Naive (unoptimised):  [yellow]{snapshot.token_naive:,}[/yellow]\n",
                f"  Optimised:            [cyan]{snapshot.token_optimized:,}[/cyan]\n",
                f"  Reduction:            [green]{snapshot.token_reduction_pct:.1f}%[/green]\n\n",
                "Total Files Indexed: [cyan bold]",
                f"{snapshot.total_files_indexed:,}[/bold cyan] files\n",
                f"  Today: {snapshot.indexed_today:,}  ·  Yesterday: {snapshot.indexed_yesterday:,}\n",
            ),
            title="[bold]STATUS OVERVIEW[/bold]",
            border_style="cyan",
            padding=(1, 2),
        )
    )
    console.print()

    tok_spark = _sparkline(snapshot.token_sparkline_data, unicode_ok=unicode_ok) or "N/A"
    idx_spark = _sparkline(snapshot.indexed_sparkline_data, unicode_ok=unicode_ok) or "N/A"

    table = Table(title="[bold]SPARKLINE TRENDS (7-day)[/bold]")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")
    table.add_column("Trend", style="yellow")
    table.add_row("Token Savings", f"{snapshot.total_tokens_saved:,} tokens", tok_spark)
    table.add_row("Files Indexed", f"{snapshot.total_files_indexed:,} files",  idx_spark)
    console.print(table)
    console.print()

    modal_table = Table(title="[bold]INDEXED BY MODALITY[/bold]")
    modal_table.add_column("Modality", style="cyan")
    modal_table.add_column("Count", style="green", justify="right")
    modal_table.add_column("Status", style="yellow")
    for m in snapshot.modalities:
        status = "[green]ready[/green]" if m.count > 0 else "[dim]empty[/dim]"
        modal_table.add_row(m.name, f"{m.count:,}" if m.count >= 0 else "-", status)
    console.print(modal_table)
    console.print()

    console.print("[dim]Launching TUI dashboard — press Escape or q to exit.[/dim]")
    console.print()

    app = StatusDashboardApp(snapshot)
    app.run()
