"""TUI for managing coding agents using Textual."""

from dataclasses import dataclass
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import DataTable, Footer, Header, OptionList, Static
from textual.widgets.option_list import Option

from hopper.claude import spawn_claude, switch_to_window
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
STATUS_IDLE = "○"  # empty circle
STATUS_ERROR = "✗"  # x mark
STATUS_ACTION = "+"  # plus for action rows

# Stage indicators
STAGE_ORE = "⚒"  # hammer and pick
STAGE_PROCESSING = "⛭"  # gear


@dataclass
class Row:
    """A row in a table."""

    id: str
    short_id: str
    stage: str  # "o" for ore, "p" for processing
    age: str  # formatted age string
    status: str  # STATUS_RUNNING, STATUS_IDLE, STATUS_ERROR
    project: str = ""  # Project name
    message: str = ""  # Human-readable status message


def session_to_row(session: Session) -> Row:
    """Convert a session to a display row."""
    if session.state == "error":
        status = STATUS_ERROR
    elif session.state == "running":
        status = STATUS_RUNNING
    else:
        status = STATUS_IDLE

    stage = STAGE_ORE if session.stage == "ore" else STAGE_PROCESSING

    return Row(
        id=session.id,
        short_id=session.short_id,
        stage=stage,
        age=format_age(session.created_at),
        status=status,
        project=session.project,
        message=session.message,
    )


def format_status_text(status: str) -> Text:
    """Format a status indicator with color using Rich Text."""
    if status == STATUS_RUNNING:
        return Text(status, style="bright_green")
    elif status == STATUS_ERROR:
        return Text(status, style="bright_red")
    elif status == STATUS_ACTION:
        return Text(status, style="bright_magenta")
    else:  # STATUS_IDLE
        return Text(status, style="bright_black")


def format_stage_text(stage: str) -> Text:
    """Format a stage indicator with color using Rich Text."""
    if stage == STAGE_ORE:
        return Text(stage, style="bright_blue")
    else:  # STAGE_PROCESSING
        return Text(stage, style="bright_yellow")


class ProjectPickerScreen(ModalScreen[Project | None]):
    """Modal screen for picking a project."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "select", "Select"),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    CSS = """
    ProjectPickerScreen {
        align: center middle;
    }

    #picker-container {
        width: 50;
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
    }

    #project-list > .option-list--option-highlighted {
        background: $panel;
    }
    """

    def __init__(self, projects: list[Project]):
        super().__init__()
        self._projects = projects

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-container"):
            yield Static("New Session", id="picker-title")
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

    def action_cursor_down(self) -> None:
        self.query_one("#project-list", OptionList).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#project-list", OptionList).action_cursor_up()


class SessionTable(DataTable):
    """Table displaying all sessions."""

    # Column keys for update_cell operations
    COL_STATUS = "status"
    COL_STAGE = "stage"
    COL_ID = "id"
    COL_PROJECT = "project"
    COL_AGE = "age"
    COL_MESSAGE = "message"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.cursor_type = "row"

    def on_mount(self) -> None:
        """Set up columns when mounted with explicit keys."""
        self.add_column("", key=self.COL_STATUS)
        self.add_column("S", key=self.COL_STAGE)
        self.add_column("ID", key=self.COL_ID)
        self.add_column("PROJECT", key=self.COL_PROJECT)
        self.add_column("AGE", key=self.COL_AGE)
        self.add_column("MESSAGE", key=self.COL_MESSAGE)


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

    #session-table {
        height: 1fr;
        background: $background;
    }

    #empty-message {
        height: 3;
        content-align: center middle;
        color: $text-muted;
    }

    DataTable > .datatable--cursor {
        background: $panel;
    }

    DataTable > .datatable--header {
        background: $surface;
        color: $text-muted;
        text-style: bold;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("escape", "quit", "Quit", show=False),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("down", "cursor_down", show=False),
        Binding("up", "cursor_up", show=False),
        Binding("enter", "select_row", "Select"),
        Binding("c", "new_session", "Create"),
        Binding("a", "archive", "Archive"),
    ]

    def __init__(self, server=None):
        super().__init__()
        self.server = server
        self._sessions: list[Session] = server.sessions if server else []
        self._projects: list[Project] = []
        self._git_hash: str = server.git_hash if server and server.git_hash else ""
        self._started_at: int | None = server.started_at if server else None
        self._update_sub_title()

    def compose(self) -> ComposeResult:
        """Create the UI layout."""
        yield Header()
        yield SessionTable(id="session-table")
        yield Static("No sessions yet. Press 'c' to create one.", id="empty-message")
        yield Footer()

    def on_mount(self) -> None:
        """Initialize when app is mounted."""
        # Register and apply Claude-inspired theme
        self.register_theme(CLAUDE_THEME)
        self.theme = "claude"

        self._projects = get_active_projects()
        self.refresh_table()
        # Start polling for server updates
        self.set_interval(1.0, self.check_server_updates)
        # Focus the table
        self.query_one("#session-table", SessionTable).focus()

    def check_server_updates(self) -> None:
        """Poll server's session list and refresh if needed."""
        self._update_sub_title()
        self.refresh_table()

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
        empty_msg = self.query_one("#empty-message", Static)

        # Build rows from sessions (ore first, then processing)
        rows: list[Row] = []
        ore_sessions = [s for s in self._sessions if s.stage == "ore"]
        processing_sessions = [s for s in self._sessions if s.stage == "processing"]

        for session in ore_sessions:
            rows.append(session_to_row(session))
        for session in processing_sessions:
            rows.append(session_to_row(session))

        # Get current row keys in table
        existing_keys: set[str] = set()
        for row_key in table.rows:
            existing_keys.add(str(row_key.value))

        # Get desired row keys
        desired_keys = {row.id for row in rows}

        # Remove rows that no longer exist
        for key in existing_keys - desired_keys:
            table.remove_row(key)

        # Add or update rows
        for row in rows:
            if row.id in existing_keys:
                # Update existing row cells
                table.update_cell(row.id, SessionTable.COL_STATUS, format_status_text(row.status))
                table.update_cell(row.id, SessionTable.COL_STAGE, format_stage_text(row.stage))
                table.update_cell(row.id, SessionTable.COL_ID, row.short_id)
                table.update_cell(row.id, SessionTable.COL_PROJECT, row.project)
                table.update_cell(row.id, SessionTable.COL_AGE, row.age)
                table.update_cell(
                    row.id, SessionTable.COL_MESSAGE, self._truncate_message(row.message)
                )
            else:
                # Add new row
                table.add_row(
                    format_status_text(row.status),
                    format_stage_text(row.stage),
                    row.short_id,
                    row.project,
                    row.age,
                    self._truncate_message(row.message),
                    key=row.id,
                )

        # Toggle empty message visibility
        if rows:
            empty_msg.display = False
            table.display = True
        else:
            empty_msg.display = True
            table.display = False

    def _truncate_message(self, message: str, max_len: int = 60) -> str:
        """Truncate message for display, replacing newlines."""
        text = message.replace("\n", " ") if message else ""
        if len(text) > max_len:
            return text[: max_len - 3] + "..."
        return text

    def _get_selected_session_id(self) -> str | None:
        """Get the session ID of the selected row."""
        table = self.query_one("#session-table", SessionTable)
        if table.cursor_row is not None and table.row_count > 0:
            cell_key = table.coordinate_to_cell_key((table.cursor_row, 0))
            return str(cell_key.row_key.value) if cell_key.row_key else None
        return None

    def _get_session(self, session_id: str) -> Session | None:
        """Get a session by ID."""
        for session in self._sessions:
            if session.id == session_id:
                return session
        return None

    def action_cursor_down(self) -> None:
        """Move cursor down."""
        table = self.query_one("#session-table", SessionTable)
        table.action_cursor_down()

    def action_cursor_up(self) -> None:
        """Move cursor up."""
        table = self.query_one("#session-table", SessionTable)
        table.action_cursor_up()

    def action_new_session(self) -> None:
        """Open project picker to create a new session."""
        if not self._projects:
            self.notify("No projects configured. Use: hop project add <path>", severity="warning")
            return

        def on_project_selected(project: Project | None) -> None:
            if project is None:
                return  # Cancelled
            session = create_session(self._sessions, project.name)
            window_id = spawn_claude(session.id, project.path)
            if window_id:
                session.tmux_window = window_id
                save_sessions(self._sessions)
            self.refresh_table()

        self.push_screen(ProjectPickerScreen(self._projects), on_project_selected)

    def action_select_row(self) -> None:
        """Handle Enter key on selected row."""
        session_id = self._get_selected_session_id()
        if not session_id:
            self.notify("No session selected", severity="warning")
            return

        session = self._get_session(session_id)
        if not session:
            self.notify(f"Session {session_id[:8]} not found", severity="error")
            return

        project = find_project(session.project) if session.project else None
        project_path = project.path if project else None

        # Check if project directory still exists
        if project_path and not Path(project_path).is_dir():
            self.notify(f"Project dir missing: {project_path}", severity="error")
            return

        if session.tmux_window and switch_to_window(session.tmux_window):
            # Successfully switched to existing window
            pass
        else:
            # Respawn in new window
            window_id = spawn_claude(session.id, project_path)
            if window_id:
                session.tmux_window = window_id
                save_sessions(self._sessions)
            else:
                self.notify("Failed to spawn tmux window", severity="error")

        self.refresh_table()

    def action_archive(self) -> None:
        """Archive the selected session."""
        session_id = self._get_selected_session_id()
        if not session_id:
            return

        archive_session(self._sessions, session_id)
        self.refresh_table()


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
