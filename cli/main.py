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
from cli.strings import (
    APP_HELP,
    APP_NAME,
    DEFAULT_BENCHMARK_DATASET,
    DEFAULT_BENCHMARK_SYSTEMS,
    DEFAULT_BENCHMARK_TOKEN_ENCODING,
    HELP_ADD_FOLDER_NO_INDEX,
    HELP_ADD_FOLDER_PATH,
    HELP_BENCHMARK_CONTEXT_TOP_K,
    HELP_BENCHMARK_DATASET,
    HELP_BENCHMARK_DATASETS_DIR,
    HELP_BENCHMARK_MAX_QUERIES,
    HELP_BENCHMARK_MEASURE_TOKENS,
    HELP_BENCHMARK_OUTPUT_JSON,
    HELP_BENCHMARK_REPORT_CSV,
    HELP_BENCHMARK_REPORT_MD,
    HELP_BENCHMARK_SYSTEMS,
    HELP_BENCHMARK_TOKEN_ENCODING,
    HELP_BENCHMARK_TOP_K,
    HELP_ENABLE_RELOAD,
    HELP_INDEX_TARGET,
    HELP_INSTALL_MODEL,
    HELP_PORT_BIND,
    HELP_PORT_LISTENING,
    HELP_REGISTER_TOOL,
    HELP_REPORT_MESSAGE,
    HELP_REPORT_REPO,
    HELP_REPORT_TITLE,
    HELP_SERVER_ACTION,
    HELP_UNINSTALL_DRY_RUN,
    HELP_UNINSTALL_PURGE_MODEL_CACHE,
    HELP_UNINSTALL_REMOVE_PACKAGE,
    HELP_UNINSTALL_YES,
    HELP_UPDATE_RESTART,
)

app = typer.Typer(
    name=APP_NAME,
    help=APP_HELP,
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
    port: int = typer.Option(DEFAULT_PORT, help=HELP_PORT_LISTENING),
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
        help=HELP_INDEX_TARGET,
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
    path: str = typer.Argument(..., help=HELP_ADD_FOLDER_PATH),
    no_index: bool = typer.Option(False, "--no-index", help=HELP_ADD_FOLDER_NO_INDEX),
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
        help=HELP_INSTALL_MODEL,
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
        help=HELP_REGISTER_TOOL,
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
def benchmark(
    dataset: str = typer.Option(DEFAULT_BENCHMARK_DATASET, "--dataset", help=HELP_BENCHMARK_DATASET),
    top_k: int = typer.Option(10, "--top-k", min=1, help=HELP_BENCHMARK_TOP_K),
    max_queries: int = typer.Option(0, "--max-queries", min=0, help=HELP_BENCHMARK_MAX_QUERIES),
    datasets_dir: str = typer.Option("", "--datasets-dir", help=HELP_BENCHMARK_DATASETS_DIR),
    output_json: str = typer.Option("", "--output-json", help=HELP_BENCHMARK_OUTPUT_JSON),
    measure_tokens: bool = typer.Option(False, "--measure-tokens", help=HELP_BENCHMARK_MEASURE_TOKENS),
    token_encoding: str = typer.Option(DEFAULT_BENCHMARK_TOKEN_ENCODING, "--token-encoding", help=HELP_BENCHMARK_TOKEN_ENCODING),
    context_top_k: int = typer.Option(0, "--context-top-k", min=0, help=HELP_BENCHMARK_CONTEXT_TOP_K),
    systems: str = typer.Option(
        DEFAULT_BENCHMARK_SYSTEMS,
        "--systems",
        help=HELP_BENCHMARK_SYSTEMS,
    ),
    report_csv: str = typer.Option("", "--report-csv", help=HELP_BENCHMARK_REPORT_CSV),
    report_md: str = typer.Option("", "--report-md", help=HELP_BENCHMARK_REPORT_MD),
):
    """
    [bold]Benchmark ContextCore retrieval on a BEIR dataset.[/bold]

    Quick-start:
      contextcore benchmark --dataset scifact
    """
    from cli.commands.benchmark import run_benchmark

    run_benchmark(
        dataset=dataset.strip() or DEFAULT_BENCHMARK_DATASET,
        top_k=top_k,
        max_queries=max_queries,
        datasets_dir=datasets_dir.strip() or None,
        output_json=output_json.strip() or None,
        measure_tokens=bool(measure_tokens),
        token_encoding=token_encoding.strip() or DEFAULT_BENCHMARK_TOKEN_ENCODING,
        context_top_k=(context_top_k if context_top_k > 0 else None),
        systems=systems.strip() or DEFAULT_BENCHMARK_SYSTEMS,
        report_csv=report_csv.strip() or None,
        report_md=report_md.strip() or None,
    )


@app.command()
def report(
    message: list[str] = typer.Argument(
        None,
        help=HELP_REPORT_MESSAGE,
    ),
    repo: str = typer.Option(
        "",
        "--repo",
        help=HELP_REPORT_REPO,
    ),
    title: str = typer.Option(
        "",
        "--title",
        help=HELP_REPORT_TITLE,
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
        help=HELP_UPDATE_RESTART,
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
    port:   int  = typer.Option(DEFAULT_PORT, help=HELP_PORT_BIND),
    reload: bool = typer.Option(False,  help=HELP_ENABLE_RELOAD),
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
    action: str = typer.Argument(..., help=HELP_SERVER_ACTION),
    port: int = typer.Option(DEFAULT_PORT, help=HELP_PORT_LISTENING),
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
    port: int = typer.Option(DEFAULT_PORT, help=HELP_PORT_LISTENING),
):
    """
    [bold]Start the ContextCore background server.[/bold]
    """
    from cli.commands.helpers import run_server
    run_server(action="start", port=port)


@app.command(name="stop")
def stop_cmd(
    port: int = typer.Option(DEFAULT_PORT, help=HELP_PORT_LISTENING),
):
    """
    [bold]Stop the ContextCore background server.[/bold]
    """
    from cli.commands.helpers import run_server
    run_server(action="stop", port=port)


@app.command(name="restart")
def restart_cmd(
    port: int = typer.Option(DEFAULT_PORT, help=HELP_PORT_LISTENING),
):
    """
    [bold]Restart the ContextCore background server.[/bold]
    """
    from cli.commands.helpers import run_server
    run_server(action="restart", port=port)


@app.command(name="uninstall")
def uninstall_cmd(
    yes: bool = typer.Option(False, "--yes", help=HELP_UNINSTALL_YES),
    dry_run: bool = typer.Option(False, "--dry-run", help=HELP_UNINSTALL_DRY_RUN),
    remove_package: bool = typer.Option(
        True,
        "--remove-package/--no-remove-package",
        help=HELP_UNINSTALL_REMOVE_PACKAGE,
    ),
    purge_model_cache: bool = typer.Option(
        True,
        "--purge-model-cache/--keep-model-cache",
        help=HELP_UNINSTALL_PURGE_MODEL_CACHE,
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
    yes: bool = typer.Option(False, "--yes", help=HELP_UNINSTALL_YES),
    dry_run: bool = typer.Option(False, "--dry-run", help=HELP_UNINSTALL_DRY_RUN),
    remove_package: bool = typer.Option(
        True,
        "--remove-package/--no-remove-package",
        help=HELP_UNINSTALL_REMOVE_PACKAGE,
    ),
    purge_model_cache: bool = typer.Option(
        True,
        "--purge-model-cache/--keep-model-cache",
        help=HELP_UNINSTALL_PURGE_MODEL_CACHE,
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

