"""TUI for managing coding agents using Textual."""

from dataclasses import dataclass
from pathlib import Path

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Button, DataTable, Footer, Header, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from hopper.backlog import BacklogItem, add_backlog_item, remove_backlog_item, update_backlog_item
from hopper.claude import spawn_claude, switch_to_pane
from hopper.projects import Project, find_project, get_active_projects
from hopper.sessions import (
    Session,
    archive_session,
    create_session,
    format_age,
    format_uptime,
    save_sessions,
)

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

# Hint row keys (always present at bottom of each table)
HINT_SESSION = "_hint_session"
HINT_BACKLOG = "_hint_backlog"

# Stage indicators
STAGE_ORE = "⚒"  # hammer and pick
STAGE_PROCESSING = "⛭"  # gear
STAGE_SHIP = "▲"  # triangle up

# Status -> color mapping (shared by icon and text formatting)
STATUS_COLORS = {
    STATUS_RUNNING: "bright_green",
    STATUS_STUCK: "bright_yellow",
    STATUS_ERROR: "bright_red",
    STATUS_NEW: "bright_black",
}


@dataclass
class Row:
    """A row in a table."""

    id: str
    short_id: str
    stage: str  # STAGE_ORE, STAGE_PROCESSING, or STAGE_SHIP
    age: str  # formatted age string
    status: str  # STATUS_RUNNING, STATUS_STUCK, STATUS_NEW, STATUS_ERROR
    active: bool = False  # Whether a runner is connected
    project: str = ""  # Project name
    status_text: str = ""  # Human-readable status text


def session_to_row(session: Session) -> Row:
    """Convert a session to a display row."""
    if session.state == "new":
        status = STATUS_NEW
    elif session.state == "error":
        status = STATUS_ERROR
    elif session.state == "stuck":
        status = STATUS_STUCK
    else:
        status = STATUS_RUNNING

    if session.stage == "ore":
        stage = STAGE_ORE
    elif session.stage == "ship":
        stage = STAGE_SHIP
    else:
        stage = STAGE_PROCESSING

    return Row(
        id=session.id,
        short_id=session.short_id,
        stage=stage,
        age=format_age(session.created_at),
        status=status,
        active=session.active,
        project=session.project,
        status_text=session.status,
    )


def format_status_text(status: str) -> Text:
    """Format a status icon with color using Rich Text."""
    return Text(status, style=STATUS_COLORS.get(status, ""))


def format_status_label(label: str, status: str) -> Text:
    """Format status text with color matching the status icon."""
    return Text(label.replace("\n", " ") if label else "", style=STATUS_COLORS.get(status, ""))


def format_active_text(active: bool) -> Text:
    """Format an active indicator with color using Rich Text."""
    if active:
        return Text("▸", style="bright_cyan")
    else:
        return Text("▹", style="bright_black")


def format_stage_text(stage: str) -> Text:
    """Format a stage indicator with color using Rich Text."""
    if stage == STAGE_ORE:
        return Text(stage, style="bright_blue")
    elif stage == STAGE_SHIP:
        return Text(stage, style="bright_green")
    else:  # STAGE_PROCESSING
        return Text(stage, style="bright_yellow")


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

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
            return
        text = self._get_text()
        if not text:
            self.notify("Please enter a description", severity="warning")
            return
        self.on_submit(event.button, text)

    def on_submit(self, button: Button, text: str) -> None:
        """Handle a validated submit. Subclasses must override."""
        raise NotImplementedError


class ScopeInputScreen(TextInputScreen):
    """Modal screen for entering task scope and spawn mode."""

    MODAL_TITLE = "Describe Task Scope"

    def compose_buttons(self) -> ComposeResult:
        yield Button("Cancel", id="btn-cancel", variant="default")
        yield Button("Background", id="btn-background", variant="default")
        yield Button("Foreground", id="btn-foreground", variant="primary")

    def on_submit(self, button: Button, text: str) -> None:
        foreground = button.id == "btn-foreground"
        self.dismiss((text, foreground))


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

        t.append("\n")

        t.append("Stage\n", style="bold")
        t.append(f"  {STAGE_ORE}", style="bright_blue")
        t.append("  ore\n", style="bright_black")
        t.append(f"  {STAGE_PROCESSING}", style="bright_yellow")
        t.append("  processing\n", style="bright_black")
        t.append(f"  {STAGE_SHIP}", style="bright_green")
        t.append("  ship\n", style="bright_black")

        t.append("\n")

        t.append("Connection\n", style="bold")
        t.append("  ▸", style="bright_cyan")
        t.append("  connected\n", style="bright_black")
        t.append("  ▹", style="bright_black")
        t.append("  disconnected", style="bright_black")

        return t

    def action_cancel(self) -> None:
        self.dismiss()


class SessionTable(DataTable):
    """Table displaying all sessions.

    IMPORTANT: Never use table.clear() to refresh data -- it resets cursor
    position. Use update_cell() for existing rows, add_row()/remove_row()
    only when rows actually change. Define column keys explicitly with
    add_column("Label", key="col_key") to enable update_cell().
    """

    # Column keys for update_cell operations
    COL_STATUS = "status"
    COL_ACTIVE = "active"
    COL_STAGE = "stage"
    COL_ID = "id"
    COL_PROJECT = "project"
    COL_AGE = "age"
    COL_STATUS_TEXT = "status_text"

    def __init__(self, **kwargs):
        super().__init__(cursor_foreground_priority="renderable", **kwargs)
        self.cursor_type = "row"

    def on_mount(self) -> None:
        """Set up columns when mounted with explicit keys."""
        self.add_column("", key=self.COL_STATUS)
        self.add_column("", key=self.COL_ACTIVE)
        self.add_column("s", key=self.COL_STAGE)
        self.add_column("id", key=self.COL_ID)
        self.add_column("project", key=self.COL_PROJECT)
        self.add_column("last", key=self.COL_AGE)
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

    #session-panel {
        height: 1fr;
        margin-top: 1;
    }

    #session-table {
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
        Binding("c", "new_session", "Create"),
        Binding("b", "new_backlog", "Backlog"),
        Binding("a", "archive", "Archive"),
        Binding("d", "delete_backlog", "Delete", show=False),
        Binding("l", "legend", "Legend"),
    ]

    def __init__(self, server=None):
        super().__init__()
        self.server = server
        self._sessions: list[Session] = server.sessions if server else []
        self._backlog: list[BacklogItem] = server.backlog if server else []
        self._projects: list[Project] = []
        self._git_hash: str = server.git_hash if server and server.git_hash else ""
        self._started_at: int | None = server.started_at if server else None
        self._update_sub_title()

    def compose(self) -> ComposeResult:
        """Create the UI layout."""
        yield Header()
        with Vertical(id="session-panel", classes="section"):
            yield Static("sessions", classes="section-label")
            yield SessionTable(id="session-table")
        with Vertical(id="backlog-panel", classes="section"):
            yield Static("backlog", classes="section-label")
            yield BacklogTable(id="backlog-table")
        yield Footer()

    def on_mount(self) -> None:
        """Initialize when app is mounted."""
        # Register and apply Claude-inspired theme
        self.register_theme(CLAUDE_THEME)
        self.theme = "claude"

        self._projects = get_active_projects()
        self.refresh_table()
        self.refresh_backlog()
        # Start polling for server updates
        self.set_interval(1.0, self.check_server_updates)
        # Focus the session table
        self.query_one("#session-table", SessionTable).focus()

    def check_server_updates(self) -> None:
        """Poll server's session list and refresh if needed."""
        self._update_sub_title()
        self.refresh_table()
        self.refresh_backlog()

    def _update_sub_title(self) -> None:
        """Update sub_title with git hash and uptime."""
        parts = []
        if self._git_hash:
            parts.append(self._git_hash)
        if self._started_at:
            parts.append(format_uptime(self._started_at))
        self.sub_title = " · ".join(parts)

    def refresh_table(self) -> None:
        """Refresh the table using incremental updates to preserve cursor position.

        Uses Textual's update_cell() for existing rows instead of clear()+add_row()
        which would reset cursor position on every refresh.
        """
        table = self.query_one("#session-table", SessionTable)

        # Build rows from sessions (ore first, then processing, then ship)
        stage_order = {"ore": 0, "processing": 1, "ship": 2}
        rows = [
            session_to_row(s)
            for s in sorted(self._sessions, key=lambda s: stage_order.get(s.stage, 3))
            if s.stage in stage_order
        ]

        # Get current row keys in table (excluding hint row)
        existing_keys: set[str] = set()
        for row_key in table.rows:
            key = str(row_key.value)
            if key != HINT_SESSION:
                existing_keys.add(key)

        # Get desired row keys
        desired_keys = {row.id for row in rows}

        has_hint = HINT_SESSION in {str(k.value) for k in table.rows}

        # Remove rows that no longer exist
        for key in existing_keys - desired_keys:
            table.remove_row(key)

        # Add or update rows (insert before hint row if it exists)
        for row in rows:
            if row.id in existing_keys:
                # Update existing row cells
                table.update_cell(row.id, SessionTable.COL_STATUS, format_status_text(row.status))
                table.update_cell(row.id, SessionTable.COL_ACTIVE, format_active_text(row.active))
                table.update_cell(row.id, SessionTable.COL_STAGE, format_stage_text(row.stage))
                table.update_cell(row.id, SessionTable.COL_ID, row.short_id)
                table.update_cell(row.id, SessionTable.COL_PROJECT, row.project)
                table.update_cell(row.id, SessionTable.COL_AGE, row.age)
                table.update_cell(
                    row.id,
                    SessionTable.COL_STATUS_TEXT,
                    format_status_label(row.status_text, row.status),
                )
            else:
                # Add new row (before hint if present)
                if has_hint:
                    table.remove_row(HINT_SESSION)
                    has_hint = False
                table.add_row(
                    format_status_text(row.status),
                    format_active_text(row.active),
                    format_stage_text(row.stage),
                    row.short_id,
                    row.project,
                    row.age,
                    format_status_label(row.status_text, row.status),
                    key=row.id,
                )

        # Add hint row at the bottom if not already there
        if not has_hint:
            hint = Text("c to create new session", style="bright_black italic")
            table.add_row("", "", "", "", "", "", hint, key=HINT_SESSION)

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

    def _get_selected_session_id(self) -> str | None:
        """Get the session ID of the selected row (skips hint rows)."""
        key = self._get_selected_row_key(self.query_one("#session-table", SessionTable))
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

    def _get_session(self, session_id: str) -> Session | None:
        """Get a session by ID."""
        for session in self._sessions:
            if session.id == session_id:
                return session
        return None

    def _require_projects(self) -> bool:
        """Check that projects are configured. Returns True if available."""
        if not self._projects:
            self.notify("No projects configured. Use: hop project add <path>", severity="warning")
            return False
        return True

    def action_new_session(self) -> None:
        """Open project picker, then scope input, to create a new session."""
        if not self._require_projects():
            return

        def on_project_selected(project: Project | None) -> None:
            if project is None:
                return  # Cancelled

            def on_scope_entered(result: tuple[str, bool] | None) -> None:
                if result is None:
                    return  # Cancelled
                scope, foreground = result
                session = create_session(self._sessions, project.name, scope)
                spawn_claude(session.id, project.path, foreground)
                self.refresh_table()

            self.push_screen(ScopeInputScreen(), on_scope_entered)

        self.push_screen(ProjectPickerScreen(self._projects), on_project_selected)

    def action_new_backlog(self) -> None:
        """Open project picker, then backlog input, to create a new backlog item."""
        if not self._require_projects():
            return

        def on_project_selected(project: Project | None) -> None:
            if project is None:
                return  # Cancelled

            def on_description_entered(description: str | None) -> None:
                if description is None:
                    return  # Cancelled
                add_backlog_item(self._backlog, project.name, description)
                self.refresh_backlog()

            self.push_screen(BacklogInputScreen(), on_description_entered)

        self.push_screen(ProjectPickerScreen(self._projects), on_project_selected)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle Enter key on selected row in any table."""
        key = str(event.row_key.value)

        # Hint row actions
        if key == HINT_SESSION:
            self.action_new_session()
            return
        if key == HINT_BACKLOG:
            self.action_new_backlog()
            return

        if isinstance(event.data_table, BacklogTable):
            self._edit_backlog_item(key)
            return

        if not isinstance(event.data_table, SessionTable):
            return

        session = self._get_session(key)
        if not session:
            self.notify(f"Session {key[:8]} not found", severity="error")
            return

        project = find_project(session.project) if session.project else None
        project_path = project.path if project else None

        # Check if project directory still exists
        if project_path and not Path(project_path).is_dir():
            self.notify(f"Project dir missing: {project_path}", severity="error")
            return

        if session.active and session.tmux_pane:
            # Session has a connected runner - switch to its window
            if not switch_to_pane(session.tmux_pane):
                self.notify("Failed to switch to window", severity="error")
        else:
            # Session is not active - spawn runner based on stage
            if not spawn_claude(session.id, project_path, stage=session.stage):
                self.notify("Failed to spawn tmux window", severity="error")

        self.refresh_table()

    def action_archive(self) -> None:
        """Archive the selected session (session table only)."""
        if not isinstance(self.focused, SessionTable):
            return

        session_id = self._get_selected_session_id()
        if not session_id:
            return

        archive_session(self._sessions, session_id)
        self.refresh_table()

    def action_legend(self) -> None:
        """Show the symbol legend modal."""
        self.push_screen(LegendScreen())

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
                update_backlog_item(self._backlog, item_id, text)
                self.refresh_backlog()
            elif action == "promote":
                project = find_project(item.project)
                project_path = project.path if project else None
                session = create_session(self._sessions, item.project, text)
                session.backlog = item.to_dict()
                save_sessions(self._sessions)
                spawn_claude(session.id, project_path, foreground=False)
                remove_backlog_item(self._backlog, item_id)
                self.refresh_table()
                self.refresh_backlog()

        self.push_screen(BacklogEditScreen(initial_text=item.description), on_edit_result)

    def action_delete_backlog(self) -> None:
        """Delete the selected backlog item."""
        if not isinstance(self.focused, BacklogTable):
            return

        item_id = self._get_selected_backlog_id()
        if not item_id:
            return

        removed = remove_backlog_item(self._backlog, item_id)
        if removed:
            self.refresh_backlog()


def run_tui(server=None) -> int:
    """Run the TUI application.

    Args:
        server: Optional Server instance for shared session state.

    Returns:
        Exit code (0 for success).
    """
    app = HopperApp(server=server)
    app.run()
    return 0
