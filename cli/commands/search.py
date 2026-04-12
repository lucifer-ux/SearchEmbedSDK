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
from textual.widgets import Input, OptionList, Select, Button
from textual.widgets.option_list import Option
from textual.containers import Vertical, Horizontal
from textual.binding import Binding
from textual.events import Key
from rich.console import Console

from cli.constants import DEFAULT_PORT

API_BASE = f"http://127.0.0.1:{DEFAULT_PORT}" #hardcoded as everything for local depends here will move to config in phase 2
console = Console()

# search function that calls the API and returns results in a unified format
def _search_api(query: str, top_k: int = 20) -> dict:
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
            results_by_modality = {}
            for modality in ["text", "image", "video", "audio"]:
                results = data.get(modality, {}).get("results", [])
                print(f"[_search_api] {modality.capitalize()} results count: {len(results)}")
                # Sort by score descending
                results.sort(key=lambda x: float(x.get("score", 0.0)), reverse=True)
                results_by_modality[modality] = results[:top_k]
            return results_by_modality
        else:
            print(f"[_search_api] Error: {r.text}")
    except Exception as e:
        print(f"[_search_api] Exception: {e}")
    return {}

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

#modality-buttons {
    width: 80;
    height: auto;
    margin-bottom: 1;
}

Button {
    margin-right: 1;
}

#results-list {
    width: 80;
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
        # TODO: Remove unused DEBUG flag or make logging conditional
        # self.DEBUG = True
        self.current_query = ""
        self._search_task = None
        self.current_modality = "text"
        self.results_by_modality = {}
        self.current_results = []
    
    BINDINGS = [
        Binding("escape", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit"),
        Binding("down", "focus_dropdown", "Focus Dropdown", show=False),
    ]

    active_input_id = None
    # This method defines the layout of the app using Textual's Compose system.
    # It creates a search bar, modality buttons, and a single results list.
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
        
            with Horizontal(id="modality-buttons"):
                yield Button("Text", id="text-btn", variant="primary")
                yield Button("Image", id="image-btn")
                yield Button("Video", id="video-btn")
                yield Button("Audio", id="audio-btn")
        
            yield OptionList(id="results-list")
    # This method is called when the app is mounted.
    # It initializes the results list with placeholder text.
    def on_mount(self) -> None:
        self.log(">>> App mounted")
        results_list = self.query_one("#results-list", OptionList)
        results_list.add_option(Option(prompt="[dim]Enter at least 3 characters to search[/dim]", id=""))
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
            results_list = self.query_one("#results-list", OptionList)
            results_list.clear_options()
            results_list.add_option(Option(prompt="[dim]Enter at least 3 characters to search[/dim]", id=""))
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
    # stores results by modality, and displays the current modality's results.
    def _perform_search(self, query: str) -> None:
        top_k_select = self.query_one("#top-k-select", Select)
        top_k = top_k_select.value or 6
        
        results_list = self.query_one("#results-list", OptionList)
        results_list.clear_options()
        results_list.add_option(Option(prompt="[dim]Searching...[/dim]", id=""))
        
        self.log(">>> Calling search...")
        
        self.results_by_modality = self._search_fn(query, top_k=top_k)
        self.log(f">>> Got results for modalities: {list(self.results_by_modality.keys())}")
        
        self._display_current_modality_results()
    
    # This method displays the results for the currently selected modality.
    def _display_current_modality_results(self) -> None:
        results_list = self.query_one("#results-list", OptionList)
        results_list.clear_options()
        
        results = self.results_by_modality.get(self.current_modality, [])
        self.current_results = results  # Store for selection
        if results:
            self.log(f">>> Displaying {len(results)} {self.current_modality} results")
            scores = [abs(float(r.get("score", 0.0))) for r in results]
            max_score = max(scores) if scores else 0.0
            for i, r in enumerate(results):
                path = r.get("path") or r.get("video_path") or ""
                filename = r.get("filename") or (Path(path).name if path else "Unknown")
                raw_score = abs(float(r.get("score", 0.0)))
                cloud_tag = " [cloud]" if r.get("cloud_url") else ""

                normalized = raw_score / max_score if max_score > 0 else 0.0
                if max_score == 0.0:
                    if i == 0:
                        relevance_tag = " [green]strong[/green]"
                    elif i < 3:
                        relevance_tag = " [yellow]medium[/yellow]"
                    else:
                        relevance_tag = ""
                elif normalized >= 0.75:
                    relevance_tag = " [green]strong[/green]"
                elif normalized >= 0.4:
                    relevance_tag = " [yellow]medium[/yellow]"
                else:
                    relevance_tag = ""

                if self.current_modality == "audio" and max_score > 0:
                    score_display = f"{normalized * 100:.0f}%"
                else:
                    score_display = f"{raw_score:.2f}" if raw_score >= 0.01 else f"{raw_score:.2e}"
                display_text = f"[bold]{filename}[/bold]{cloud_tag}{relevance_tag} [dim]{score_display}[/dim]"
                results_list.add_option(Option(prompt=display_text, id=str(i)))
            results_list.highlighted = 0  # Highlight first
        else:
            results_list.add_option(Option(prompt=f"[dim]No {self.current_modality} results[/dim]", id=""))
    
    # This method handles button presses for modality selection.
    @on(Button.Pressed)
    def on_modality_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "text-btn":
            self.current_modality = "text"
        elif button_id == "image-btn":
            self.current_modality = "image"
        elif button_id == "video-btn":
            self.current_modality = "video"
        elif button_id == "audio-btn":
            self.current_modality = "audio"
        
        # Update button variants
        for btn_id in ["text-btn", "image-btn", "video-btn", "audio-btn"]:
            btn = self.query_one(f"#{btn_id}", Button)
            if btn_id == button_id:
                btn.variant = "primary"
            else:
                btn.variant = "default"
        
        self._display_current_modality_results()
    # This method is triggered by the "Focus Dropdown" binding (e.g., pressing the down arrow key).
    def action_focus_dropdown(self) -> None:
        results_list = self.query_one("#results-list", OptionList)
        if results_list.display and results_list.option_count > 0:
            results_list.focus()
    # This method handles the event when the user selects an option from the results.
    @on(OptionList.OptionSelected)
    def handle_selection(self, event: OptionList.OptionSelected) -> None:
        self.log(f">>> Selection event: {event.option.id}")
        try:
            index = int(event.option.id)
            if 0 <= index < len(self.current_results):
                selected_item = self.current_results[index]
                self.log(f">>> Opening item: {selected_item.get('filename', 'unknown')}")
                self._open_fn(selected_item)
            else:
                self.log(f">>> Invalid index: {index}, current_results length: {len(self.current_results)}")
        except ValueError as e:
            self.log(f">>> ValueError parsing id '{event.option.id}': {e}")
        except Exception as e:
            self.log(f">>> Unexpected error in handle_selection: {e}")
    # This method handles key presses to navigate between the search bar and results.
    def _on_key(self, event: Key) -> None:
        """Handle key presses to navigate between search bar and results."""
        if event.key == "up":
            try:
                search_input = self.query_one("#search-input", Input)
                results_list = self.query_one("#results-list", OptionList)
                # Check if focused on results and at top
                if self.focused == results_list and results_list.highlighted == 0:
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