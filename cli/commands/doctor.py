# cli/commands/doctor.py
#
# contextcore doctor - diagnostic check of the local installation.

from __future__ import annotations

import json
import platform
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from cli.constants import DEFAULT_PORT
from cli.lifecycle import (
    autostart_status,
    get_port_usage,
    index_lock_active,
    read_index_state,
)
from cli.paths import get_sdk_root
from cli.ui import console, error, header, hint, section, success, warning


def _check(label: str, ok: bool, fix_label: str = "", fix_cmd: str = "") -> bool:
    if ok:
        success(label)
    else:
        error(label)
        if fix_cmd:
            hint(fix_label, fix_cmd)
    return ok


def run_doctor() -> None:
    header("ContextCore Doctor")
    console.print("[dim]Checking your ContextCore setup...[/dim]")

    sdk_root = get_sdk_root()
    issues = 0

    section("Runtime")
    py_ok = sys.version_info >= (3, 10)
    if not _check(
        f"Python {sys.version.split()[0]} {'(OK)' if py_ok else '(requires 3.10+)'}",
        py_ok,
        "upgrade python",
        "https://python.org/downloads",
    ):
        issues += 1

    try:
        with sqlite3.connect(":memory:") as c:
            c.execute("SELECT 1").fetchone()
        _check("SQLite accessible", True)
    except Exception as e:
        _check(f"SQLite error: {e}", False)
        issues += 1

    try:
        import sqlite_vec  # noqa: F401
        _check("sqlite-vec installed", True)
    except ImportError:
        _check(
            "sqlite-vec not installed",
            False,
            "install sqlite-vec",
            ".venv/Scripts/pip install sqlite-vec",
        )
        issues += 1

    section("Configuration")
    cfg = Path.home() / ".contextcore" / "contextcore.yaml"
    if _check(
        f"Config file at {cfg}",
        cfg.exists(),
        "run init to create config",
        "contextcore init",
    ):
        for line in cfg.read_text(encoding="utf-8").splitlines():
            if line.startswith("organized_root:"):
                val = line.split(":", 1)[1].strip().strip("'\"")
                root = Path(val)
                _check(
                    f"organized_root exists: {root}",
                    root.exists(),
                    "create the directory or update the config",
                    f"mkdir \"{root}\"",
                )
                if not root.exists():
                    issues += 1
                break
    else:
        issues += 1

    section("Autostart")
    auto = autostart_status()
    installed = bool(auto.get("installed"))
    if _check(
        f"Autostart {'installed' if installed else 'not installed'}",
        installed,
        "repair autostart",
        "contextcore init",
    ):
        if auto.get("target"):
            success(f"Autostart target: {auto.get('target')}")
    else:
        issues += 1

    section("Index Lock")
    active_lock, state = index_lock_active()
    if active_lock:
        success("A full index job is active")
        if state.get("source"):
            success(f"Source: {state.get('source')}")
    else:
        success("No active full index lock")
    if state.get("stale_lock_recovered_at"):
        warning(f"Recovered stale lock at {state.get('stale_lock_recovered_at')}")
    elif read_index_state().get("active"):
        warning("Index state says active, but no live lock was found")

    section("MCP Server")
    mcp = sdk_root / "mcp_server.py"
    if _check(
        "mcp_server.py found",
        mcp.exists(),
        "reinstall contextcore",
        "pip install --force-reinstall contextcore",
    ):
        import_result = subprocess.run(
            [sys.executable, "-c", "import mcp_server"],
            capture_output=True,
            cwd=str(sdk_root),
            timeout=10,
        )
        _check(
            "mcp_server imports cleanly",
            import_result.returncode == 0,
            "retry MCP import check",
            f"cd \"{sdk_root}\" && \"{sys.executable}\" -c \"import mcp_server\"",
        )
        if import_result.returncode != 0:
            console.print(f"  [dim]{import_result.stderr.strip()[-400:]}[/dim]")
            issues += 1
    else:
        issues += 1

    section("FastAPI Server")
    usage = get_port_usage(DEFAULT_PORT)
    if usage.get("is_contextcore"):
        success(f"Server listening on port {DEFAULT_PORT}")
    elif usage.get("in_use"):
        pid = usage.get("pid")
        name = usage.get("process_name") or "unknown"
        conflict_label = (
            f"Port {DEFAULT_PORT} is occupied by {name}"
            f"{f' (PID {pid})' if pid else ''}"
        )
        _check(
            conflict_label,
            False,
            "inspect the conflicting process",
            (
                f"tasklist /FI \"PID eq {pid}\""
                if platform.system() == "Windows" and pid
                else f"ps -p {pid} -o pid,comm,args"
                if pid
                else ""
            ),
        )
        if platform.system() == "Windows" and pid:
            hint("stop it if appropriate", f"taskkill /F /PID {pid}")
        elif pid:
            hint("stop it if appropriate", f"kill {pid}")
        issues += 1
    else:
        _check(
            f"Server listening on port {DEFAULT_PORT}",
            False,
            "start the server",
            "contextcore serve",
        )
        issues += 1

    section("Claude Desktop")
    plat = platform.system().lower()
    if plat == "windows":
        import os
        claude_cfg = (
            Path(os.environ.get("APPDATA", "~"))
            / "Claude"
            / "claude_desktop_config.json"
        )
    elif plat == "darwin":
        claude_cfg = (
            Path.home()
            / "Library"
            / "Application Support"
            / "Claude"
            / "claude_desktop_config.json"
        )
    else:
        claude_cfg = Path.home() / ".config" / "Claude" / "claude_desktop_config.json"

    claude_cfg = claude_cfg.expanduser()
    if _check(
        f"Claude Desktop config found at {claude_cfg}",
        claude_cfg.exists(),
        "install Claude Desktop or open it once to create the config",
        "https://claude.ai/download",
    ):
        try:
            data = json.loads(claude_cfg.read_text(encoding="utf-8"))
            has_cc = "contextcore" in data.get("mcpServers", {})
            _check(
                "ContextCore registered in Claude Desktop",
                has_cc,
                "re-register",
                "contextcore register claude-desktop",
            )
            if not has_cc:
                issues += 1
        except Exception as e:
            error(f"Could not read Claude config: {e}")
            issues += 1
    else:
        issues += 1

    section("Optional Models")
    try:
        import torch  # noqa: F401
        success("torch installed (image/video search available)")
    except ImportError:
        warning("torch not installed - image/video search unavailable")
        hint("install vision model", "contextcore install clip")

    try:
        import faster_whisper  # noqa: F401
        success("faster-whisper installed (audio search available)")
    except ImportError:
        warning("faster-whisper not installed - audio search unavailable")
        hint("install audio model", "contextcore install audio")

    section("Image Search Capabilities")
    try:
        import pytesseract  # noqa: F401
        success("pytesseract installed (OCR Python package available)")
    except ImportError:
        warning("pytesseract not installed - OCR text extraction disabled")
        hint("install OCR package", f"{sys.executable} -m pip install pytesseract")

    tesseract_ok = bool(shutil.which("tesseract"))
    if tesseract_ok:
        success("tesseract binary found (OCR runtime available)")
    else:
        warning("tesseract binary not found - OCR text extraction disabled")
        if platform.system() == "Windows":
            hint("install tesseract", "winget install UB-Mannheim.TesseractOCR")
        elif platform.system() == "Darwin":
            hint("install tesseract", "brew install tesseract")
        else:
            hint("install tesseract", "sudo apt-get install tesseract-ocr")

    try:
        import annoy  # noqa: F401
        success("annoy installed (semantic image ANN backend available)")
    except ImportError:
        warning(
            "annoy not installed - semantic image search disabled "
            "(OCR/filename still works)"
        )
        hint("install annoy", f"{sys.executable} -m pip install annoy")

    console.print()
    if issues == 0:
        console.print(
            "[bold green]All checks passed[/bold green]  ContextCore is healthy."
        )
    else:
        console.print(
            f"[bold red]{issues} issue{'s' if issues > 1 else ''} found.[/bold red]  "
            "Follow the Fix: suggestions above."
        )
    console.print()
