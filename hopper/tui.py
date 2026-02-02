"""TUI for managing coding agents."""

from dataclasses import dataclass, field

from blessed import Terminal

from hopper.claude import spawn_claude, switch_to_window
from hopper.sessions import Session, create_session, load_sessions, save_sessions


@dataclass
class Row:
    """A row in a table."""

    id: str
    label: str


def truncate_id(session_id: str, all_ids: list[str], min_len: int = 4) -> str:
    """Truncate a session ID to the shortest unique prefix.

    Uses the first segment of the UUID (before the first dash) and finds
    the minimum length that's unique among all IDs.
    """
    # Use the first segment of the UUID (8 hex chars)
    first_segment = session_id.split("-")[0]
    other_segments = [s.split("-")[0] for s in all_ids if s != session_id]

    # Find minimum unique prefix
    for length in range(min_len, len(first_segment) + 1):
        prefix = first_segment[:length]
        if not any(other.startswith(prefix) for other in other_segments):
            return prefix

    return first_segment


def session_label(session: Session, all_sessions: list[Session]) -> str:
    """Generate a display label for a session."""
    all_ids = [s.id for s in all_sessions]
    short_id = truncate_id(session.id, all_ids)

    if session.state == "error":
        return f"{short_id} (error)"
    elif session.state == "running":
        return f"{short_id} (running)"
    else:
        return short_id


@dataclass
class TUIState:
    """State for the TUI."""

    sessions: list[Session] = field(default_factory=list)
    ore_rows: list[Row] = field(default_factory=lambda: [Row("new", "new shovel")])
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
        ore_rows = [Row("new", "new shovel")]
        processing_rows = []

        for session in self.sessions:
            row = Row(session.id, session_label(session, self.sessions))
            if session.stage == "ore":
                ore_rows.append(row)
            else:
                processing_rows.append(row)

        # Clamp cursor to valid range
        total = len(ore_rows) + len(processing_rows)
        cursor = min(self.cursor_index, total - 1) if total > 0 else 0

        return TUIState(self.sessions, ore_rows, processing_rows, cursor)


def render(term: Terminal, state: TUIState) -> None:
    """Render the TUI to the terminal."""
    print(term.home + term.clear, end="")

    row_num = 0

    # ORE table
    print(term.bold("ORE"))
    print()
    for row in state.ore_rows:
        if row_num == state.cursor_index:
            print(term.reverse(f"> {row.label}"))
        else:
            print(f"  {row.label}")
        row_num += 1

    # Spacing between tables
    print()
    print()

    # PROCESSING table
    print(term.bold("PROCESSING"))
    print()
    if state.processing_rows:
        for row in state.processing_rows:
            if row_num == state.cursor_index:
                print(term.reverse(f"> {row.label}"))
            else:
                print(f"  {row.label}")
            row_num += 1
    else:
        print(term.dim("  (empty)"))


def handle_enter(state: TUIState) -> TUIState:
    """Handle Enter key press on the selected row."""
    row = state.get_selected_row()
    if not row:
        return state

    if row.id == "new":
        # Create a new session (create_session saves to disk)
        session = create_session(state.sessions)
        session.state = "running"

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
            save_sessions(state.sessions)
    else:
        # Window doesn't exist or switch failed - respawn claude with resume
        session.state = "running"
        window_id = spawn_claude(session.id, resume=True)
        if window_id:
            session.tmux_window = window_id
        else:
            session.state = "error"
        save_sessions(state.sessions)

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
            elif key == "q" or key.name == "KEY_ESCAPE":
                break

            render(term, state)

    return 0
