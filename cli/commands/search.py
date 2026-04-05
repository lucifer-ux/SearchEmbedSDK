# cli/commands/search.py

import os
import sys
import subprocess
import webbrowser
import requests
from pathlib import Path

import questionary
from rich.table import Table
from cli.constants import DEFAULT_PORT
from cli.ui import console

API_BASE = f"http://127.0.0.1:{DEFAULT_PORT}"


def run_search():
    from cli.server import is_server_running, ensure_server

    ensure_server(port=DEFAULT_PORT, silent=True)
    if not is_server_running(DEFAULT_PORT):
        console.print("[red]Error:[/red] ContextCore server not running.")
        console.print("  Start it with: [bold]contextcore serve[/bold]")
        return

    while True:
        query = questionary.text(
            "Search (or 'q' to quit):",
            qmark=">",
        ).ask()

        if not query or query.strip().lower() in ("q", "quit", "exit"):
            break

        results = _search_api(query.strip())
        if not results:
            console.print("[yellow]No results found.[/yellow]")
            continue

        all_items = _display_results(results)

        if not all_items:
            continue

        choice = questionary.text(
            "Enter number to open (or 'n' new search, 'q' quit):",
            qmark=">",
        ).ask()

        if not choice:
            continue

        choice = choice.strip().lower()
        if choice in ("q", "quit", "exit"):
            break
        if choice in ("n", "new"):
            continue

        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(all_items):
                _open_item(all_items[idx])
            else:
                console.print("[red]Invalid selection.[/red]")
        else:
            console.print("[red]Invalid input. Enter a number, 'n' for new search, or 'q' to quit.[/red]")

    console.print("[dim]Goodbye![/dim]")


def _search_api(query: str, top_k: int = 20) -> dict:
    try:
        r = requests.get(
            f"{API_BASE}/search",
            params={"query": query, "top_k": top_k, "modality": "all"},
            timeout=30,
        )
        if r.status_code == 200:
            return r.json()
        else:
            console.print(f"[red]Search error:[/red] {r.status_code}")
    except requests.exceptions.ConnectionError:
        console.print(f"[red]Cannot connect to server at[/red] {API_BASE}")
    except Exception as e:
        console.print(f"[red]Search failed:[/red] {e}")
    return {}


def _display_results(results: dict) -> list:
    all_items = []
    skip_keys = {"query", "modality"}
    category_names = {
        "text": "Text",
        "image": "Images",
        "video": "Videos",
        "audio": "Audio",
    }

    for category, data in results.items():
        if category in skip_keys:
            continue

        if not isinstance(data, dict):
            continue

        items = data.get("results", [])
        if not items:
            continue

        items = sorted(items, key=lambda x: x.get("score", 0), reverse=True)

        display_name = category_names.get(category, category.capitalize())
        console.print(f"\n[bold cyan]{display_name} ({len(items)})[/bold cyan]")

        if category == "text":
            local_items = []
            cloud_items = []
            for item in items[:40]:
                path = item.get("path", "")
                filename = item.get("filename", Path(path).name if path else "Unknown")
                score = item.get("score", 0)
                cloud_url = item.get("cloud_url")
                source = (item.get("source") or "").lower().strip()
                item_category = (item.get("category") or "").lower().strip()
                row = {
                    "path": path,
                    "filename": filename,
                    "category": category,
                    "score": score,
                    "cloud_url": cloud_url,
                    "source": source,
                }
                if source == "cloud" or item_category == "cloud_text":
                    cloud_items.append(row)
                else:
                    local_items.append(row)

            local_items = local_items[:20]
            cloud_items = cloud_items[:20]

            table = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 2))
            table.add_column(f"Local Text ({len(local_items)})")
            table.add_column(f"Cloud Text ({len(cloud_items)})")

            row_count = max(len(local_items), len(cloud_items), 1)
            for i in range(row_count):
                left_cell = ""
                right_cell = ""

                if i < len(local_items):
                    l = local_items[i]
                    all_items.append(l)
                    idx = len(all_items)
                    left_cell = f"[bold]{idx}.[/bold] {l['filename']}\n[dim]{l['path']}[/dim]\n[dim]Score: {float(l['score']):.2f}[/dim]"

                if i < len(cloud_items):
                    c = cloud_items[i]
                    all_items.append(c)
                    idx = len(all_items)
                    right_cell = f"[bold]{idx}.[/bold] {c['filename']}\n[dim]{c['path']}[/dim]\n[dim]Score: {float(c['score']):.2f}[/dim]"
                    if c.get("cloud_url"):
                        right_cell += f"\n[dim]Cloud Link: {c['cloud_url']}[/dim]"

                table.add_row(left_cell, right_cell)

            console.print(table)
            continue

        for item in items[:20]:
            if category == "video":
                path = item.get("video_path", "")
            else:
                path = item.get("path", "")

            filename = item.get("filename", Path(path).name if path else "Unknown")
            score = item.get("score", 0)
            cloud_url = item.get("cloud_url")
            source = item.get("source")

            all_items.append(
                {
                    "path": path,
                    "filename": filename,
                    "category": category,
                    "score": score,
                    "cloud_url": cloud_url,
                    "source": source,
                }
            )
            idx = len(all_items)

            console.print(f"  [bold]{idx}.[/bold] {filename}")
            console.print(f"      [dim]{path}[/dim]")
            console.print(f"      [dim]Score: {score:.2f}[/dim]")
            if cloud_url:
                console.print(f"      [dim]Cloud Link: {cloud_url}[/dim]")

    return all_items


def _open_item(item: dict):
    path = item.get("path", "")
    cloud_url = item.get("cloud_url", "")
    source = (item.get("source") or "").lower().strip()
    category = (item.get("category") or "").lower().strip()

    if source == "cloud" or category == "cloud_text":
        if cloud_url:
            try:
                webbrowser.open(cloud_url)
                console.print(f"[green]Opened cloud link:[/green] {cloud_url}")
            except Exception as e:
                console.print(f"[red]Failed to open cloud link:[/red] {e}")
            return
        console.print("[yellow]No direct cloud link available for this file.[/yellow]")
        console.print(f"[dim]Cloud path:[/dim] {path}")
        return

    if not path:
        console.print("[red]No path found for this item.[/red]")
        return

    filepath = Path(path)
    if not filepath.exists():
        console.print(f"[red]File not found:[/red] {path}")
        return

    action = questionary.select(
        "Open file or folder?",
        choices=[
            "Open file",
            "Open containing folder",
        ],
    ).ask()

    try:
        if action == "Open file":
            if sys.platform == "win32":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.run(["open", path], check=True)
            else:
                subprocess.run(["xdg-open", path], check=True)
            console.print(f"[green]Opened:[/green] {path}")
        elif action == "Open containing folder":
            folder = filepath.parent
            if sys.platform == "win32":
                os.startfile(folder)
            elif sys.platform == "darwin":
                subprocess.run(["open", str(folder)], check=True)
            else:
                subprocess.run(["xdg-open", str(folder)], check=True)
            console.print(f"[green]Opened folder:[/green] {folder}")
    except Exception as e:
        console.print(f"[red]Failed to open:[/red] {e}")
