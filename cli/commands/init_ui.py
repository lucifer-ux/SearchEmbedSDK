from __future__ import annotations

from typing import Literal, Sequence

from textual import on
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import (
    Button,
    Checkbox,
    Header,
    Input,
    RadioButton,
    RadioSet,
    Static,
)


PromptOption = tuple[str, str]
PromptMultiOption = tuple[str, str, bool]


_BASE_CSS = """
Screen {
    layout: vertical;
}

#main-container {
    width: 1fr;
    height: auto;
    align: center middle;
    margin: 1 2;
    padding: 1 2;
    border: round $primary;
}

#content {
    width: 1fr;
    height: auto;
}

#multi-select-actions {
    width: 1fr;
    height: auto;
    margin: 1 0 0 0;
}

.toolbar-spacer {
    width: 1fr;
}

#title {
    margin: 0 0 1 0;
    content-align: center middle;
    text-style: bold;
}

Checkbox {
    width: 1fr;
    margin: 0 0 1 0;
    padding: 0 1;
    color: $text;
}

Checkbox:focus {
    background: $primary 20%;
    color: $text;
    text-style: bold;
    border: round $primary;
}

Checkbox.checked-option {
    color: #48bb78;
    text-style: bold;
}

Checkbox.checked-option:focus {
    color: #68d391;
}

#hint {
    margin: 1 0 0 0;
    content-align: center middle;
}

#continue-button {
    width: 24;
    margin: 1 0 0 0;
    content-align: center middle;
}

.warm-light {
    background: #fff9ea;
    color: #3f3322;
}

.warm-light #main-container {
    background: #fff2c8;
    border: round #c59b2e;
}

.warm-light #hint {
    color: #685735;
}

.warm-light Checkbox:focus {
    background: #f4e4b2;
    color: #3f3322;
    border: round #c59b2e;
}

.warm-light Checkbox.checked-option {
    color: #2f6b3a;
}

.warm-light Checkbox.checked-option:focus {
    color: #24552e;
}
"""


class _BasePromptApp(App[object]):
    CSS = _BASE_CSS
    BINDINGS = [
        ("enter", "confirm", "Confirm"),
        ("escape", "cancel", "Cancel"),
        ("ctrl+c", "cancel", "Cancel"),
        ("q", "cancel", "Cancel"),
    ]

    def __init__(
        self,
        prompt: str,
        setup_theme: Literal["dark", "light"] = "dark",
    ) -> None:
        super().__init__()
        self.prompt = prompt
        self.setup_theme = setup_theme

    def on_mount(self) -> None:
        self._set_setup_theme(self.setup_theme)

    def _set_setup_theme(
        self,
        theme_name: Literal["dark", "light"] | str,
    ) -> None:
        resolved_theme: Literal["dark", "light"] = (
            "light" if theme_name == "light" else "dark"
        )
        self.setup_theme = resolved_theme
        self.dark = resolved_theme != "light"
        if resolved_theme == "light":
            self.screen.add_class("warm-light")
        else:
            self.screen.remove_class("warm-light")

    def action_cancel(self) -> None:
        self.exit(None)


class _SingleSelectApp(_BasePromptApp):
    def __init__(
        self,
        prompt: str,
        options: Sequence[PromptOption],
        default_value: str | None,
        setup_theme: Literal["dark", "light"],
        show_continue_button: bool = False,
    ) -> None:
        super().__init__(prompt, setup_theme)
        self.options = list(options)
        self.default_value = default_value
        self.show_continue_button = show_continue_button
        self._radio_set: RadioSet | None = None
        self._continue_button: Button | None = None
        valid_values = {value for _, value in self.options}
        if default_value in valid_values:
            self._selected_value = default_value
        elif self.options:
            self._selected_value = self.options[0][1]
        else:
            self._selected_value = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Container(id="main-container"):
            with Vertical(id="content"):
                yield Static(self.prompt, id="title")
                with RadioSet(id="radio-set") as radio_set:
                    for label, _ in self.options:
                        yield RadioButton(label)
                self._radio_set = radio_set
                if self.show_continue_button:
                    self._continue_button = Button(
                        "Continue",
                        id="continue-button",
                        variant="primary",
                    )
                    yield self._continue_button
                    yield Static(
                        "Choose a theme, then press Enter or click Continue",
                        id="hint",
                    )
                else:
                    yield Static(
                        "Use arrow keys, then press Enter to continue",
                        id="hint",
                    )

    def on_mount(self) -> None:
        super().on_mount()
        self._radio_set = self.query_one("#radio-set", RadioSet)
        if self.show_continue_button:
            self._continue_button = self.query_one("#continue-button", Button)
        if not self._radio_set:
            return
        selected_index = 0
        if self.default_value:
            for idx, (_, value) in enumerate(self.options):
                if value == self.default_value:
                    selected_index = idx
                    break
        button = self._radio_set.query(RadioButton)[selected_index]
        button.value = True
        self._selected_value = self.options[selected_index][1]
        self._radio_set.focus()
        if self._selected_value in {"dark", "light"}:
            self._set_setup_theme(self._selected_value)

    def _focus_selected_radio(self) -> None:
        if not self._radio_set:
            return
        self._radio_set.focus()

    @on(Button.Pressed, "#continue-button")
    def on_continue_pressed(self, _: Button.Pressed) -> None:
        self.action_confirm()

    def on_key(self, event) -> None:
        if event.key == "enter" and not self.show_continue_button:
            if isinstance(self.focused, RadioButton) or isinstance(
                self.focused,
                RadioSet,
            ):
                self.action_confirm()
                event.stop()
                return

        if event.key == "down":
            if (
                self.show_continue_button
                and self._radio_set is not None
                and self._continue_button is not None
            ):
                buttons = list(self._radio_set.query(RadioButton))
                if buttons:
                    in_radio_group = bool(
                        getattr(self._radio_set, "has_focus", False)
                        or getattr(self._radio_set, "has_focus_within", False)
                        or isinstance(self.focused, RadioButton)
                    )
                    if in_radio_group:
                        self._continue_button.focus()
                        event.stop()
        elif event.key == "up":
            if isinstance(self.focused, Button):
                self._focus_selected_radio()
                event.stop()

    @on(RadioSet.Changed)
    def on_radio_set_changed(self, _: RadioSet.Changed) -> None:
        if not self._radio_set:
            return
        for idx, button in enumerate(self._radio_set.query(RadioButton)):
            if button.value:
                self._selected_value = self.options[idx][1]
                if self._selected_value in {"dark", "light"}:
                    self._set_setup_theme(self._selected_value)
                break

    def action_confirm(self) -> None:
        if self._selected_value is not None:
            self.exit(self._selected_value)
            return
        if not self._radio_set:
            self.exit(None)
            return
        for idx, button in enumerate(self._radio_set.query(RadioButton)):
            if button.value:
                self.exit(self.options[idx][1])
                return
        self.exit(None)


class _MultiSelectApp(_BasePromptApp):
    def __init__(
        self,
        prompt: str,
        options: Sequence[PromptMultiOption],
        setup_theme: Literal["dark", "light"],
    ) -> None:
        super().__init__(prompt, setup_theme)
        self.options = list(options)
        self._checkboxes: list[Checkbox] = []
        self._continue_button: Button | None = None
        self._hint: Static | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Container(id="main-container"):
            with Vertical(id="content"):
                yield Static(self.prompt, id="title")
                for label, _, checked in self.options:
                    checkbox = Checkbox(label, value=checked)
                    self._checkboxes.append(checkbox)
                    yield checkbox
                with Horizontal(id="multi-select-actions"):
                    yield Static("", classes="toolbar-spacer")
                    self._continue_button = Button(
                        "Enter",
                        id="multi-continue-button",
                        variant="primary",
                    )
                    yield self._continue_button
                self._hint = Static("", id="hint")
                yield self._hint

    def on_mount(self) -> None:
        super().on_mount()
        self._continue_button = self.query_one("#multi-continue-button", Button)
        self._sync_checkbox_styles()
        if self._checkboxes:
            self._checkboxes[0].focus()
        self._update_hint()

    def _focus_last_checkbox(self) -> None:
        if self._checkboxes:
            self._checkboxes[-1].focus()

    def _update_hint(self) -> None:
        if not self._hint:
            return
        selected_count = sum(1 for checkbox in self._checkboxes if checkbox.value)
        suffix = "" if selected_count == 1 else "s"
        self._hint.update(
            f"{selected_count} item{suffix} selected · Use arrow keys + Space "
            "to toggle, then Enter or click the button"
        )

    def _sync_checkbox_styles(self) -> None:
        for checkbox in self._checkboxes:
            if checkbox.value:
                checkbox.add_class("checked-option")
            else:
                checkbox.remove_class("checked-option")

    @on(Checkbox.Changed)
    def on_checkbox_changed(self, _: Checkbox.Changed) -> None:
        self._sync_checkbox_styles()
        self._update_hint()

    @on(Button.Pressed, "#multi-continue-button")
    def on_continue_pressed(self, _: Button.Pressed) -> None:
        self.action_confirm()

    def on_key(self, event) -> None:
        focused = self.focused
        if event.key in {"down", "up"} and isinstance(focused, Checkbox):
            try:
                current_index = self._checkboxes.index(focused)
            except ValueError:
                current_index = -1

            if current_index >= 0:
                if event.key == "down":
                    if current_index < len(self._checkboxes) - 1:
                        self._checkboxes[current_index + 1].focus()
                    elif self._continue_button is not None:
                        self._continue_button.focus()
                    event.stop()
                elif event.key == "up" and current_index > 0:
                    self._checkboxes[current_index - 1].focus()
                    event.stop()
        elif event.key == "up":
            if isinstance(self.focused, Button):
                self._focus_last_checkbox()
                event.stop()

    def action_confirm(self) -> None:
        selected: list[str] = []
        for idx, checkbox in enumerate(self._checkboxes):
            if checkbox.value:
                selected.append(self.options[idx][1])
        self.exit(selected)


class _TextInputApp(_BasePromptApp):
    def __init__(
        self,
        prompt: str,
        default_value: str,
        placeholder: str,
        setup_theme: Literal["dark", "light"],
    ) -> None:
        super().__init__(prompt, setup_theme)
        self.default_value = default_value
        self.placeholder = placeholder
        self._input: Input | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Container(id="main-container"):
            with Vertical(id="content"):
                yield Static(self.prompt, id="title")
                self._input = Input(
                    value=self.default_value,
                    placeholder=self.placeholder,
                )
                yield self._input
                yield Static("Type value and press Enter to confirm", id="hint")

    def on_mount(self) -> None:
        super().on_mount()
        self._input = self.query_one(Input)
        if self._input:
            self._input.focus()

    @on(Input.Submitted)
    def on_input_submitted(self, _: Input.Submitted) -> None:
        self.action_confirm()

    def action_confirm(self) -> None:
        self.exit(self._input.value if self._input else None)


def show_init_welcome(
    default_theme: Literal["dark", "light"] = "dark",
) -> Literal["dark", "light"]:
    options: list[PromptOption] = [
        ("Dark (recommended)", "dark"),
        ("Light (warm)", "light"),
    ]
    selected = show_single_select(
        "Welcome to ContextCore\nSelect your setup theme",
        options,
        default=default_theme,
        theme=default_theme,
        show_continue_button=True,
    )
    if selected in {"dark", "light"}:
        return selected
    raise KeyboardInterrupt


def show_single_select(
    prompt: str,
    options: Sequence[PromptOption],
    default: str | None = None,
    theme: Literal["dark", "light"] = "dark",
    show_continue_button: bool = False,
) -> str | None:
    app = _SingleSelectApp(
        prompt,
        options,
        default,
        theme,
        show_continue_button=show_continue_button,
    )
    result = app.run()
    return str(result) if isinstance(result, str) else None


def show_multi_select(
    prompt: str,
    options: Sequence[PromptMultiOption],
    theme: Literal["dark", "light"] = "dark",
) -> list[str] | None:
    app = _MultiSelectApp(prompt, options, theme)
    result = app.run()
    if isinstance(result, list):
        return [str(v) for v in result]
    return None


def show_text_input(
    prompt: str,
    default: str = "",
    placeholder: str = "",
    theme: Literal["dark", "light"] = "dark",
) -> str | None:
    app = _TextInputApp(prompt, default, placeholder, theme)
    result = app.run()
    return str(result) if isinstance(result, str) else None
