# cli/commands/cloudconnect.py

import subprocess
import shutil
import platform
from pathlib import Path
import yaml

from cli.ui import console, error, header, hint, section, success, warning


PROVIDERS = [
    ("drive", "Google Drive"),
    ("onedrive", "OneDrive"),
    ("dropbox", "Dropbox"),
    ("box", "Box"),
    ("s3", "Amazon S3"),
    ("webdav", "WebDAV"),
]

CONFIG_PATH = Path.home() / ".contextcore" / "contextcore.yaml"


# -----------------------------
# RCLONE RESOLUTION (FIXED)
# -----------------------------
def get_rclone_path():
    # 1. Try PATH
    system = shutil.which("rclone")
    if system:
        return system

    # 2. Winget install detection (YOUR CASE)
    winget_base = Path.home() / "AppData/Local/Microsoft/WinGet/Packages"
    if winget_base.exists():
        for p in winget_base.rglob("rclone.exe"):
            return str(p)

    # 3. Common install paths
    known_paths = [
        Path("C:/Program Files/rclone/rclone.exe"),
        Path("C:/Program Files (x86)/rclone/rclone.exe"),
    ]

    for p in known_paths:
        if p.exists():
            return str(p)

    raise RuntimeError("rclone not found")


def is_rclone_available():
    try:
        get_rclone_path()
        return True
    except:
        return False


# -----------------------------
# RCLONE COMMAND WRAPPER
# -----------------------------
def run_rclone_command(args):
    try:
        result = subprocess.run(
            [get_rclone_path()] + args,
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"rclone error: {e.stderr.strip()}")


def list_remotes():
    output = run_rclone_command(["listremotes"])
    return output.splitlines() if output else []


def create_remote(name: str, provider: str):
    subprocess.run([get_rclone_path(), "config", "create", name, provider])


def test_remote(remote: str):
    try:
        run_rclone_command(["lsf", remote, "--max-depth", "1"])
        return True
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        return False


# -----------------------------
# CONFIG SAVE
# -----------------------------
def _save_remote(remote: str):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

    config = {}
    if CONFIG_PATH.exists():
        config = yaml.safe_load(CONFIG_PATH.read_text()) or {}

    config.setdefault("storage", {})
    config["storage"]["cloud"] = {"remote": remote}

    CONFIG_PATH.write_text(yaml.dump(config))


# -----------------------------
# INSTALL (IMPROVED)
# -----------------------------
def install_rclone():
    os_name = platform.system().lower()

    try:
        if os_name == "windows":
            result = subprocess.run(
                ["winget", "install", "-e", "--id", "Rclone.Rclone"],
                capture_output=True,
                text=True
            )
        elif os_name == "darwin":
            result = subprocess.run(["brew", "install", "rclone"], capture_output=True, text=True)
        elif os_name == "linux":
            result = subprocess.run(["sudo", "apt", "install", "-y", "rclone"], capture_output=True, text=True)
        else:
            error("Unsupported OS")
            return False

        if result.returncode != 0:
            console.print(result.stderr)
            return False

        return True

    except Exception as e:
        error(f"Install failed: {e}")
        return False


# -----------------------------
# PROVIDER SELECTION
# -----------------------------
def select_providers():
    section("Select Cloud Provider")

    for i, (_, name) in enumerate(PROVIDERS):
        console.print(f"{i + 1}. {name}")

    choice = console.input("Enter number: ")

    try:
        index = int(choice) - 1
        if 0 <= index < len(PROVIDERS):
            return PROVIDERS[index]
    except:
        pass

    error("Invalid selection")
    return None


# -----------------------------
# MAIN COMMAND
# -----------------------------
def run_cloud_connect():
    header("Cloud Storage Connection")

    section("Checking rclone")

    if not is_rclone_available():
        warning("rclone not found")

        section("Installing rclone")

        if not install_rclone():
            error("Automatic installation failed")
            hint("manual install", "https://rclone.org/downloads/")
            return

        # Try again WITHOUT requiring restart
        if not is_rclone_available():
            error("rclone installed but not accessible")
            hint("restart terminal", "or rerun command")
            return

        success("rclone installed successfully")

    path = get_rclone_path()
    success(f"Using rclone: {path}")

    remotes = list_remotes()

    # -----------------------------
    # NO REMOTES
    # -----------------------------
    if not remotes:
        section("No cloud remotes found")

        provider = select_providers()
        if not provider:
            return

        key, label = provider
        remote_name = f"contextcore_{key}"

        console.print("\nOpening browser for authentication...")
        console.print("Complete login and return here.\n")

        create_remote(remote_name, key)

        remote_path = f"{remote_name}:"

        section(f"Testing {label}")

        if not test_remote(remote_path):
            error("Connection failed")
            return

        _save_remote(remote_name)
        success(f"{label} connected successfully")
        return

    # -----------------------------
    # EXISTING REMOTES
    # -----------------------------
    section("Available Remotes")

    for i, r in enumerate(remotes):
        console.print(f"{i + 1}. {r}")

    choice = console.input("Select remote: ")

    try:
        selected = remotes[int(choice) - 1]
    except:
        error("Invalid selection")
        return

    section("Verifying connection")

    if not test_remote(selected):
        error("Remote not accessible")
        return

    _save_remote(selected.rstrip(":"))
    success(f"Connected to {selected}")