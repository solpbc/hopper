"""TUI for managing coding agents using Textual."""

from dataclasses import dataclass

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Footer, Header, Static

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

# Status indicators (Unicode symbols, no emoji)
STATUS_RUNNING = "●"  # filled circle
STATUS_IDLE = "○"  # empty circle
STATUS_ERROR = "✗"  # x mark
STATUS_ACTION = "+"  # plus for action rows


@dataclass
class Row:
    """A row in a table."""

    id: str
    short_id: str
    age: str  # formatted age string
    updated: str  # formatted updated string
    status: str  # STATUS_RUNNING, STATUS_IDLE, STATUS_ERROR, or STATUS_ACTION
    project: str = ""  # Project name
    message: str = ""  # Human-readable status message
    is_action: bool = False  # True for action rows like "new session"


def session_to_row(session: Session) -> Row:
    """Convert a session to a display row."""
    if session.state == "error":
        status = STATUS_ERROR
    elif session.state == "running":
        status = STATUS_RUNNING
    else:
        status = STATUS_IDLE

    return Row(
        id=session.id,
        short_id=session.short_id,
        age=format_age(session.created_at),
        updated=format_age(session.effective_updated_at),
        status=status,
        project=session.project,
        message=session.message,
    )


def new_shovel_row(project_name: str = "") -> Row:
    """Create the 'new session' action row."""
    return Row(
        id="new",
        short_id="new",
        age="",
        updated="",
        status=STATUS_ACTION,
        project=project_name,
        is_action=True,
    )


def format_status_text(status: str) -> Text:
    """Format a status indicator with color using Rich Text."""
    if status == STATUS_RUNNING:
        return Text(status, style="green")
    elif status == STATUS_ERROR:
        return Text(status, style="red")
    elif status == STATUS_ACTION:
        return Text(status, style="cyan")
    else:  # STATUS_IDLE
        return Text(status, style="dim")


class SessionTable(DataTable):
    """Table displaying sessions in a stage."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.cursor_type = "row"

    def on_mount(self) -> None:
        """Set up columns when mounted."""
        self.add_columns("", "ID", "PROJ", "AGE", "UPD", "MESSAGE")


class HopperApp(App):
    """Hopper TUI application."""

    TITLE = "HOPPER"

    CSS = """
    Screen {
        layout: vertical;
    }

    #ore-header, #processing-header {
        height: 1;
        padding: 0 1;
        text-style: bold;
    }

    #ore-table, #processing-table {
        height: auto;
        max-height: 50%;
    }

    #empty-processing {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }

    DataTable > .datatable--cursor {
        background: $accent;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("escape", "quit", "Quit", show=False),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", show=False),
        Binding("up", "cursor_up", show=False),
        Binding("enter", "select_row", "Select"),
        Binding("a", "archive", "Archive"),
        Binding("h", "project_left", show=False),
        Binding("l", "project_right", show=False),
        Binding("left", "project_left", show=False),
        Binding("right", "project_right", show=False),
    ]

    def __init__(self, server=None):
        super().__init__()
        self.server = server
        self._sessions: list[Session] = server.sessions if server else []
        self._projects: list[Project] = []
        self._selected_project_index: int = 0
        self._active_table: str = "ore"  # "ore" or "processing"
        self._git_hash: str = server.git_hash if server and server.git_hash else ""
        self._started_at: int | None = server.started_at if server else None
        self._update_sub_title()

    @property
    def selected_project(self) -> Project | None:
        """Get the currently selected project for new sessions."""
        if self._projects and 0 <= self._selected_project_index < len(self._projects):
            return self._projects[self._selected_project_index]
        return None

    @property
    def is_add_project_selected(self) -> bool:
        """True when 'add...' option is selected (past last project)."""
        return self._selected_project_index >= len(self._projects)

    def compose(self) -> ComposeResult:
        """Create the UI layout."""
        yield Header()
        yield Static("ORE", id="ore-header")
        yield SessionTable(id="ore-table")
        yield Static("PROCESSING", id="processing-header")
        yield SessionTable(id="processing-table")
        yield Static("(empty)", id="empty-processing")
        yield Footer()

    def on_mount(self) -> None:
        """Initialize when app is mounted."""
        self._projects = get_active_projects()
        self.refresh_tables()
        # Start polling for server updates
        self.set_interval(1.0, self.check_server_updates)
        # Focus the ore table initially
        self.query_one("#ore-table", SessionTable).focus()

    def check_server_updates(self) -> None:
        """Poll server's session list and refresh if needed."""
        self._update_sub_title()
        self.refresh_tables()

    def _update_sub_title(self) -> None:
        """Update sub_title with git hash and uptime."""
        parts = []
        if self._git_hash:
            parts.append(self._git_hash)
        if self._started_at:
            parts.append(format_uptime(self._started_at))
        self.sub_title = " · ".join(parts)

    def refresh_tables(self) -> None:
        """Refresh both tables from session data."""
        ore_table = self.query_one("#ore-table", SessionTable)
        processing_table = self.query_one("#processing-table", SessionTable)
        empty_label = self.query_one("#empty-processing", Static)

        # Build rows from sessions
        ore_rows = [self._build_new_row()]
        processing_rows = []

        for session in self._sessions:
            row = session_to_row(session)
            if session.stage == "ore":
                ore_rows.append(row)
            else:
                processing_rows.append(row)

        # Update ore table
        ore_table.clear()
        for row in ore_rows:
            ore_table.add_row(
                format_status_text(row.status),
                row.short_id,
                row.project,
                row.age,
                row.updated,
                self._truncate_message(row.message),
                key=row.id,
            )

        # Update processing table
        processing_table.clear()
        if processing_rows:
            empty_label.display = False
            processing_table.display = True
            for row in processing_rows:
                processing_table.add_row(
                    format_status_text(row.status),
                    row.short_id,
                    row.project,
                    row.age,
                    row.updated,
                    self._truncate_message(row.message),
                    key=row.id,
                )
        else:
            empty_label.display = True
            processing_table.display = False

    def _build_new_row(self) -> Row:
        """Build the 'new session' action row with current project."""
        if self._projects and self._selected_project_index < len(self._projects):
            project_name = self._projects[self._selected_project_index].name
        else:
            project_name = "add..."
        return new_shovel_row(project_name)

    def _truncate_message(self, message: str, max_len: int = 40) -> str:
        """Truncate message for display, replacing newlines."""
        text = message.replace("\n", " ") if message else ""
        if len(text) > max_len:
            return text[: max_len - 3] + "..."
        return text

    def _get_active_table(self) -> SessionTable:
        """Get the currently active table."""
        if self._active_table == "processing":
            table = self.query_one("#processing-table", SessionTable)
            if table.display:
                return table
        return self.query_one("#ore-table", SessionTable)

    def _get_selected_session_id(self) -> str | None:
        """Get the session ID of the selected row."""
        table = self._get_active_table()
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

    def _is_on_action_row(self) -> bool:
        """Check if cursor is on the 'new' action row."""
        if self._active_table != "ore":
            return False
        table = self.query_one("#ore-table", SessionTable)
        return table.cursor_row == 0

    def action_cursor_down(self) -> None:
        """Move cursor down, crossing table boundary if needed."""
        table = self._get_active_table()

        if self._active_table == "ore":
            # Check if at bottom of ore table
            if table.cursor_row is not None and table.cursor_row >= table.row_count - 1:
                # Try to move to processing table
                processing_table = self.query_one("#processing-table", SessionTable)
                if processing_table.display and processing_table.row_count > 0:
                    self._active_table = "processing"
                    processing_table.focus()
                    processing_table.move_cursor(row=0)
                    return
        # Normal movement within table
        table.action_cursor_down()

    def action_cursor_up(self) -> None:
        """Move cursor up, crossing table boundary if needed."""
        table = self._get_active_table()

        if self._active_table == "processing":
            # Check if at top of processing table
            if table.cursor_row == 0:
                # Move to ore table
                ore_table = self.query_one("#ore-table", SessionTable)
                self._active_table = "ore"
                ore_table.focus()
                ore_table.move_cursor(row=ore_table.row_count - 1)
                return
        # Normal movement within table
        table.action_cursor_up()

    def action_project_left(self) -> None:
        """Cycle to previous project when on action row."""
        if not self._is_on_action_row():
            return
        total_options = len(self._projects) + 1
        self._selected_project_index = (self._selected_project_index - 1) % total_options
        self.refresh_tables()

    def action_project_right(self) -> None:
        """Cycle to next project when on action row."""
        if not self._is_on_action_row():
            return
        total_options = len(self._projects) + 1
        self._selected_project_index = (self._selected_project_index + 1) % total_options
        self.refresh_tables()

    def action_select_row(self) -> None:
        """Handle Enter key on selected row."""
        session_id = self._get_selected_session_id()
        if not session_id:
            return

        if session_id == "new":
            # Create new session
            project = self.selected_project
            if not project:
                return  # No project selected (add... selected)

            session = create_session(self._sessions, project.name)
            window_id = spawn_claude(session.id, project.path)
            if window_id:
                session.tmux_window = window_id
                save_sessions(self._sessions)
            self.refresh_tables()
            return

        # Existing session - try to switch or respawn
        session = self._get_session(session_id)
        if not session:
            return

        project = find_project(session.project) if session.project else None
        project_path = project.path if project else None

        if session.tmux_window and switch_to_window(session.tmux_window):
            # Successfully switched
            pass
        else:
            # Respawn
            window_id = spawn_claude(session.id, project_path)
            if window_id:
                session.tmux_window = window_id
                save_sessions(self._sessions)

        self.refresh_tables()

    def action_archive(self) -> None:
        """Archive the selected session."""
        if self._is_on_action_row():
            return  # Can't archive action row

        session_id = self._get_selected_session_id()
        if not session_id or session_id == "new":
            return

        archive_session(self._sessions, session_id)
        self.refresh_tables()


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
