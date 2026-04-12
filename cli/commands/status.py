from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, ScrollableContainer
from textual.widgets import (
    Label,
    Static,
    DataTable,
    Rule,
    Footer,
    Header,
    ProgressBar,
)
from textual.widget import Widget
from textual.reactive import reactive
from textual.css.query import NoMatches

from cli.constants import DEFAULT_PORT
from cli.lifecycle import (
    autostart_status,
    get_port_usage,
    index_lock_active,
    read_index_state,
)
from cli.paths import get_sdk_root
from cli.ui import console, error, header, section, success, warning
from cli.ui import get_setup_theme

_SDK_ROOT = get_sdk_root()
if str(_SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(_SDK_ROOT))

from video_search_implementation_v2.runtime import video_runtime_status  # noqa: E402
from config import get_enable_code, get_watch_directories, get_storage_dir  # noqa: E402


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

    def __init__(self, online: bool, **kwargs):
        symbol = "■ SYSTEM ONLINE" if online else "■ SYSTEM OFFLINE"
        cls = "system-status-online" if online else "system-status-offline"
        super().__init__(symbol, classes=cls, **kwargs)


# ── Main App ──────────────────────────────────────────────────────────────────

class StatusDashboard(App):
    """ContextCore status dashboard with Textual UI."""

    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
        Binding("escape", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit"),
    ]

    def __init__(self, port: int = DEFAULT_PORT, **kwargs):
        theme = get_setup_theme()  # "light" or "dark"
        self.CSS = LIGHT_CSS if theme == "light" else DARK_CSS
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

    def action_quit(self) -> None:
        self.exit()


# ── Public entry point ────────────────────────────────────────────────────────

def run_status(port: int = DEFAULT_PORT) -> None:
    app = StatusDashboard(port=port)
    app.run()