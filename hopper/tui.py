# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""TUI for managing coding agents using Textual."""

import os
import re
from dataclasses import dataclass
from pathlib import Path

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Key
from textual.highlight import highlight
from textual.reactive import reactive
from textual.screen import ModalScreen, Screen
from textual.theme import Theme
from textual.widgets import (
    Button,
    DataTable,
    DirectoryTree,
    Footer,
    Header,
    OptionList,
    Static,
    TextArea,
)
from textual.widgets.option_list import Option

from hopper.backlog import BacklogItem
from hopper.claude import spawn_claude, switch_to_pane
from hopper.git import get_diff_stat
from hopper.lodes import (
    format_age,
    format_uptime,
    get_lode_dir,
)
from hopper.projects import Project, find_project, touch_project

# Claude Code-inspired theme
# Colors derived from Claude Code's terminal UI (ANSI bright colors)
CLAUDE_THEME = Theme(
    name="claude",
    primary="#ff5555",  # Bright red - logo accent, primary branding
    secondary="#5555ff",  # Bright blue - code/identifiers
    accent="#ff55ff",  # Bright magenta - hints, prompts
    foreground="#ffffff",  # Bright white - main text
    background="#000000",  # Black - main background
    surface="#1a1a1a",  # Very dark gray - widget backgrounds
    panel="#262626",  # Dark gray - differentiated sections
    success="#55ff55",  # Bright green - completed/running
    warning="#ffff55",  # Bright yellow - activity/processing
    error="#ff5555",  # Bright red - errors
    dark=True,
    variables={
        "footer-key-foreground": "#ff55ff",  # Magenta for key hints
        "footer-description-foreground": "#888888",  # Gray for descriptions
    },
)

# Status indicators (Unicode symbols, no emoji)
STATUS_RUNNING = "●"  # filled circle
STATUS_STUCK = "◐"  # half-filled circle
STATUS_NEW = "○"  # empty circle
STATUS_ERROR = "✗"  # x mark
STATUS_SHIPPED = "✓"
STATUS_DISCONNECTED = "⊘"  # circled division slash — runner not connected

# Hint row keys (always present at bottom of each table)
HINT_LODE = "_hint_lode"
HINT_BACKLOG = "_hint_backlog"

AUTO_ON = "↻"
AUTO_OFF = "·"

# Status -> color mapping (shared by icon and text formatting)
STATUS_COLORS = {
    STATUS_RUNNING: "bright_green",
    STATUS_STUCK: "bright_yellow",
    STATUS_ERROR: "bright_red",
    STATUS_NEW: "bright_black",
    STATUS_SHIPPED: "bright_green",
    STATUS_DISCONNECTED: "bright_red",
}

STAGE_ORDER = {"mill": 0, "refine": 1, "ship": 2, "shipped": 3}


@dataclass
class Row:
    """A row in a table."""

    id: str
    stage: str  # "mill", "refine", "ship", or "shipped"
    age: str  # formatted age string
    last: str  # formatted time since last mutation
    # STATUS_RUNNING, STATUS_STUCK, STATUS_NEW, STATUS_ERROR, STATUS_SHIPPED, STATUS_DISCONNECTED
    status: str
    auto: bool = True  # Whether auto-advance is enabled
    project: str = ""  # Project name
    title: str = ""  # Short human-readable lode title
    status_text: str = ""  # Human-readable status text


def lode_to_row(lode: dict) -> Row:
    """Convert a lode dict to a display row."""
    state = lode.get("state", "new")
    if lode.get("stage") == "shipped":
        status = STATUS_SHIPPED
    elif state == "new":
        status = STATUS_NEW
    elif state == "error":
        status = STATUS_ERROR
    elif state == "stuck":
        status = STATUS_STUCK
    else:
        status = STATUS_RUNNING

    stage = lode.get("stage", "mill")
    if not lode.get("active", False) and stage != "shipped":
        status = STATUS_DISCONNECTED

    return Row(
        id=lode["id"],
        stage=stage,
        age=format_age(lode["created_at"]),
        last=format_age(lode.get("updated_at", lode["created_at"])),
        status=status,
        auto=lode.get("auto", False),
        project=lode.get("project", ""),
        title=lode.get("title", ""),
        status_text=lode.get("status", ""),
    )


def format_status_text(status: str) -> Text:
    """Format a status icon with color using Rich Text."""
    return Text(status, style=STATUS_COLORS.get(status, ""))


def strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from text."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def format_status_label(label: str, status: str) -> Text:
    """Format status text with color matching the status icon."""
    cleaned = strip_ansi(label.replace("\n", " ")) if label else ""
    return Text(cleaned, style=STATUS_COLORS.get(status, ""))


def format_auto_text(auto: bool) -> Text:
    """Format an auto-advance indicator with color using Rich Text."""
    if auto:
        return Text(AUTO_ON, style="bright_green")
    return Text(AUTO_OFF, style="bright_black")


def format_stage_text(stage: str) -> Text:
    """Format a stage indicator with color using Rich Text."""
    if stage == "mill":
        return Text(stage, style="bright_blue")
    elif stage == "refine":
        return Text(stage, style="bright_yellow")
    elif stage == "ship":
        return Text(stage, style="bright_green")
    elif stage == "shipped":
        return Text(stage, style="bright_green")
    return Text(stage)


class ProjectPickerScreen(ModalScreen[Project | None]):
    """Modal screen for picking a project."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "select", "Select"),
    ]

    CSS = """
    ProjectPickerScreen {
        align: center middle;
        height: 100%;
    }

    #picker-container {
        width: 70;
        height: auto;
        max-height: 80%;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }

    #picker-title {
        text-align: center;
        text-style: bold;
        color: $text;
        padding-bottom: 1;
    }

    #project-list {
        height: auto;
        max-height: 20;
        background: $surface;
        border: none;
    }
    """

    def __init__(self, projects: list[Project], title: str = "Select Project"):
        super().__init__()
        self._projects = projects
        self._picker_title = title

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-container"):
            yield Static(self._picker_title, id="picker-title")
            options = [Option(p.name, id=p.name) for p in self._projects]
            yield OptionList(*options, id="project-list")

    def on_mount(self) -> None:
        self.query_one("#project-list", OptionList).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_select(self) -> None:
        option_list = self.query_one("#project-list", OptionList)
        if option_list.highlighted is not None:
            project = self._projects[option_list.highlighted]
            self.dismiss(project)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        project = self._projects[event.option_index]
        self.dismiss(project)


class TextInputScreen(ModalScreen):
    """Base modal screen with a title, TextArea, and action buttons.

    Subclasses must define MODAL_TITLE, compose_buttons(), and on_submit().
    """

    SCOPED_CSS = False
    MODAL_TITLE: str = ""

    def __init__(self, initial_text: str = ""):
        super().__init__()
        self._initial_text = initial_text

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    CSS = """
    TextInputScreen {
        align: center middle;
        height: 100%;
    }

    .text-input-container {
        width: 70;
        height: auto;
        max-height: 80%;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }

    .text-input-title {
        text-align: center;
        text-style: bold;
        color: $text;
        padding-bottom: 1;
    }

    .text-input-area {
        height: 10;
        margin-bottom: 1;
    }

    .text-input-buttons {
        height: auto;
        align: center middle;
    }

    .text-input-buttons Button {
        margin: 0 1;
    }

    .text-input-buttons Button:focus {
        text-style: bold reverse;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(classes="text-input-container"):
            yield Static(self.MODAL_TITLE, classes="text-input-title")
            yield TextArea(classes="text-input-area")
            with Horizontal(classes="text-input-buttons"):
                yield from self.compose_buttons()

    def compose_buttons(self) -> ComposeResult:
        """Yield the action buttons. Subclasses must override."""
        raise NotImplementedError

    def on_mount(self) -> None:
        ta = self.query_one(TextArea)
        if self._initial_text:
            ta.text = self._initial_text
        ta.focus()

    def on_key(self, event: events.Key) -> None:
        focused = self.focused
        buttons = list(self.query(".text-input-buttons Button"))

        if event.key == "ctrl+enter":
            event.prevent_default()
            event.stop()
            primary = next((b for b in buttons if b.variant == "primary"), None)
            if primary:
                self._try_submit(primary)
            return

        if event.key == "right" and focused in buttons:
            event.prevent_default()
            event.stop()
            idx = buttons.index(focused)
            buttons[(idx + 1) % len(buttons)].focus()
        elif event.key == "left" and focused in buttons:
            event.prevent_default()
            event.stop()
            idx = buttons.index(focused)
            buttons[(idx - 1) % len(buttons)].focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _get_text(self) -> str:
        """Get the stripped text from the TextArea."""
        return self.query_one(TextArea).text.strip()

    def _try_submit(self, button: Button) -> None:
        """Validate text and call on_submit for the given button."""
        text = self._get_text()
        if not text:
            self.notify("Please enter a description", severity="warning")
            return
        self.on_submit(button, text)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
            return
        self._try_submit(event.button)

    def on_submit(self, button: Button, text: str) -> None:
        """Handle a validated submit. Subclasses must override."""
        raise NotImplementedError


class ScopeInputScreen(TextInputScreen):
    """Modal screen for entering task scope — start a lode or add to backlog."""

    MODAL_TITLE = "Describe Task Scope"

    def __init__(self, project_name: str) -> None:
        super().__init__()
        self.MODAL_TITLE = f"Describe {project_name.capitalize()} Task Scope"

    def compose_buttons(self) -> ComposeResult:
        yield Button("Cancel", id="btn-cancel", variant="default")
        yield Button("Backlog", id="btn-backlog", variant="default")
        yield Button("Start", id="btn-start", variant="primary")

    def on_submit(self, button: Button, text: str) -> None:
        action = "start" if button.id == "btn-start" else "backlog"
        self.dismiss((text, action))


class BacklogInputScreen(TextInputScreen):
    """Modal screen for entering a backlog item description."""

    MODAL_TITLE = "Describe Backlog Item"

    def compose_buttons(self) -> ComposeResult:
        yield Button("Cancel", id="btn-cancel", variant="default")
        yield Button("Add", id="btn-add", variant="primary")

    def on_submit(self, button: Button, text: str) -> None:
        self.dismiss(text)


class BacklogEditScreen(TextInputScreen):
    """Modal screen for editing a backlog item with save/promote options."""

    MODAL_TITLE = "Edit Backlog Item"

    def compose_buttons(self) -> ComposeResult:
        yield Button("Cancel", id="btn-cancel", variant="default")
        yield Button("Promote", id="btn-promote", variant="default")
        yield Button("Save", id="btn-save", variant="primary")

    def on_submit(self, button: Button, text: str) -> None:
        action = "promote" if button.id == "btn-promote" else "save"
        self.dismiss((action, text))


class MillReviewScreen(TextInputScreen):
    """Modal screen for reviewing/editing the mill output before refine."""

    MODAL_TITLE = "Review Mill Output"

    def compose_buttons(self) -> ComposeResult:
        yield Button("Cancel", id="btn-cancel", variant="default")
        yield Button("Process", id="btn-process", variant="default")
        yield Button("Save", id="btn-save", variant="primary")

    def on_submit(self, button: Button, text: str) -> None:
        action = "process" if button.id == "btn-process" else "save"
        self.dismiss((action, text))


def format_diff_stat(diff_stat: str) -> Text:
    """Format diff stat output with colors (green +, red -)."""
    if not diff_stat:
        return Text("No changes", style="dim")

    text = Text()
    for line in diff_stat.split("\n"):
        if "|" in line:
            # File line: " filename | 10 +++++-----"
            parts = line.split("|")
            text.append(parts[0])
            text.append("|")
            if len(parts) > 1:
                stat_part = parts[1]
                for char in stat_part:
                    if char == "+":
                        text.append(char, style="bright_green")
                    elif char == "-":
                        text.append(char, style="bright_red")
                    else:
                        text.append(char)
            text.append("\n")
        else:
            # Summary line or other
            text.append(line + "\n")
    return text


class ShipReviewScreen(ModalScreen[str | None]):
    """Modal screen for reviewing changes before shipping."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    CSS = """
    ShipReviewScreen {
        align: center middle;
        height: 100%;
    }

    #ship-container {
        width: 80;
        height: auto;
        max-height: 80%;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }

    #ship-title {
        text-align: center;
        text-style: bold;
        color: $text;
        padding-bottom: 1;
    }

    #ship-diff {
        height: auto;
        max-height: 20;
        padding: 0 1;
        overflow-y: auto;
    }

    #ship-buttons {
        height: auto;
        align: center middle;
        padding-top: 1;
    }

    #ship-buttons Button {
        margin: 0 1;
    }

    #ship-buttons Button:focus {
        text-style: bold reverse;
    }
    """

    def __init__(self, diff_stat: str = ""):
        super().__init__()
        self._diff_stat = diff_stat

    def compose(self) -> ComposeResult:
        with Vertical(id="ship-container"):
            yield Static("Ship Review", id="ship-title")
            yield Static(self._format_diff(), id="ship-diff")
            with Horizontal(id="ship-buttons"):
                yield Button("Cancel", id="btn-cancel", variant="default")
                yield Button("Refine", id="btn-refine", variant="default")
                yield Button("Ship", id="btn-ship", variant="primary")

    def _format_diff(self) -> Text:
        return format_diff_stat(self._diff_stat)

    def on_mount(self) -> None:
        self.query_one("#btn-ship").focus()

    def on_key(self, event: events.Key) -> None:
        focused = self.focused
        buttons = list(self.query("#ship-buttons Button"))

        if event.key == "right" and focused in buttons:
            event.prevent_default()
            event.stop()
            idx = buttons.index(focused)
            buttons[(idx + 1) % len(buttons)].focus()
        elif event.key == "left" and focused in buttons:
            event.prevent_default()
            event.stop()
            idx = buttons.index(focused)
            buttons[(idx - 1) % len(buttons)].focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
        elif event.button.id == "btn-refine":
            self.dismiss("refine")
        elif event.button.id == "btn-ship":
            self.dismiss("ship")


class ArchiveConfirmScreen(ModalScreen[bool | None]):
    """Modal screen for confirming archive of a lode with unmerged changes."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    CSS = """
    ArchiveConfirmScreen {
        align: center middle;
        height: 100%;
    }

    #archive-container {
        width: 80;
        height: auto;
        max-height: 80%;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }

    #archive-title {
        text-align: center;
        text-style: bold;
        color: $text;
        padding-bottom: 1;
    }

    #archive-diff {
        height: auto;
        max-height: 20;
        padding: 0 1;
        overflow-y: auto;
    }

    #archive-buttons {
        height: auto;
        align: center middle;
        padding-top: 1;
    }

    #archive-buttons Button {
        margin: 0 1;
    }

    #archive-buttons Button:focus {
        text-style: bold reverse;
    }
    """

    def __init__(self, diff_stat: str, branch: str):
        super().__init__()
        self._diff_stat = diff_stat
        self._branch = branch

    def compose(self) -> ComposeResult:
        with Vertical(id="archive-container"):
            yield Static(
                f"Unmerged changes on {self._branch}",
                id="archive-title",
            )
            yield Static(format_diff_stat(self._diff_stat), id="archive-diff")
            with Horizontal(id="archive-buttons"):
                yield Button("Cancel", id="btn-cancel", variant="default")
                yield Button("Archive", id="btn-archive", variant="error")

    def on_mount(self) -> None:
        self.query_one("#btn-cancel").focus()

    def on_key(self, event: events.Key) -> None:
        focused = self.focused
        buttons = list(self.query("#archive-buttons Button"))

        if event.key == "right" and focused in buttons:
            event.prevent_default()
            event.stop()
            idx = buttons.index(focused)
            buttons[(idx + 1) % len(buttons)].focus()
        elif event.key == "left" and focused in buttons:
            event.prevent_default()
            event.stop()
            idx = buttons.index(focused)
            buttons[(idx - 1) % len(buttons)].focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
        elif event.button.id == "btn-archive":
            self.dismiss(True)


class ShippedReviewScreen(ModalScreen[bool | None]):
    """Modal screen for reviewing ship output and archiving."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    CSS = """
    ShippedReviewScreen {
        align: center middle;
        height: 100%;
    }

    #shipped-container {
        width: 80;
        height: auto;
        max-height: 80%;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }

    #shipped-title {
        text-align: center;
        text-style: bold;
        color: $text;
        margin-bottom: 1;
    }

    #shipped-content {
        height: auto;
        max-height: 70vh;
        padding: 0 1;
        overflow-y: auto;
    }

    #shipped-buttons {
        height: auto;
        align: center middle;
        padding-top: 1;
    }

    #shipped-buttons Button {
        margin: 0 1;
    }

    #shipped-buttons Button:focus {
        text-style: bold reverse;
    }
    """

    def __init__(self, content: str, lode_title: str):
        super().__init__()
        self._content = content
        self._lode_title = lode_title

    def compose(self) -> ComposeResult:
        title = "Ship Complete"
        if self._lode_title:
            title = f"Ship Complete - {self._lode_title}"
        with Vertical(id="shipped-container"):
            yield Static(title, id="shipped-title")
            yield Static(self._content, id="shipped-content")
            with Horizontal(id="shipped-buttons"):
                yield Button("Cancel", id="shipped-cancel", variant="default")
                yield Button("Archive", id="shipped-archive", variant="error")

    def on_mount(self) -> None:
        self.query_one("#shipped-cancel").focus()

    def on_key(self, event: events.Key) -> None:
        focused = self.focused
        buttons = list(self.query("#shipped-buttons Button"))

        if event.key == "right" and focused in buttons:
            event.prevent_default()
            event.stop()
            idx = buttons.index(focused)
            buttons[(idx + 1) % len(buttons)].focus()
        elif event.key == "left" and focused in buttons:
            event.prevent_default()
            event.stop()
            idx = buttons.index(focused)
            buttons[(idx - 1) % len(buttons)].focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "shipped-cancel":
            self.dismiss(None)
        elif event.button.id == "shipped-archive":
            self.dismiss(True)


class LegendScreen(ModalScreen):
    """Modal screen showing the symbol legend."""

    BINDINGS = [
        Binding("escape", "cancel", "Close"),
    ]

    CSS = """
    LegendScreen {
        align: center middle;
        height: 100%;
    }

    #legend-container {
        width: 70;
        height: auto;
        max-height: 80%;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }

    #legend-title {
        text-align: center;
        text-style: bold;
        color: $text;
        padding-bottom: 1;
    }

    #legend-body {
        padding: 0 2;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="legend-container"):
            yield Static("Legend", id="legend-title")
            yield Static(self._build_legend(), id="legend-body")

    def _build_legend(self) -> Text:
        t = Text()

        t.append("Status\n", style="bold")
        t.append(f"  {STATUS_RUNNING}", style="bright_green")
        t.append("  running\n", style="bright_black")
        t.append(f"  {STATUS_STUCK}", style="bright_yellow")
        t.append("  stuck\n", style="bright_black")
        t.append(f"  {STATUS_ERROR}", style="bright_red")
        t.append("  error\n", style="bright_black")
        t.append(f"  {STATUS_NEW}", style="bright_black")
        t.append("  new\n", style="bright_black")
        t.append(f"  {STATUS_SHIPPED}", style="bright_green")
        t.append("  shipped\n", style="bright_black")
        t.append(f"  {STATUS_DISCONNECTED}", style="bright_red")
        t.append("  disconnected\n", style="bright_black")

        t.append("\n")

        t.append("Auto\n", style="bold")
        t.append(f"  {AUTO_ON}", style="bright_green")
        t.append("  auto-advance on\n", style="bright_black")
        t.append(f"  {AUTO_OFF}", style="bright_black")
        t.append("  auto-advance off\n", style="bright_black")

        t.append("\n")
        t.append("Keys\n", style="bold")
        t.append("  r", style="bright_cyan")
        t.append("  reload stage\n", style="bright_black")
        t.append("  a", style="bright_cyan")
        t.append("  toggle auto-advance\n", style="bright_black")
        t.append("  d", style="bright_cyan")
        t.append("  delete/archive", style="bright_black")

        return t

    def action_cancel(self) -> None:
        self.dismiss()


class FileViewerScreen(Screen):
    """Fullscreen file viewer for a lode directory."""

    BINDINGS = [
        Binding("escape", "dismiss", "Back"),
    ]

    CSS = """
    FileViewerScreen {
        background: $background;
    }
    FileViewerScreen DirectoryTree {
        dock: left;
        width: auto;
        min-width: 20;
        max-width: 50%;
        background: $surface;
        border-right: solid $primary;
        scrollbar-background: $surface;
        scrollbar-color: $primary;
    }
    FileViewerScreen VerticalScroll {
        background: $background;
    }
    FileViewerScreen #code-view {
        padding: 1 2;
    }
    """

    path: reactive[str] = reactive("")

    def __init__(self, lode_dir: Path, lode_id: str) -> None:
        super().__init__()
        self.lode_dir = lode_dir
        self.lode_id = lode_id

    def compose(self) -> ComposeResult:
        yield DirectoryTree(str(self.lode_dir))
        with VerticalScroll():
            yield Static(id="code-view")

    @property
    def sub_title(self) -> str:
        return f"lode {self.lode_id}"

    @sub_title.setter
    def sub_title(self, _value: str) -> None:
        """Ignore base Screen assignment; subtitle is derived from lode ID."""

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        self.path = str(event.path)

    def watch_path(self) -> None:
        code_view = self.query_one("#code-view", Static)
        if not self.path:
            code_view.update("")
            return
        try:
            code = Path(self.path).read_text(encoding="utf-8")
        except Exception:
            code_view.update("Cannot read file")
            return
        syntax = highlight(code, path=self.path)
        code_view.update(syntax)
        self.query_one(VerticalScroll).scroll_home(animate=False)

    def action_dismiss(self) -> None:
        self.dismiss()


class LodeTable(DataTable):
    """Table displaying all lodes.

    IMPORTANT: Never use table.clear() to refresh data -- it resets cursor
    position. Use update_cell() for existing rows, add_row()/remove_row()
    only when rows actually change. Define column keys explicitly with
    add_column("Label", key="col_key") to enable update_cell().
    """

    # Column keys for update_cell operations
    COL_STATUS = "status"
    COL_AUTO = "auto"
    COL_STAGE = "stage"
    COL_ID = "id"
    COL_PROJECT = "project"
    COL_AGE = "age"
    COL_LAST = "last"
    COL_TITLE = "title"
    COL_STATUS_TEXT = "status_text"

    def __init__(self, **kwargs):
        super().__init__(cursor_foreground_priority="renderable", **kwargs)
        self.cursor_type = "row"

    def on_mount(self) -> None:
        """Set up columns when mounted with explicit keys."""
        self.add_column("", key=self.COL_STATUS)
        self.add_column(AUTO_ON, key=self.COL_AUTO, width=3)
        self.add_column("stage", key=self.COL_STAGE)
        self.add_column("id", key=self.COL_ID)
        self.add_column("project", key=self.COL_PROJECT)
        self.add_column("age", key=self.COL_AGE)
        self.add_column("last", key=self.COL_LAST)
        self.add_column("title", key=self.COL_TITLE, width=26)
        self.add_column("status", key=self.COL_STATUS_TEXT)

    def on_resize(self, event: events.Resize) -> None:
        """Make the last column fill remaining width."""
        cols = list(self.columns.values())
        if not cols:
            return
        fixed_width = sum(c.get_render_width(self) for c in cols[:-1])
        last = cols[-1]
        last.width = max(1, event.size.width - fixed_width - 2 * self.cell_padding)
        last.auto_width = False

    def on_key(self, event: Key) -> None:
        if event.key == "left":
            event.prevent_default()
            event.stop()
            self.app.set_archive_view(True)
        elif event.key == "right":
            event.prevent_default()
            event.stop()
            self.app.set_archive_view(False)


class BacklogTable(DataTable):
    """Table displaying backlog items."""

    COL_PROJECT = "project"
    COL_DESCRIPTION = "description"
    COL_AGE = "age"

    def __init__(self, **kwargs):
        super().__init__(cursor_foreground_priority="renderable", **kwargs)
        self.cursor_type = "row"

    def on_mount(self) -> None:
        """Set up columns when mounted with explicit keys."""
        self.add_column("project", key=self.COL_PROJECT)
        self.add_column("added", key=self.COL_AGE)
        self.add_column("description", key=self.COL_DESCRIPTION)

    def on_resize(self, event: events.Resize) -> None:
        """Make the last column fill remaining width."""
        cols = list(self.columns.values())
        if not cols:
            return
        fixed_width = sum(c.get_render_width(self) for c in cols[:-1])
        last = cols[-1]
        last.width = max(1, event.size.width - fixed_width - 2 * self.cell_padding)
        last.auto_width = False


class HopperApp(App):
    """Hopper TUI application."""

    TITLE = "HOPPER"

    CSS = """
    Screen {
        layout: vertical;
        background: $background;
    }

    Header {
        background: $surface;
        color: $text;
    }

    Footer {
        background: $surface;
    }

    .section {
        border: solid $panel;
        margin: 0 1;
    }

    .section:focus-within {
        border: solid $primary;
    }

    .section-label {
        height: 1;
        padding: 0 1;
        color: $text-muted;
        text-style: bold;
        background: $surface;
    }

    #lode-panel {
        height: 1fr;
        margin-top: 1;
    }

    #lode-table {
        height: 1fr;
        background: $background;
    }

    #backlog-panel {
        height: auto;
        max-height: 40%;
        margin-bottom: 1;
    }

    #backlog-table {
        height: auto;
        max-height: 100%;
        background: $background;
    }

    DataTable > .datatable--cursor {
        background: $panel;
    }

    DataTable > .datatable--header {
        background: $surface;
        color: $text-muted;
        text-style: bold;
    }

    DataTable:focus > .datatable--header {
        color: $text;
    }

    DataTable:blur > .datatable--cursor {
        background: $surface;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
        Binding("ctrl+d", "quit", "Quit", show=False, priority=True),
        Binding("c", "new_lode", "Create"),
        Binding("b", "new_backlog", "Backlog"),
        Binding("a", "toggle_auto", "Auto"),
        Binding("d", "delete", "Delete"),
        Binding("l", "legend", "Legend"),
        Binding("v", "view_files", "View"),
        Binding("r", "reload", "Reload"),
    ]

    def __init__(self, server=None):
        super().__init__()
        self.server = server
        self._lodes: list[dict] = server.lodes if server else []
        self._archived_lodes = server.archived_lodes if server else []
        self._archive_view: bool = False
        self._backlog: list[BacklogItem] = server.backlog if server else []
        self._projects: list[Project] = list(server.projects) if server else []
        self._git_hash: str = server.git_hash if server and server.git_hash else ""
        self._started_at: int | None = server.started_at if server else None
        self._update_sub_title()

    def compose(self) -> ComposeResult:
        """Create the UI layout."""
        yield Header()
        with Vertical(id="lode-panel", classes="section"):
            yield Static("lodes", id="lodes_label", classes="section-label")
            yield LodeTable(id="lode-table")
        with Vertical(id="backlog-panel", classes="section"):
            yield Static("backlog", classes="section-label")
            yield BacklogTable(id="backlog-table")
        yield Footer()

    def on_mount(self) -> None:
        """Initialize when app is mounted."""
        # Register and apply Claude-inspired theme
        self.register_theme(CLAUDE_THEME)
        self.theme = "claude"

        self.refresh_table()
        self.refresh_backlog()
        # Start polling for server updates
        self.set_interval(1.0, self.check_server_updates)
        # Focus the lode table
        self.query_one("#lode-table", LodeTable).focus()

    def check_server_updates(self) -> None:
        """Poll server's lode list and refresh if needed."""
        self._update_sub_title()
        self.refresh_table()
        self.refresh_backlog()
        if self.server:
            self._projects = list(self.server.projects)

    def _update_sub_title(self) -> None:
        """Update sub_title with git hash and uptime."""
        parts = []
        if self._git_hash:
            parts.append(self._git_hash)
        if self._started_at:
            parts.append(format_uptime(self._started_at))
        self.sub_title = " · ".join(parts)

    def set_archive_view(self, archived: bool) -> None:
        """Switch between active and archived lode views."""
        if self._archive_view == archived:
            return
        self._archive_view = archived
        label = self.query_one("#lodes_label", Static)
        label.update("lodes · archived" if archived else "lodes")
        self.refresh_table()

    def refresh_table(self) -> None:
        """Refresh the table using incremental updates to preserve cursor position.

        Uses Textual's update_cell() for existing rows instead of clear()+add_row()
        which would reset cursor position on every refresh.
        """
        table = self.query_one("#lode-table", LodeTable)

        def archived_sort_key(lode: dict) -> int:
            updated_at = lode.get("updated_at")
            return updated_at if isinstance(updated_at, int) else 0

        if self._archive_view:
            lodes = sorted(
                self._archived_lodes,
                key=archived_sort_key,
                reverse=True,
            )
        else:
            lodes = sorted(
                self._lodes, key=lambda lode: STAGE_ORDER.get(lode.get("stage", "mill"), 0)
            )

        # Build rows for the current view.
        rows = [
            lode_to_row(s) for s in lodes if self._archive_view or s.get("stage") in STAGE_ORDER
        ]

        # Get current row keys in table (excluding hint row)
        existing_keys: set[str] = set()
        for row_key in table.rows:
            key = str(row_key.value)
            if key != HINT_LODE:
                existing_keys.add(key)

        # Get desired row keys
        desired_keys = {row.id for row in rows}

        has_hint = HINT_LODE in {str(k.value) for k in table.rows}

        # Remove rows that no longer exist
        for key in existing_keys - desired_keys:
            table.remove_row(key)

        # Add or update rows (insert before hint row if it exists)
        for row in rows:
            if row.id in existing_keys:
                # Update existing row cells
                table.update_cell(row.id, LodeTable.COL_STATUS, format_status_text(row.status))
                table.update_cell(row.id, LodeTable.COL_AUTO, format_auto_text(row.auto))
                table.update_cell(row.id, LodeTable.COL_STAGE, format_stage_text(row.stage))
                table.update_cell(row.id, LodeTable.COL_ID, row.id)
                table.update_cell(row.id, LodeTable.COL_PROJECT, row.project)
                table.update_cell(row.id, LodeTable.COL_AGE, row.age)
                table.update_cell(row.id, LodeTable.COL_LAST, row.last)
                table.update_cell(row.id, LodeTable.COL_TITLE, row.title)
                table.update_cell(
                    row.id,
                    LodeTable.COL_STATUS_TEXT,
                    format_status_label(row.status_text, row.status),
                )
            else:
                # Add new row (before hint if present)
                if has_hint:
                    table.remove_row(HINT_LODE)
                    has_hint = False
                table.add_row(
                    format_status_text(row.status),
                    format_auto_text(row.auto),
                    format_stage_text(row.stage),
                    row.id,
                    row.project,
                    row.age,
                    row.last,
                    row.title,
                    format_status_label(row.status_text, row.status),
                    key=row.id,
                )

        hint_text = "← back to active lodes" if self._archive_view else "c to create new lode"
        hint = Text(hint_text, style="bright_black italic")

        # Keep hint row text in sync with active/archive mode.
        if has_hint:
            table.update_cell(HINT_LODE, LodeTable.COL_STATUS_TEXT, hint)
        else:
            table.add_row("", "", "", "", "", "", "", "", hint, key=HINT_LODE)

    def refresh_backlog(self) -> None:
        """Refresh the backlog table using incremental updates."""
        table = self.query_one("#backlog-table", BacklogTable)

        items = self._backlog

        existing_keys: set[str] = set()
        for row_key in table.rows:
            key = str(row_key.value)
            if key != HINT_BACKLOG:
                existing_keys.add(key)

        desired_keys = {item.id for item in items}

        has_hint = HINT_BACKLOG in {str(k.value) for k in table.rows}

        for key in existing_keys - desired_keys:
            table.remove_row(key)

        for item in items:
            age = format_age(item.created_at)
            if item.id in existing_keys:
                table.update_cell(item.id, BacklogTable.COL_PROJECT, item.project)
                table.update_cell(item.id, BacklogTable.COL_DESCRIPTION, item.description)
                table.update_cell(item.id, BacklogTable.COL_AGE, age)
            else:
                # Add new row (before hint if present)
                if has_hint:
                    table.remove_row(HINT_BACKLOG)
                    has_hint = False
                table.add_row(item.project, age, item.description, key=item.id)

        # Add hint row at the bottom if not already there
        if not has_hint:
            hint = Text("b to add to backlog", style="bright_black italic")
            table.add_row("", "", hint, key=HINT_BACKLOG)

    def _get_selected_row_key(self, table: DataTable) -> str | None:
        """Get the row key of the selected row in a table."""
        if table.cursor_row is not None and table.row_count > 0:
            cell_key = table.coordinate_to_cell_key((table.cursor_row, 0))
            return str(cell_key.row_key.value) if cell_key.row_key else None
        return None

    def _get_selected_lode_id(self) -> str | None:
        """Get the lode ID of the selected row (skips hint rows)."""
        key = self._get_selected_row_key(self.query_one("#lode-table", LodeTable))
        if key and key.startswith("_hint"):
            return None
        return key

    def _get_selected_backlog_id(self) -> str | None:
        """Get the backlog item ID of the selected row (skips hint rows)."""
        key = self._get_selected_row_key(self.query_one("#backlog-table", BacklogTable))
        if key and key.startswith("_hint"):
            return None
        return key

    def _get_backlog_item(self, item_id: str) -> BacklogItem | None:
        """Get a backlog item by ID."""
        for item in self._backlog:
            if item.id == item_id:
                return item
        return None

    def _get_lode(self, lode_id: str) -> dict | None:
        """Get a lode by ID."""
        for lode in self._lodes:
            if lode["id"] == lode_id:
                return lode
        return None

    def _require_projects(self) -> bool:
        """Check that projects are configured. Returns True if available."""
        if not self._projects:
            self.notify("No projects configured. Use: hop project add <path>", severity="warning")
            return False
        return True

    def action_new_lode(self) -> None:
        """Open project picker, then scope input, to create a lode or backlog item."""
        if self._archive_view:
            return
        if not self._require_projects():
            return

        def on_project_selected(project: Project | None) -> None:
            if project is None:
                return  # Cancelled

            touch_project(project.name)
            if self.server:
                self.server.enqueue({"type": "projects_reload"})

            def on_scope_entered(result: tuple[str, str] | None) -> None:
                if result is None:
                    return  # Cancelled
                scope, action = result
                if action == "backlog":
                    if self.server:
                        self.server.enqueue(
                            {
                                "type": "backlog_add",
                                "project": project.name,
                                "description": scope,
                            }
                        )
                else:
                    if self.server:
                        self.server.enqueue(
                            {
                                "type": "lode_create",
                                "project": project.name,
                                "scope": scope,
                                "spawn": True,
                            }
                        )

            self.push_screen(ScopeInputScreen(project.name), on_scope_entered)

        self.push_screen(ProjectPickerScreen(self._projects), on_project_selected)

    def action_new_backlog(self) -> None:
        """Open project picker, then backlog input, to create a new backlog item."""
        if not self._require_projects():
            return

        def on_project_selected(project: Project | None) -> None:
            if project is None:
                return  # Cancelled

            touch_project(project.name)
            if self.server:
                self.server.enqueue({"type": "projects_reload"})

            def on_description_entered(description: str | None) -> None:
                if description is None:
                    return  # Cancelled
                if self.server:
                    self.server.enqueue(
                        {
                            "type": "backlog_add",
                            "project": project.name,
                            "description": description,
                        }
                    )

            self.push_screen(BacklogInputScreen(), on_description_entered)

        self.push_screen(ProjectPickerScreen(self._projects), on_project_selected)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle Enter key on selected row in any table."""
        if isinstance(event.data_table, LodeTable) and self._archive_view:
            lode_id = self._get_selected_lode_id()
            if lode_id is None:
                return
            lode_dir = get_lode_dir(lode_id)
            self.push_screen(FileViewerScreen(lode_dir, lode_id))
            return
        key = str(event.row_key.value)

        # Hint row actions
        if key == HINT_LODE:
            self.action_new_lode()
            return
        if key == HINT_BACKLOG:
            self.action_new_backlog()
            return

        if isinstance(event.data_table, BacklogTable):
            self._edit_backlog_item(key)
            return

        if not isinstance(event.data_table, LodeTable):
            return

        lode = self._get_lode(key)
        if not lode:
            self.notify(f"Lode {key} not found", severity="error")
            return

        lode_project = lode.get("project", "")
        project = find_project(lode_project) if lode_project else None
        project_path = project.path if project else None

        # Check if project directory still exists
        if project_path and not Path(project_path).is_dir():
            self.notify(f"Project dir missing: {project_path}", severity="error")
            return

        if lode.get("active") and lode.get("tmux_pane"):
            # Lode has a connected runner - switch to its window
            if not switch_to_pane(lode["tmux_pane"]):
                self.notify("Failed to switch to window", severity="error")
        elif lode.get("stage") == "refine" and lode.get("state") == "ready":
            # Mill complete, ready for refine - review before starting
            self._review_mill_output(lode, project_path)
        elif lode.get("stage") == "ship" and lode.get("state") == "ready":
            # Refine complete, ready to ship - review changes before shipping
            self._review_ship(lode, project_path)
        else:
            # Lode is not active - spawn runner based on stage
            if not spawn_claude(lode["id"], project_path):
                self.notify("Failed to spawn tmux window", severity="error")

        self.refresh_table()

    def action_delete(self) -> None:
        """Delete: archive lode or remove backlog item, depending on focus."""
        if isinstance(self.focused, LodeTable):
            if self._archive_view:
                return
            lode_id = self._get_selected_lode_id()
            if not lode_id:
                return
            lode = self._get_lode(lode_id)
            branch = lode.get("branch", "") if lode else ""
            branch = branch or f"hopper-{lode_id}"
            # Check for worktree with unmerged changes
            worktree_path = get_lode_dir(lode_id) / "worktree"
            if worktree_path.is_dir():
                diff_stat = get_diff_stat(str(worktree_path))
                if diff_stat:
                    # Has unmerged changes - show confirmation modal
                    def on_confirm(result: bool | None) -> None:
                        if result and self.server:
                            self.server.enqueue({"type": "lode_archive", "lode_id": lode_id})

                    self.push_screen(
                        ArchiveConfirmScreen(diff_stat=diff_stat, branch=branch),
                        on_confirm,
                    )
                    return
            # No worktree or no changes - archive immediately
            if self.server:
                self.server.enqueue({"type": "lode_archive", "lode_id": lode_id})
        elif isinstance(self.focused, BacklogTable):
            item_id = self._get_selected_backlog_id()
            if not item_id:
                return
            if self.server:
                self.server.enqueue({"type": "backlog_remove", "item_id": item_id})

    def action_toggle_auto(self) -> None:
        """Toggle auto-advance on the selected lode."""
        if not isinstance(self.focused, LodeTable):
            return
        if self._archive_view:
            return

        lode_id = self._get_selected_lode_id()
        if not lode_id:
            return

        lode = self._get_lode(lode_id)
        if not lode:
            return

        new_auto = not lode.get("auto", False)
        if self.server:
            self.server.enqueue({"type": "lode_set_auto", "lode_id": lode["id"], "auto": new_auto})

    def action_reload(self) -> None:
        """Reload the current stage with a fresh Claude session."""
        if not isinstance(self.focused, LodeTable):
            return
        if self._archive_view:
            return

        lode_id = self._get_selected_lode_id()
        if not lode_id:
            return

        lode = self._get_lode(lode_id)
        if not lode:
            return

        if lode.get("active"):
            self.notify("Cannot reload while active", severity="warning")
            return

        stage = lode.get("stage", "")
        if stage not in ("mill", "refine", "ship"):
            return

        if self.server:
            self.server.enqueue(
                {
                    "type": "lode_reset_claude_stage",
                    "lode_id": lode["id"],
                    "claude_stage": stage,
                    "spawn": True,
                }
            )
        self.notify(f"Reloading {stage}...")

    def action_legend(self) -> None:
        """Show the symbol legend modal."""
        self.push_screen(LegendScreen())

    def action_view_files(self) -> None:
        """Open the file viewer for the selected lode."""
        if not isinstance(self.focused, LodeTable):
            return
        lode_id = self._get_selected_lode_id()
        if lode_id is None:
            return
        lode_dir = get_lode_dir(lode_id)
        self.push_screen(FileViewerScreen(lode_dir, lode_id))

    def _edit_backlog_item(self, item_id: str) -> None:
        """Open the edit modal for a backlog item."""
        item = self._get_backlog_item(item_id)
        if not item:
            return

        def on_edit_result(result: tuple[str, str] | None) -> None:
            if result is None:
                return  # Cancelled
            action, text = result
            if action == "save":
                if self.server:
                    self.server.enqueue(
                        {
                            "type": "backlog_update",
                            "item_id": item_id,
                            "description": text,
                        }
                    )
            elif action == "promote":
                if self.server:
                    self.server.enqueue(
                        {
                            "type": "lode_promote_backlog",
                            "item_id": item_id,
                            "scope": text,
                        }
                    )

        self.push_screen(BacklogEditScreen(initial_text=item.description), on_edit_result)

    def _review_mill_output(self, lode: dict, project_path: str | None) -> None:
        """Open the mill output review modal for a refine-ready lode."""
        mill_path = get_lode_dir(lode["id"]) / "mill_out.md"
        if not mill_path.exists():
            self.notify("Mill output not found", severity="error")
            return

        mill_text = mill_path.read_text()

        def on_review_result(result: tuple[str, str] | None) -> None:
            if result is None:
                return  # Cancelled
            action, text = result
            # Write edited text back to mill_out.md
            tmp_path = mill_path.with_suffix(".md.tmp")
            tmp_path.write_text(text)
            os.replace(tmp_path, mill_path)
            if action == "process":
                spawn_claude(lode["id"], project_path, foreground=False)
            self.refresh_table()

        self.push_screen(MillReviewScreen(initial_text=mill_text), on_review_result)

    def _review_ship(self, lode: dict, project_path: str | None) -> None:
        """Open the ship review modal for a ship-ready lode."""
        worktree_path = get_lode_dir(lode["id"]) / "worktree"
        if not worktree_path.is_dir():
            self.notify("Worktree not found", severity="error")
            return

        diff_stat = get_diff_stat(str(worktree_path))

        def on_review_result(result: str | None) -> None:
            if result is None:
                return  # Cancelled
            if result == "ship":
                spawn_claude(lode["id"], project_path, foreground=False)
            elif result == "refine":
                if self.server:
                    self.server.enqueue({"type": "lode_resume_refine", "lode_id": lode["id"]})

        self.push_screen(ShipReviewScreen(diff_stat=diff_stat), on_review_result)


def run_tui(server=None) -> int:
    """Run the TUI application.

    Args:
        server: Optional Server instance for shared lode state.

    Returns:
        Exit code (0 for success).
    """
    app = HopperApp(server=server)
    app.run()
    return 0
