from __future__ import annotations

import os
import sys

from rich.console import Console
from rich.panel import Panel
from rich.theme import Theme

_THEME_PRESETS = {
    "dark": {
        "info": "bold #63B3ED",
        "success": "bold #48BB78",
        "warning": "bold #F6AD55",
        "error": "bold #FC8181",
        "dim": "#A0AEC0",
        "header": "bold #E2E8F0",
        "title": "bold #90CDF4",
        "panel_border": "#4A5568",
        "section_border": "#2D3748",
        "muted": "#A0AEC0",
    },
    "light": {
        "info": "bold #8A5A00",
        "success": "bold #2F6B3A",
        "warning": "bold #B66A00",
        "error": "bold #A63B28",
        "dim": "#5A4A2A",
        "header": "bold #4D3B1A",
        "title": "bold #7A5200",
        "panel_border": "#D5BE85",
        "section_border": "#C7AA65",
        "muted": "#6B5830",
    },
}


def _resolve_theme_name(raw: str | None) -> str:
    candidate = (raw or "").strip().lower()
    return candidate if candidate in _THEME_PRESETS else "dark"


def _theme_from_config() -> str | None:
    try:
        from config import get_config

        raw = get_config().get("ui_theme")
        return str(raw) if raw else None
    except Exception:
        return None


def _resolve_initial_theme_name() -> str:
    env_theme = os.getenv("CONTEXTCORE_UI_THEME") or os.getenv("CONTEXTCORE_THEME")
    return _resolve_theme_name(env_theme or _theme_from_config())


_ACTIVE_THEME_NAME = _resolve_initial_theme_name()


def _build_theme(theme_name: str) -> Theme:
    preset = _THEME_PRESETS[theme_name]
    return Theme(
        {
            "info": preset["info"],
            "success": preset["success"],
            "warning": preset["warning"],
            "error": preset["error"],
            "dim": preset["dim"],
            "header": preset["header"],
            "title": preset["title"],
            "panel_border": preset["panel_border"],
            "section_border": preset["section_border"],
            "muted": preset["muted"],
        }
    )


console = Console(theme=_build_theme(_ACTIVE_THEME_NAME))
_HAS_PUSHED_THEME = False

_ENCODING = (getattr(sys.stdout, "encoding", None) or "").lower()
_UNICODE_OK = "utf" in _ENCODING or _ENCODING in {"cp65001"}
_SYM_SUCCESS = "\u2713" if _UNICODE_OK else "[OK]"
_SYM_WARNING = "\u26a0" if _UNICODE_OK else "[!]"
_SYM_ERROR = "\u2717" if _UNICODE_OK else "[X]"
_SYM_INFO = "\u2022" if _UNICODE_OK else "-"
_TITLE_SUFFIX = " \u2726" if _UNICODE_OK else ""


def get_theme_name() -> str:
    return _ACTIVE_THEME_NAME


def set_theme(theme_name: str) -> str:
    global _ACTIVE_THEME_NAME, _HAS_PUSHED_THEME

    resolved = _resolve_theme_name(theme_name)
    if _HAS_PUSHED_THEME:
        try:
            console.pop_theme()
        except Exception:
            pass

    console.push_theme(_build_theme(resolved))
    _HAS_PUSHED_THEME = True
    _ACTIVE_THEME_NAME = resolved
    return resolved


def header(title: str = f"ContextCore{_TITLE_SUFFIX}") -> None:
    subtitle = "Local-first multimodal retrieval"
    footer = f"[muted]Theme: {_ACTIVE_THEME_NAME}[/muted]"
    console.print()
    console.print(
        Panel(
            f"[muted]{subtitle}[/muted]",
            title=f"[title]{title}[/title]",
            subtitle=footer,
            border_style="panel_border",
            padding=(0, 2),
        )
    )
    console.print()


def section(title: str, description: str = "") -> None:
    console.print()
    body = f"[muted]{description}[/muted]" if description else ""
    console.print(
        Panel(
            body,
            title=f"[header]{title}[/header]",
            border_style="section_border",
            padding=(0, 2),
        )
    )


def success(msg: str) -> None:
    console.print(f"  [success]{_SYM_SUCCESS}[/success] {msg}")


def warning(msg: str) -> None:
    console.print(f"  [warning]{_SYM_WARNING}[/warning] {msg}")


def error(msg: str) -> None:
    console.print(f"  [error]{_SYM_ERROR}[/error] {msg}")


def info(msg: str) -> None:
    console.print(f"  [info]{_SYM_INFO}[/info] {msg}")


def hint(label: str, cmd: str) -> None:
    console.print(f"\n    Fix: [bold yellow]{cmd}[/bold yellow]  ({label})\n")


def done_panel(lines: list[str]) -> None:
    body = "\n".join(f"  {line}" for line in lines)
    panel_title = f"[bold green]Setup complete{_TITLE_SUFFIX}[/bold green]"
    console.print()
    console.print(Panel(body, title=panel_title, border_style="green", padding=(1, 2)))
    console.print()
