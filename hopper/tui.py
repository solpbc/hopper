"""TUI for managing coding agents."""

from dataclasses import dataclass, field

from blessed import Terminal

from hopper.claude import spawn_claude, switch_to_window
from hopper.sessions import (
    Session,
    archive_session,
    create_session,
    format_age,
    load_sessions,
    save_sessions,
)

# Box-drawing characters (Unicode, no emoji)
BOX_H = "─"  # horizontal line
BOX_H_BOLD = "━"  # bold horizontal line

# Status indicators (Unicode symbols, no emoji)
STATUS_RUNNING = "●"  # filled circle
STATUS_IDLE = "○"  # empty circle
STATUS_ERROR = "✗"  # x mark
STATUS_ACTION = "+"  # plus for action rows

# Column widths for table formatting
COL_STATUS = 1  # status indicator
COL_ID = 8  # short_id length
COL_AGE = 3  # "now", "3m", "4h", "2d", "1w"


@dataclass
class Row:
    """A row in a table."""

    id: str
    short_id: str
    age: str  # formatted age string
    updated: str  # formatted updated string
    status: str  # STATUS_RUNNING, STATUS_IDLE, STATUS_ERROR, or STATUS_ACTION
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
    )


def new_shovel_row() -> Row:
    """Create the 'new session' action row."""
    return Row(
        id="new",
        short_id="new session",
        age="",
        updated="",
        status=STATUS_ACTION,
        is_action=True,
    )


@dataclass
class TUIState:
    """State for the TUI."""

    sessions: list[Session] = field(default_factory=list)
    ore_rows: list[Row] = field(default_factory=lambda: [new_shovel_row()])
    processing_rows: list[Row] = field(default_factory=list)
    cursor_index: int = 0

    @property
    def total_rows(self) -> int:
        return len(self.ore_rows) + len(self.processing_rows)

    def cursor_up(self) -> "TUIState":
        new_index = (self.cursor_index - 1) % self.total_rows
        return TUIState(self.sessions, self.ore_rows, self.processing_rows, new_index)

    def cursor_down(self) -> "TUIState":
        new_index = (self.cursor_index + 1) % self.total_rows
        return TUIState(self.sessions, self.ore_rows, self.processing_rows, new_index)

    def get_selected_row(self) -> Row | None:
        """Get the currently selected row."""
        if self.cursor_index < len(self.ore_rows):
            return self.ore_rows[self.cursor_index]
        processing_index = self.cursor_index - len(self.ore_rows)
        if processing_index < len(self.processing_rows):
            return self.processing_rows[processing_index]
        return None

    def get_session(self, session_id: str) -> Session | None:
        """Get a session by ID."""
        for session in self.sessions:
            if session.id == session_id:
                return session
        return None

    def rebuild_rows(self) -> "TUIState":
        """Rebuild row lists from sessions."""
        ore_rows = [new_shovel_row()]
        processing_rows = []

        for session in self.sessions:
            row = session_to_row(session)
            if session.stage == "ore":
                ore_rows.append(row)
            else:
                processing_rows.append(row)

        # Clamp cursor to valid range
        total = len(ore_rows) + len(processing_rows)
        cursor = min(self.cursor_index, total - 1) if total > 0 else 0

        return TUIState(self.sessions, ore_rows, processing_rows, cursor)


def format_status(term: Terminal, status: str) -> str:
    """Format a status indicator with color."""
    if status == STATUS_RUNNING:
        return term.green(status)
    elif status == STATUS_ERROR:
        return term.red(status)
    elif status == STATUS_ACTION:
        return term.cyan(status)
    else:  # STATUS_IDLE
        return term.dim + status + term.normal


def format_row(term: Terminal, row: Row, width: int) -> str:
    """Format a row for display.

    Args:
        term: Terminal for color formatting
        row: Row data to format
        width: Available width for the row content (excluding cursor prefix)

    Returns a string like:
      "● abcd1234   now   now"
      "+ new session"
    """
    status_str = format_status(term, row.status)

    if row.is_action:
        return f"{status_str} {row.short_id}"

    # Build columns: status, id, age, updated
    # Format: "● abcd1234   now   now"
    id_part = row.short_id.ljust(COL_ID)
    age_part = row.age.rjust(COL_AGE) if row.age else "".rjust(COL_AGE)
    updated_part = row.updated.rjust(COL_AGE) if row.updated else "".rjust(COL_AGE)

    return f"{status_str} {id_part}  {age_part}  {updated_part}"


def render_line(term: Terminal, width: int, char: str = BOX_H) -> str:
    """Render a horizontal line of the given width."""
    return char * width


def render_header(term: Terminal, width: int) -> None:
    """Render the title header."""
    title = " HOPPER "
    # Center the title in the line
    line_len = width - len(title)
    left = line_len // 2
    right = line_len - left
    print(term.bold(BOX_H_BOLD * left + title + BOX_H_BOLD * right))
    print()


def render_table_header(term: Terminal, title: str, width: int) -> None:
    """Render a table section header with column labels."""
    # Table title
    print(term.bold(title))
    # Column headers: aligned with data columns
    # "  ● ID        AGE   UPD"
    header = f"    {'ID'.ljust(COL_ID)}  {'AGE'.rjust(COL_AGE)}  {'UPD'.rjust(COL_AGE)}"
    print(term.dim + header + term.normal)


def render_footer(term: Terminal, width: int) -> None:
    """Render the footer with keybindings."""
    print()
    print(render_line(term, width))
    hints = " ↑↓/jk Navigate  ⏎ Select  a Archive  q Quit"
    print(term.dim + hints + term.normal)


def render(term: Terminal, state: TUIState) -> None:
    """Render the TUI to the terminal."""
    width = term.width or 40  # Fallback for tests

    print(term.home + term.clear, end="")

    # Header
    render_header(term, width)

    row_num = 0

    # ORE table
    render_table_header(term, "ORE", width)
    for row in state.ore_rows:
        line = format_row(term, row, width - 2)  # -2 for cursor prefix
        if row_num == state.cursor_index:
            print(term.reverse(f"> {line}"))
        else:
            print(f"  {line}")
        row_num += 1

    # Spacing between tables
    print()

    # PROCESSING table
    render_table_header(term, "PROCESSING", width)
    if state.processing_rows:
        for row in state.processing_rows:
            line = format_row(term, row, width - 2)
            if row_num == state.cursor_index:
                print(term.reverse(f"> {line}"))
            else:
                print(f"  {line}")
            row_num += 1
    else:
        print(term.dim + "    (empty)" + term.normal)

    # Footer
    render_footer(term, width)


def handle_enter(state: TUIState) -> TUIState:
    """Handle Enter key press on the selected row."""
    row = state.get_selected_row()
    if not row:
        return state

    if row.id == "new":
        # Create a new session (create_session saves to disk with timestamps)
        session = create_session(state.sessions)
        session.state = "running"
        session.touch()

        # Spawn claude
        window_id = spawn_claude(session.id, resume=False)
        if window_id:
            session.tmux_window = window_id
        else:
            session.state = "error"

        # Save state/window updates (create_session already saved the initial session)
        save_sessions(state.sessions)
        return state.rebuild_rows()

    # Existing session - try to switch or respawn
    session = state.get_session(row.id)
    if not session:
        return state

    # Try to switch to existing window
    if session.tmux_window and switch_to_window(session.tmux_window):
        # Successfully switched - ensure state reflects running
        if session.state != "running":
            session.state = "running"
            session.touch()
            save_sessions(state.sessions)
    else:
        # Window doesn't exist or switch failed - respawn claude with resume
        session.state = "running"
        session.touch()
        window_id = spawn_claude(session.id, resume=True)
        if window_id:
            session.tmux_window = window_id
        else:
            session.state = "error"
        save_sessions(state.sessions)

    return state.rebuild_rows()


def handle_archive(state: TUIState) -> TUIState:
    """Handle 'a' key press to archive the selected session."""
    row = state.get_selected_row()
    if not row or row.is_action:
        # Can't archive action rows
        return state

    # Archive the session (removes from list and persists)
    archive_session(state.sessions, row.id)
    return state.rebuild_rows()


def run_tui(term: Terminal, server=None) -> int:
    """Run the TUI main loop.

    Args:
        term: blessed Terminal instance
        server: Optional Server instance. If provided, uses server's session list
                for shared state. Otherwise loads from disk.
    """
    # Use server's session list if available, otherwise load from disk
    if server is not None:
        sessions = server.sessions
    else:
        sessions = load_sessions()

    # Build initial state
    state = TUIState(sessions=sessions)
    state = state.rebuild_rows()

    with term.fullscreen(), term.cbreak(), term.hidden_cursor():
        render(term, state)

        while True:
            key = term.inkey()

            if key.name == "KEY_UP" or key == "k":
                state = state.cursor_up()
            elif key.name == "KEY_DOWN" or key == "j":
                state = state.cursor_down()
            elif key.name == "KEY_ENTER" or key == "\n" or key == "\r":
                state = handle_enter(state)
            elif key == "a":
                state = handle_archive(state)
            elif key == "q" or key.name == "KEY_ESCAPE":
                break

            render(term, state)

    return 0
