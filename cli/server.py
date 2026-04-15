# cli/server.py
#
# Background FastAPI server management.
# Auto-starts the server when any CLI command needs it,
# so the user never has to run 'contextcore serve' manually.

from __future__ import annotations
import platform
import subprocess
import time
import re
from pathlib import Path

from cli.constants import DEFAULT_PORT
from cli.env import build_runtime_env
from cli.lifecycle import (
    build_background_server_command,
    get_port_usage,
    is_contextcore_healthy,
    stop_pid,
)
from cli.paths import get_sdk_root, get_default_config
from cli.ui import console, success, info, warning, error

_PID_FILE = Path.home() / ".contextcore" / "server.pid"


def _find_pid_by_port(port: int) -> int | None:
    try:
        if platform.system() == "Windows":
            output = subprocess.check_output(
                ["netstat", "-ano", "-p", "tcp"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
            pattern = re.compile(rf"^\s*TCP\s+\S+:{port}\s+\S+\s+LISTENING\s+(\d+)\s*$", re.IGNORECASE)
            for line in output.splitlines():
                match = pattern.match(line)
                if match:
                    return int(match.group(1))
            return None

        output = subprocess.check_output(
            ["lsof", "-ti", f"tcp:{port}"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if output:
            return int(output.splitlines()[0].strip())
    except Exception:
        return None
    return None


def _write_pid_file(pid: int) -> None:
    try:
        _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PID_FILE.write_text(str(pid), encoding="utf-8")
    except Exception as exc:
        warning(f"Could not persist background server PID: {exc}")


def is_server_running(port: int = DEFAULT_PORT) -> bool:
    """Check if the FastAPI server is listening."""
    return is_contextcore_healthy(port)


def describe_port_conflict(port: int = DEFAULT_PORT) -> str | None:
    usage = get_port_usage(port)
    if not usage.get("in_use") or usage.get("is_contextcore"):
        return None
    pid = usage.get("pid")
    name = usage.get("process_name") or "unknown"
    return f"port {port} is already in use by {name} (PID {pid})" if pid else f"port {port} is already in use"


def _print_port_conflict(port: int = DEFAULT_PORT) -> None:
    usage = get_port_usage(port)
    if not usage.get("in_use") or usage.get("is_contextcore"):
        return
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


def ensure_server(port: int = DEFAULT_PORT, silent: bool = False, force_restart: bool = False) -> bool:
    """
    Make sure the FastAPI server is running.
    If it's already up, returns True immediately.
    If not, starts it in the background and waits until it responds.
    
    This is the ONLY function other CLI commands should call.
    """
    if force_restart and is_server_running(port):
        if not silent:
            info(f"Restarting ContextCore server on port {port} to load new dependencies...")
        if not stop_server():
            if not silent:
                warning("Could not stop the existing background server cleanly. Trying to continue.")
        else:
            for _ in range(20):
                time.sleep(0.25)
                if not is_server_running(port):
                    break

    if is_server_running(port):
        if not silent:
            success(f"Server already running on port {port}")
        return True

    usage = get_port_usage(port)
    if usage.get("in_use") and not usage.get("is_contextcore"):
        if not silent:
            _print_port_conflict(port)
        return False

    if not silent:
        info("Starting ContextCore server in background...")

    ok = _start_background(port)
    if not ok:
        return False

    # Wait for server to come up (up to 15 seconds)
    for attempt in range(30):
        time.sleep(0.5)
        if is_server_running(port):
            if not silent:
                success(f"Server started on port {port}")
            return True

    if not silent:
        error(f"Server did not start on port {port} within 15 seconds")
        console.print("  [dim]Try starting it manually:  contextcore serve[/dim]")
        console.print("  [dim]If it still fails, diagnose with:[/dim] [bold]contextcore doctor[/bold]")
    return False


def _start_background(port: int = DEFAULT_PORT) -> bool:
    """Launch uvicorn as a detached background process."""
    sdk_root = get_sdk_root()
    config_path = get_default_config()

    env = build_runtime_env({"CONTEXTCORE_CONFIG": str(config_path)})

    usage = get_port_usage(port)
    if usage.get("in_use") and not usage.get("is_contextcore"):
        _print_port_conflict(port)
        return False

    cmd = build_background_server_command(port)

    try:
        kwargs = {
            "cwd": str(sdk_root),
            "env": env,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }

        # Windows: use CREATE_NEW_PROCESS_GROUP + DETACHED_PROCESS + CREATE_NO_WINDOW
        # This keeps the server alive after the terminal closes AND prevents a new
        # console window from appearing.
        if platform.system() == "Windows":
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            DETACHED_PROCESS         = 0x00000008
            CREATE_NO_WINDOW         = 0x08000000
            kwargs["creationflags"] = CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS | CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True

        proc = subprocess.Popen(cmd, **kwargs)
        _write_pid_file(proc.pid)

        return True
    except Exception as e:
        error(f"Failed to start server: {e}")
        console.print("  [dim]Retry in a new terminal:[/dim] [bold]contextcore serve[/bold]")
        return False


def stop_server(port: int = DEFAULT_PORT) -> bool:
    """Stop the background server if we have its PID."""
    pid: int | None = None
    try:
        if _PID_FILE.exists():
            pid = int(_PID_FILE.read_text().strip())
    except Exception:
        pid = None

    if pid is None:
        if not is_server_running(port):
            _PID_FILE.unlink(missing_ok=True)
            return False
        pid = _find_pid_by_port(port)
        if pid is None:
            _PID_FILE.unlink(missing_ok=True)
            return False

    try:
        if not stop_pid(pid):
            raise RuntimeError("failed to stop process")
        _PID_FILE.unlink(missing_ok=True)
        return True
    except Exception:
        _PID_FILE.unlink(missing_ok=True)
        return False
