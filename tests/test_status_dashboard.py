from __future__ import annotations

import time
import sqlite3
from pathlib import Path

import cli.commands.status as status


def _make_text_db(path: Path) -> None:
    now = time.time()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(path)) as conn:
        conn.execute("CREATE TABLE files (category TEXT, content TEXT, mtime REAL)")
        conn.execute(
            "INSERT INTO files(category, content, mtime) VALUES ('text', 'hello world token payload', ?)",
            (now,),
        )
        conn.execute(
            "INSERT INTO files(category, content, mtime) VALUES ('audio', 'audio transcript payload', ?)",
            (now - 86400,),
        )


def _make_image_db(path: Path) -> None:
    now = time.time()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(path)) as conn:
        conn.execute("CREATE TABLE images (id INTEGER PRIMARY KEY, mtime REAL)")
        conn.execute("INSERT INTO images(mtime) VALUES (?)", (now,))


def _make_video_db(path: Path) -> None:
    now = time.time()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(path)) as conn:
        conn.execute("CREATE TABLE videos (id INTEGER PRIMARY KEY, mtime REAL)")
        conn.execute("INSERT INTO videos(mtime) VALUES (?)", (now - 86400,))


def _make_code_db(path: Path) -> None:
    now = time.time()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(path)) as conn:
        conn.execute(
            "CREATE TABLE project_files (line_count INTEGER, last_modified REAL)"
        )
        conn.execute("CREATE TABLE code_symbols (name TEXT)")
        conn.execute(
            "INSERT INTO project_files(line_count, last_modified) VALUES (120, ?)",
            (now,),
        )
        conn.execute("INSERT INTO code_symbols(name) VALUES ('main')")


def test_collect_status_snapshot_builds_expected_metrics(tmp_path, monkeypatch):
    text_db = (
        tmp_path
        / "text_search_implementation_v2"
        / "storage"
        / "text_search_implementation_v2.db"
    )
    image_db = (
        tmp_path / "image_search_implementation_v2" / "storage" / "images_meta.db"
    )
    video_db = (
        tmp_path / "video_search_implementation_v2" / "storage" / "videos_meta.db"
    )
    code_db = tmp_path / "storage" / "code_index_layer1.db"
    _make_text_db(text_db)
    _make_image_db(image_db)
    _make_video_db(video_db)
    _make_code_db(code_db)

    watch_existing = tmp_path / "watch-existing"
    watch_existing.mkdir(parents=True, exist_ok=True)
    watch_missing = tmp_path / "watch-missing"

    fake_home = tmp_path / "home"
    cfg = fake_home / ".contextcore" / "contextcore.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("ui_theme: dark\n", encoding="utf-8")

    monkeypatch.setattr(status, "get_storage_dir", lambda: tmp_path)
    monkeypatch.setattr(
        status, "get_watch_directories", lambda: [watch_existing, watch_missing]
    )
    monkeypatch.setattr(status, "get_enable_code", lambda: True)
    monkeypatch.setattr(
        status,
        "video_runtime_status",
        lambda: {
            "ffmpeg_ready": True,
            "clip_ready": True,
            "ffmpeg_path": "C:/ffmpeg/bin/ffmpeg.exe",
        },
    )
    monkeypatch.setattr(
        status,
        "autostart_status",
        lambda: {"installed": True, "target": "Task Scheduler"},
    )
    monkeypatch.setattr(status, "index_lock_active", lambda: (False, {}))
    monkeypatch.setattr(
        status,
        "read_index_state",
        lambda: {
            "started_at": "2026-04-17T10:00:00",
            "result": "success",
            "completed_at": "2026-04-17T10:05:00",
        },
    )
    monkeypatch.setattr(status, "get_port_usage", lambda _port: {"in_use": False})
    monkeypatch.setattr(status, "get_sdk_root", lambda: tmp_path)
    monkeypatch.setattr(status, "_config_path", lambda: cfg)
    monkeypatch.setattr(
        status,
        "top_searched_files",
        lambda limit=3: [("C:/docs/a.txt", 7), ("C:/docs/b.txt", 3)][:limit],
    )

    (tmp_path / "mcp_server.py").write_text("# ok\n", encoding="utf-8")

    monkeypatch.setattr("cli.server.ensure_server", lambda *args, **kwargs: None)
    monkeypatch.setattr("cli.server.is_server_running", lambda _port: True)

    snap = status._collect_status_snapshot(port=8123)

    assert snap.server_ok is True
    assert snap.mcp_ok is True
    assert snap.autostart_ok is True
    assert snap.config_ok is True
    assert len(snap.modalities) == 5
    assert any(m.name == "Code" and m.count == 1 for m in snap.modalities)
    assert snap.token_naive > snap.token_optimized
    assert snap.token_reduction_pct > 0
    assert len(snap.watch_folders) == 2
    assert snap.watch_folders[0].exists is True
    assert snap.watch_folders[1].exists is False
    assert snap.token_vs_graph
    assert snap.indexed_trend_graph
    assert snap.indexed_today >= 0
    assert len(snap.top_searched_files) == 2


def test_graph_helpers_ascii_fallback():
    bar = status._mini_bar(value=5, max_value=10, width=8, unicode_ok=False)
    assert bar == "####----"


def test_token_health_thresholds():
    assert status._token_health_label(55.0) == "good"
    assert status._token_health_label(25.0) == "warn"
    assert status._token_health_label(5.0) == "critical"


def test_watch_path_truncation():
    value = "C:/" + "very-long-segment/" * 10
    truncated = status._truncate_path(value, width=24)
    assert len(truncated) <= 24
    assert truncated.endswith("...")
