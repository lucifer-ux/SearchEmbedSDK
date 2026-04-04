# cli/main.py
#
# contextcore â€” the main CLI entrypoint.
# All 8 commands are registered here and dispatched to their modules.
#
# Install as a command:
#   pip install -e .       (picks up pyproject.toml entry_points)
# Or run directly:
#   python -m cli.main init

from __future__ import annotations
from typing import Optional
import typer
from cli.constants import DEFAULT_PORT

app = typer.Typer(
    name="contextcore",
    help="ContextCore â€” unified local search for Claude and other AI tools.",
    add_completion=False,
    rich_markup_mode="rich",
    no_args_is_help=True,
)


@app.command()
def init():
    """
    [bold]First-time setup wizard.[/bold]

    Walks you through directory selection, modality setup, model downloads,
    and auto-registration with Claude Desktop / Cline / Cursor.
    """
    from cli.commands.init import run_init
    run_init()


@app.command()
def status(
    port: int = typer.Option(DEFAULT_PORT, help="Port the server is listening on."),
):
    """
    [bold]Show server health and index progress.[/bold]

    Displays which modalities are indexed, file counts, and server status.
    """
    from cli.commands.status import run_status
    run_status(port=port)



@app.command(name="index")
def index_cmd(
    target: Optional[str] = typer.Argument(
        None,
        help="Optional directory to index. Omit to scan all configured directories.",
    ),
):
    """
    [bold]Scan for new or updated files and index them now.[/bold]

    Examples:
      contextcore index
      contextcore index ~/Downloads
      contextcore index "C:/Users/Me/Videos"
    """
    from cli.commands.helpers import run_index
    run_index(target=target)


@app.command()
def search():
    """
    [bold]Interactive search across all modalities.[/bold]

    Search for files by content (text, images, videos, audio).
    Enter a query, view results grouped by type, and open files directly.
    """
    from cli.commands.search import run_search
    run_search()


@app.command("add-folder")
def add_folder_cmd(
    path: str = typer.Argument(..., help="Directory to add to the watch list."),
    no_index: bool = typer.Option(False, "--no-index", help="Add the folder without indexing it immediately."),
):
    """
    [bold]Add a new folder to ContextCore after setup.[/bold]

    Updates the config and, by default, indexes the new folder immediately.
    """
    from cli.commands.helpers import run_add_folder
    run_add_folder(path=path, index_now=not no_index)


@app.command()
def install(
    model: str = typer.Argument(
        ...,
        help="Which model to download: [bold]clip[/bold], [bold]audio[/bold], or [bold]all[/bold].",
    ),
):
    """
    [bold]Download optional ML models.[/bold]

    Heavy models (torch, whisper) are not installed by default to keep
    the initial setup fast. Use this command once you need them.

    Examples:
      contextcore install clip     # image + video search
      contextcore install audio    # audio + meeting transcription
      contextcore install all      # everything
    """
    from cli.commands.helpers import run_install
    run_install(model=model)


@app.command()
def register(
    tool: str = typer.Argument(
        ...,
        help="Tool to register with. Options: claude-desktop, claude-code, cline, cursor.",
    ),
):
    """
    [bold]Add ContextCore to an AI tool's MCP config.[/bold]

    Run this after installing Claude Desktop, Cline, or Cursor for the first
    time, or if you skipped the step during  [bold]contextcore init[/bold].

    Examples:
      contextcore register claude-desktop
      contextcore register cline
      contextcore register cursor
    """
    from cli.commands.helpers import run_register
    run_register(tool=tool)


@app.command()
def report(
    message: list[str] = typer.Argument(
        None,
        help="Issue description. Example: contextcore report image search returns empty results",
    ),
    repo: str = typer.Option(
        "",
        "--repo",
        help="Optional GitHub repo override (owner/repo). Default: detected from git origin.",
    ),
    title: str = typer.Option(
        "",
        "--title",
        help="Optional issue title override.",
    ),
):
    """
    [bold]Report an issue to GitHub from the CLI.[/bold]

    Examples:
      contextcore report image search returns wrong file
      contextcore report --title "MCP issue" "reveal_file opens wrong folder"
      contextcore report --repo lucifer-ux/SearchEmbedSDK "setup failed on macOS"
    """
    from cli.commands.report import run_report

    joined = " ".join(message).strip() if message else ""
    run_report(message=joined, repo=(repo or "").strip() or None, title=(title or "").strip() or None)


@app.command()
def update(
    restart: bool = typer.Option(
        True,
        "--restart/--no-restart",
        help="Restart ContextCore background server after pulling updates.",
    ),
):
    """
    [bold]Pull latest ContextCore fixes from GitHub.[/bold]

    Uses the sdk_root saved during [bold]contextcore init[/bold], so this works
    from any current directory.
    """
    from cli.commands.update import run_update

    run_update(restart_server=restart)

@app.command()
def doctor():
    """
    [bold]Diagnose problems with your ContextCore setup.[/bold]

    Checks Python version, SQLite, config file, MCP server, FastAPI server,
    Claude Desktop config, and optional ML models.
    Every failure includes a specific [bold]Fix:[/bold] command.
    """
    from cli.commands.doctor import run_doctor
    run_doctor()

@app.command()
def serve(
    port:   int  = typer.Option(DEFAULT_PORT, help="Port to bind the server to."),
    reload: bool = typer.Option(False,  help="Enable hot-reload for development."),
):
    """
    [bold]Start the ContextCore FastAPI server.[/bold]

    This is usually started automatically by  [bold]contextcore init[/bold].
    Use this command to start it manually, or after a reboot.
    """
    from cli.commands.helpers import run_serve
    run_serve(port=port, reload=reload)


@app.command(name="server")
def server_cmd(
    action: str = typer.Argument(..., help="Action to run: start, stop, restart, status."),
    port: int = typer.Option(DEFAULT_PORT, help="Port the server is listening on."),
):
    """
    [bold]Manage the ContextCore background server.[/bold]

    Use this command to start, stop, restart, or check server state.

    Examples:
      contextcore server start
      contextcore server stop
      contextcore server restart
      contextcore server status
    """
    from cli.commands.helpers import run_server
    run_server(action=action, port=port)


@app.command(name="start")
def start_cmd(
    port: int = typer.Option(DEFAULT_PORT, help="Port the server is listening on."),
):
    """
    [bold]Start the ContextCore background server.[/bold]
    """
    from cli.commands.helpers import run_server
    run_server(action="start", port=port)


@app.command(name="stop")
def stop_cmd(
    port: int = typer.Option(DEFAULT_PORT, help="Port the server is listening on."),
):
    """
    [bold]Stop the ContextCore background server.[/bold]
    """
    from cli.commands.helpers import run_server
    run_server(action="stop", port=port)


@app.command(name="restart")
def restart_cmd(
    port: int = typer.Option(DEFAULT_PORT, help="Port the server is listening on."),
):
    """
    [bold]Restart the ContextCore background server.[/bold]
    """
    from cli.commands.helpers import run_server
    run_server(action="restart", port=port)


@app.command(name="uninstall")
def uninstall_cmd(
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation prompt."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be removed."),
    remove_package: bool = typer.Option(
        True,
        "--remove-package/--no-remove-package",
        help="Also run pip uninstall for contextcore after cleanup.",
    ),
    purge_model_cache: bool = typer.Option(
        True,
        "--purge-model-cache/--keep-model-cache",
        help="Also remove local Hugging Face/Torch model cache folders.",
    ),
):
    """
    [bold]Remove ContextCore from this machine.[/bold]

    Stops server + autostart, unregisters MCP entries, and deletes local
    ContextCore config/index state.
    """
    from cli.commands.helpers import run_uninstall

    run_uninstall(
        yes=yes,
        dry_run=dry_run,
        remove_package=remove_package,
        purge_model_cache=purge_model_cache,
    )


@app.command(name="remove")
def remove_cmd(
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation prompt."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be removed."),
    remove_package: bool = typer.Option(
        True,
        "--remove-package/--no-remove-package",
        help="Also run pip uninstall for contextcore after cleanup.",
    ),
    purge_model_cache: bool = typer.Option(
        True,
        "--purge-model-cache/--keep-model-cache",
        help="Also remove local Hugging Face/Torch model cache folders.",
    ),
):
    """
    [bold]Alias for contextcore uninstall.[/bold]
    """
    from cli.commands.helpers import run_uninstall

    run_uninstall(
        yes=yes,
        dry_run=dry_run,
        remove_package=remove_package,
        purge_model_cache=purge_model_cache,
    )

@app.command(name="cloudconnect")
def cloud_connect_cmd():
    """
    [bold]Connect cloud storage services to ContextCore.[/bold]

    Use rclone to link Google Drive, OneDrive, Dropbox, Box, S3, and WebDAV
    accounts to ContextCore for unified search across local and cloud files.

    Example:
      contextcore cloudconnect
    """
    from cli.commands.cloudconnect import run_cloud_connect
    run_cloud_connect()

@app.command(name="clouddisconnect")
def cloud_disconnect_cmd():
    """
    [bold]Disconnect cloud storage services from ContextCore.[/bold]

    Use rclone to unlink Google Drive, OneDrive, Dropbox, Box, S3, and WebDAV
    accounts from ContextCore.

    Example:
      contextcore clouddisconnect
    """
    from cli.commands.clouddisconnect import run_cloud_disconnect
    run_cloud_disconnect()

def main() -> None:
    app()


if __name__ == "__main__":
    main()

