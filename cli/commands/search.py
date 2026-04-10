# cli/commands/search.py

import os
import sys
import subprocess
import webbrowser
import requests
import asyncio
from pathlib import Path

from textual import on #for ui improvements
from textual.app import App, ComposeResult
from textual.widgets import Input, OptionList, Select
from textual.widgets.option_list import Option
from textual.containers import Vertical, Horizontal
from textual.binding import Binding
from textual.events import Key
from rich.console import Console

from cli.constants import DEFAULT_PORT

API_BASE = f"http://127.0.0.1:{DEFAULT_PORT}" #hardcoded as everything for local depends here will move to config in phase 2
console = Console()

# search function that calls the API and returns results in a unified format
def _search_api(query: str, top_k: int = 20) -> list:
    print(f"[_search_api] Starting search for: {query}")
    print(f"[_search_api] API_BASE: {API_BASE}")
    try:
        r = requests.get(
            f"{API_BASE}/search",
            params={"query": query, "top_k": top_k, "modality": "all"},
            timeout=30,
        )
        print(f"[_search_api] Response status: {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            print(f"[_search_api] Response keys: {data.keys()}")
            all_results = []
            for modality in ["text", "image", "video", "audio"]:
                results = data.get(modality, {}).get("results", [])
                print(f"[_search_api] {modality.capitalize()} results count: {len(results)}")
                all_results.extend(results)
            # Sort by score descending
            all_results.sort(key=lambda x: float(x.get("score", 0.0)), reverse=True)
            return all_results[:top_k]
        else:
            print(f"[_search_api] Error: {r.text}")
    except Exception as e:
        print(f"[_search_api] Exception: {e}")
    return []

#search app using textual - handles user input, displays results, and opens files/links ui is mandated from it
class SearchApp(App):
    CSS = """
Screen {
    align: center middle; 
}

#main-container {
    width: 80;
    height: auto;
}

#search-bar {
    width: 100%;
    height: auto;
    margin-bottom: 1;
}

Input {
    width: 70%;
}

#top-k-select {
    width: 30%;
}

#results-container {
    width: 80;
    height: auto;
}

#local-results, #cloud-results {
    width: 50%;
    height: auto;
    max-height: 20;
    border: solid $primary;
    background: $surface;
}

OptionList > .option-list--option {
    padding: 0 1;
}

OptionList:focus > .option-list--option-highlighted {
    background: $primary;
    color: $text;
    text-style: bold;
}
"""
    #initialization of the app with search and open functions passed in for better separation of concerns and testability
    def __init__(self, search_fn, open_fn):
        super().__init__()
        self._search_fn = search_fn
        self._open_fn = open_fn
        self.DEBUG = True
        self.current_query = ""
        self._search_task = None
    
    BINDINGS = [
        Binding("escape", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit"),
        Binding("down", "focus_dropdown", "Focus Dropdown", show=False),
    ]

    active_input_id = None
    # This method defines the layout of the app using Textual's Compose system.
    # It creates a search bar with an input and a select for top-k, and two option lists for local and cloud results.
    def compose(self) -> ComposeResult:
        with Vertical(id="main-container"):
            with Horizontal(id="search-bar"):
                yield Input(placeholder="Search files...", id="search-input")
                yield Select(
                    id="top-k-select",
                    options=[("Top 2", 2), ("Top 6", 6), ("Top 10", 10)],
                    value=6,
                    prompt="Results"
                )
        
        with Horizontal(id="results-container"):
            yield OptionList(id="local-results")
            yield OptionList(id="cloud-results")
    # This method is called when the app is mounted.
    # It initializes the local and cloud results option lists with placeholder text.
    def on_mount(self) -> None:
        self.log(">>> App mounted")
        local_results = self.query_one("#local-results", OptionList)
        cloud_results = self.query_one("#cloud-results", OptionList)
        local_results.add_option(Option(prompt="[dim]Local results will appear here[/dim]", id=""))
        cloud_results.add_option(Option(prompt="[dim]Cloud results will appear here[/dim]", id=""))
    # This method handles the event when the user submits a search query (e.g., presses Enter).
    @on(Input.Submitted)
    def on_search_submit(self, event: Input.Submitted) -> None:
        self.log(f">>> Submit event: {repr(event.value)}")
        query = event.value.strip()
        if not query:
            return
        
        self.current_query = query
        if len(query) >= 3:
            self._perform_search(query)
    # This method handles the event when the user changes the search input.
    @on(Input.Changed)
    def on_search_input_changed(self, event: Input.Changed) -> None:
        query = event.value.strip()
        self.current_query = query
        
        # Cancel previous search task if it exists
        if self._search_task is not None and not self._search_task.done():
            self._search_task.cancel()
            self._search_task = None
        
        # Only trigger search if query has at least 3 characters
        if len(query) < 3:
            # Clear results if query is too short
            local_results = self.query_one("#local-results", OptionList)
            cloud_results = self.query_one("#cloud-results", OptionList)
            local_results.clear_options()
            cloud_results.clear_options()
            local_results.add_option(Option(prompt="[dim]Enter at least 3 characters to search[/dim]", id=""))
            cloud_results.add_option(Option(prompt="[dim]Enter at least 3 characters to search[/dim]", id=""))
            return
        
        # Create debounced search task (900ms)
        self._search_task = asyncio.create_task(self._debounced_search(query))
    # This method implements a debounce mechanism to avoid performing a search on every keystroke.
    async def _debounced_search(self, query: str) -> None:
        try:
            # Wait 500ms before searching
            await asyncio.sleep(0.5)
            # Only search if this is still the current query
            if query == self.current_query:
                self._perform_search(query)
        except asyncio.CancelledError:
            pass  # Task was cancelled, which is fine
    # This method handles the event when the user changes the top-k selection.
    @on(Select.Changed)
    def on_top_k_changed(self, event: Select.Changed) -> None:
        if self.current_query and len(self.current_query) >= 3:
            self._perform_search(self.current_query)
    # This method performs the actual search by calling the provided search function,
    # then updates the local and cloud results option lists based on the returned results.
    # values hardcoded are by design as the search function is expected to return unified results with
    # scores and paths for both local and cloud items, and the UI is built around that expectation.
    # This keeps the UI logic simpler and more focused on display rather than data manipulation.
    def _perform_search(self, query: str) -> None:
        local_results = self.query_one("#local-results", OptionList)
        cloud_results = self.query_one("#cloud-results", OptionList)
        top_k_select = self.query_one("#top-k-select", Select)
        top_k = top_k_select.value or 6
        
        local_results.clear_options()
        cloud_results.clear_options()
        local_results.add_option(Option(prompt="[dim]Searching...[/dim]", id=""))
        cloud_results.add_option(Option(prompt="[dim]Searching...[/dim]", id=""))
        self.log(">>> Calling search...")
        
        results = self._search_fn(query, top_k=top_k)
        self.log(f">>> Got {len(results)} results")
        
        # Separate local and cloud results
        local_list = [r for r in results if not r.get("cloud_url")]
        cloud_list = [r for r in results if r.get("cloud_url")]
        
        local_results.clear_options()
        cloud_results.clear_options()
        
        if local_list:
            self.log(f">>> Adding {len(local_list)} local results")
            for r in local_list:
                path = r.get("path") or r.get("video_path") or ""
                filename = r.get("filename") or (Path(path).name if path else "Unknown")
                score = r.get("score", 0.0)
                display_text = f"[bold]{filename}[/bold] [dim]{score:.2f}[/dim]"
                local_results.add_option(Option(prompt=display_text, id=path))
            local_results.highlighted = 0  # Highlight first
        else:
            local_results.add_option(Option(prompt="[dim]No local results[/dim]", id=""))
        
        if cloud_list:
            self.log(f">>> Adding {len(cloud_list)} cloud results")
            for r in cloud_list:
                path = r.get("path") or r.get("video_path") or ""
                filename = r.get("filename") or (Path(path).name if path else "Unknown")
                score = r.get("score", 0.0)
                display_text = f"[bold]{filename}[/bold] [dim]{score:.2f}[/dim]"
                cloud_results.add_option(Option(prompt=display_text, id=path))
            if not local_list:
                cloud_results.highlighted = 0  # Highlight first if no local
        else:
            cloud_results.add_option(Option(prompt="[dim]No cloud results[/dim]", id=""))
    # This method is triggered by the "Focus Dropdown" binding (e.g., pressing the down arrow key).
    def action_focus_dropdown(self) -> None:
        local_results = self.query_one("#local-results", OptionList)
        if local_results.display and local_results.option_count > 0:
            local_results.focus()
        else:
            cloud_results = self.query_one("#cloud-results", OptionList)
            if cloud_results.display and cloud_results.option_count > 0:
                cloud_results.focus()
    # This method handles the event when the user selects an option from either the local or cloud results.
    @on(OptionList.OptionSelected)
    def handle_selection(self, event: OptionList.OptionSelected) -> None:
        local_results = self.query_one("#local-results", OptionList)
        cloud_results = self.query_one("#cloud-results", OptionList)
        
        if self.active_input_id:
            active_input = self.query_one(f"#{self.active_input_id}", Input)
            
            with active_input.prevent(Input.Changed):
                active_input.value = str(event.option.prompt)
            
            local_results.display = False
            cloud_results.display = False
            
            path = event.option.id
            if path:
                filename = path.split("\\")[-1].split("/")[-1]
                self._open_fn({"path": path, "filename": filename})
            
            active_input.focus()
    # This method handles key presses to navigate between the search bar and results.
    def _on_key(self, event: Key) -> None:
        """Handle key presses to navigate between search bar and results."""
        if event.key == "up":
            try:
                local_results = self.query_one("#local-results", OptionList)
                cloud_results = self.query_one("#cloud-results", OptionList)
                search_input = self.query_one("#search-input", Input)
                
                # Check if focused on local-results and at top
                if self.focused == local_results and local_results.highlighted == 0:
                    search_input.focus()
                    return
                
                # Check if focused on cloud-results and at top
                if self.focused == cloud_results and cloud_results.highlighted == 0:
                    search_input.focus()
                    return
            except Exception:
                pass

# This method is responsible for opening the selected item, whether it's a local file or a cloud link.
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

    if sys.platform == "win32":
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.run(["open", path], check=True)
    else:
        subprocess.run(["xdg-open", path], check=True)
    console.print(f"[green]Opened:[/green] {path}")

# This function checks if the ContextCore server is running, and if not, it prompts the user to start it.
# Then it initializes and runs the SearchApp.
def run_search():
    from cli.server import is_server_running, ensure_server

    ensure_server(port=DEFAULT_PORT, silent=True)
    if not is_server_running(DEFAULT_PORT):
        console.print("[red]Error:[/red] ContextCore server not running.")
        console.print("  Start it with: [bold]contextcore serve[/bold]")
        return

    app = SearchApp(search_fn=_search_api, open_fn=_open_item)
    app.run()


if __name__ == "__main__":
    run_search()