import subprocess
import shutil
from pathlib import Path
import yaml

from cli.ui import console, error, header, hint, section, success, warning

CONFIG_PATH = Path.home() / ".contextcore" / "contextcore.yaml"

# -----------------------------
# RCLONE PATH (reuse logic)
# -----------------------------
def get_rclone_path():
    system = shutil.which("rclone")
    if system:
        return system

    winget_base = Path.home() / "AppData/Local/Microsoft/WinGet/Packages"
    if winget_base.exists():
        for p in winget_base.rglob("rclone.exe"):
            return str(p)

    known = [
        Path("C:/Program Files/rclone/rclone.exe"),
        Path("C:/Program Files (x86)/rclone/rclone.exe"),
    ]

    for p in known:
        if p.exists():
            return str(p)

    raise RuntimeError("rclone not found")


def run_rclone_command(args):
    result = subprocess.run(
        [get_rclone_path()] + args,
        capture_output=True,
        text=True
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return result.stdout.strip()


def list_remotes():
    output = run_rclone_command(["listremotes"])
    return output.splitlines() if output else []


# -----------------------------
# CONFIG UPDATE
# -----------------------------
def remove_remote_from_config(remote: str):
    if not CONFIG_PATH.exists():
        return

    config = yaml.safe_load(CONFIG_PATH.read_text()) or {}

    if "storage" in config and "cloud" in config["storage"]:
        if config["storage"]["cloud"].get("remote") == remote.rstrip(":"):
            del config["storage"]["cloud"]

    CONFIG_PATH.write_text(yaml.dump(config))


# -----------------------------
# MAIN COMMAND
# -----------------------------
def run_cloud_disconnect():
    header("Disconnect Cloud Storage")

    try:
        remotes = list_remotes()
    except Exception as e:
        error(f"Failed to fetch remotes: {e}")
        return

    if not remotes:
        warning("No connected cloud remotes found")
        return

    section("Connected Remotes")

    for i, r in enumerate(remotes):
        console.print(f"{i + 1}. {r}")

    choice = console.input("Select remote to disconnect: ")

    try:
        selected = remotes[int(choice) - 1]
    except:
        error("Invalid selection")
        return

    console.print(f"\nYou are about to remove: [bold]{selected}[/bold]")
    confirm = console.input("Type 'yes' to confirm: ")

    if confirm.lower() != "yes":
        warning("Cancelled")
        return

    # -----------------------------
    # REMOVE FROM RCLONE
    # -----------------------------
    try:
        run_rclone_command(["config", "delete", selected.rstrip(":")])
    except Exception as e:
        error(f"Failed to remove remote: {e}")
        return

    # -----------------------------
    # REMOVE FROM CONFIG
    # -----------------------------
    remove_remote_from_config(selected)

    success(f"{selected} disconnected successfully")