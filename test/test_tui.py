"""Tests for the TUI module."""

from unittest.mock import MagicMock

from hopper.sessions import Session
from hopper.tui import (
    STATUS_ACTION,
    STATUS_ERROR,
    STATUS_IDLE,
    STATUS_RUNNING,
    Row,
    TUIState,
    format_row,
    handle_archive,
    new_shovel_row,
    render,
    session_to_row,
)

# Tests for session_to_row


def test_session_to_row_idle():
    """Idle session has idle status indicator."""
    session = Session(id="abcd1234-5678-uuid", stage="ore", created_at=1000, state="idle")
    row = session_to_row(session)
    assert row.short_id == "abcd1234"
    assert row.status == STATUS_IDLE


def test_session_to_row_running():
    """Running session has running status indicator."""
    session = Session(id="abcd1234-5678-uuid", stage="ore", created_at=1000, state="running")
    row = session_to_row(session)
    assert row.short_id == "abcd1234"
    assert row.status == STATUS_RUNNING


def test_session_to_row_error():
    """Error session has error status indicator."""
    session = Session(id="abcd1234-5678-uuid", stage="ore", created_at=1000, state="error")
    row = session_to_row(session)
    assert row.short_id == "abcd1234"
    assert row.status == STATUS_ERROR


# Tests for TUIState


def _make_row(id: str, status: str = STATUS_IDLE) -> Row:
    """Helper to create a test row."""
    return Row(id=id, short_id=id[:8], age="1m", updated="1m", status=status)


def test_tuistate_default():
    """Default state has new session row and cursor at 0."""
    state = TUIState()
    assert len(state.ore_rows) == 1
    assert state.ore_rows[0].id == "new"
    assert state.ore_rows[0].is_action is True
    assert state.ore_rows[0].status == STATUS_ACTION
    assert state.cursor_index == 0


def test_tuistate_total_rows():
    """total_rows counts both tables."""
    state = TUIState(
        ore_rows=[new_shovel_row(), _make_row("a")],
        processing_rows=[_make_row("b"), _make_row("c")],
    )
    assert state.total_rows == 4


def test_tuistate_cursor_up():
    """cursor_up wraps around."""
    state = TUIState(ore_rows=[_make_row("a"), _make_row("b")], cursor_index=0)
    new_state = state.cursor_up()
    assert new_state.cursor_index == 1  # Wrapped to end


def test_tuistate_cursor_down():
    """cursor_down wraps around."""
    state = TUIState(ore_rows=[_make_row("a"), _make_row("b")], cursor_index=1)
    new_state = state.cursor_down()
    assert new_state.cursor_index == 0  # Wrapped to start


def test_tuistate_get_selected_row_ore():
    """get_selected_row returns correct ore row."""
    state = TUIState(
        ore_rows=[new_shovel_row(), _make_row("a")],
        cursor_index=1,
    )
    row = state.get_selected_row()
    assert row is not None
    assert row.id == "a"


def test_tuistate_get_selected_row_processing():
    """get_selected_row returns correct processing row."""
    state = TUIState(
        ore_rows=[new_shovel_row()],
        processing_rows=[_make_row("b")],
        cursor_index=1,
    )
    row = state.get_selected_row()
    assert row is not None
    assert row.id == "b"


def test_tuistate_get_session():
    """get_session finds session by ID."""
    session = Session(id="test-id", stage="ore", created_at=1000)
    state = TUIState(sessions=[session])
    result = state.get_session("test-id")
    assert result is session


def test_tuistate_get_session_not_found():
    """get_session returns None for unknown ID."""
    state = TUIState(sessions=[])
    result = state.get_session("nonexistent")
    assert result is None


def test_tuistate_rebuild_rows():
    """rebuild_rows creates rows from sessions."""
    sessions = [
        Session(id="ore-1", stage="ore", created_at=1000),
        Session(id="proc-1", stage="processing", created_at=2000),
        Session(id="ore-2", stage="ore", created_at=3000),
    ]
    state = TUIState(sessions=sessions)
    rebuilt = state.rebuild_rows()

    # New shovel + 2 ore sessions
    assert len(rebuilt.ore_rows) == 3
    assert rebuilt.ore_rows[0].id == "new"
    assert rebuilt.ore_rows[1].id == "ore-1"
    assert rebuilt.ore_rows[2].id == "ore-2"

    # 1 processing session
    assert len(rebuilt.processing_rows) == 1
    assert rebuilt.processing_rows[0].id == "proc-1"


def test_tuistate_rebuild_rows_clamps_cursor():
    """rebuild_rows clamps cursor to valid range."""
    state = TUIState(
        sessions=[],
        ore_rows=[new_shovel_row(), _make_row("deleted")],
        cursor_index=1,
    )
    # After rebuild, only "new shovel" remains, cursor should clamp to 0
    rebuilt = state.rebuild_rows()
    assert rebuilt.cursor_index == 0


# Tests for render


def _mock_terminal(width: int = 40):
    """Create a mock Terminal that returns strings for capabilities."""
    term = MagicMock()
    term.home = "[HOME]"
    term.clear = "[CLEAR]"
    term.normal = "[NORMAL]"
    term.dim = "[DIM]"
    term.width = width
    term.bold = lambda s: f"[BOLD]{s}[/BOLD]"
    term.reverse = lambda s: f"[REV]{s}[/REV]"
    term.green = lambda s: f"[GREEN]{s}[/GREEN]"
    term.red = lambda s: f"[RED]{s}[/RED]"
    term.cyan = lambda s: f"[CYAN]{s}[/CYAN]"
    return term


def test_render_empty_processing(capsys):
    """render shows header, tables, and footer."""
    term = _mock_terminal()
    state = TUIState()
    state = state.rebuild_rows()

    render(term, state)

    captured = capsys.readouterr()
    # Header
    assert "HOPPER" in captured.out
    # Tables
    assert "[BOLD]ORE[/BOLD]" in captured.out
    assert "[BOLD]PROCESSING[/BOLD]" in captured.out
    assert "(empty)" in captured.out
    # Footer
    assert "Navigate" in captured.out
    assert "Archive" in captured.out
    assert "Quit" in captured.out


def test_render_with_sessions(capsys):
    """render shows sessions in correct tables."""
    term = _mock_terminal()
    sessions = [
        Session(id="aaaa1111-uuid", stage="ore", created_at=1000),
        Session(id="bbbb2222-uuid", stage="processing", created_at=2000),
    ]
    state = TUIState(sessions=sessions)
    state = state.rebuild_rows()

    render(term, state)

    captured = capsys.readouterr()
    # Action row with + indicator (cyan)
    assert "[CYAN]+[/CYAN]" in captured.out
    assert "new session" in captured.out
    # Sessions with status indicators
    assert "aaaa1111" in captured.out
    assert "bbbb2222" in captured.out


def test_render_cursor_on_session(capsys):
    """render highlights the selected row."""
    term = _mock_terminal()
    sessions = [Session(id="aaaa1111-uuid", stage="ore", created_at=1000)]
    state = TUIState(sessions=sessions, cursor_index=1)
    state = state.rebuild_rows()

    render(term, state)

    captured = capsys.readouterr()
    # Action row not selected (no >)
    assert "  [CYAN]+[/CYAN] new session" in captured.out
    # Session row selected
    assert "[REV]>" in captured.out
    assert "aaaa1111" in captured.out


def test_render_cursor_on_processing(capsys):
    """render highlights processing row when selected."""
    term = _mock_terminal()
    sessions = [Session(id="bbbb2222-uuid", stage="processing", created_at=1000)]
    state = TUIState(sessions=sessions, cursor_index=1)
    state = state.rebuild_rows()

    render(term, state)

    captured = capsys.readouterr()
    assert "[REV]>" in captured.out
    assert "bbbb2222" in captured.out


# Tests for format_row


def test_format_row_action():
    """format_row for action row shows + and label."""
    term = _mock_terminal()
    row = new_shovel_row()
    result = format_row(term, row, 40)
    assert "[CYAN]+[/CYAN]" in result
    assert "new session" in result


def test_format_row_session_running():
    """format_row formats running session with green indicator."""
    term = _mock_terminal()
    row = Row(id="test-id", short_id="abcd1234", age="3m", updated="1m", status=STATUS_RUNNING)
    result = format_row(term, row, 40)
    assert "[GREEN]●[/GREEN]" in result
    assert "abcd1234" in result
    assert "3m" in result
    assert "1m" in result


def test_format_row_session_error():
    """format_row formats error session with red indicator."""
    term = _mock_terminal()
    row = Row(id="test-id", short_id="abcd1234", age="3m", updated="1m", status=STATUS_ERROR)
    result = format_row(term, row, 40)
    assert "[RED]✗[/RED]" in result
    assert "abcd1234" in result


def test_format_row_session_idle():
    """format_row formats idle session with dim indicator."""
    term = _mock_terminal()
    row = Row(id="test-id", short_id="abcd1234", age="2h", updated="1h", status=STATUS_IDLE)
    result = format_row(term, row, 40)
    assert "[DIM]○[NORMAL]" in result
    assert "abcd1234" in result
    assert "2h" in result
    assert "1h" in result


# Tests for handle_archive


def test_handle_archive_removes_session():
    """handle_archive removes the selected session."""
    sessions = [
        Session(
            id="keep-id",
            stage="ore",
            created_at=1000,
            updated_at=1000,
            state="idle",
            tmux_window=None,
        ),
        Session(
            id="archive-id",
            stage="ore",
            created_at=2000,
            updated_at=2000,
            state="idle",
            tmux_window=None,
        ),
    ]
    state = TUIState(sessions=sessions, cursor_index=2)  # cursor on archive-id (after new + keep)
    state = state.rebuild_rows()

    new_state = handle_archive(state)

    # Session should be removed from list
    assert len(new_state.sessions) == 1
    assert new_state.sessions[0].id == "keep-id"


def test_handle_archive_ignores_action_row():
    """handle_archive does nothing when action row is selected."""
    sessions = [
        Session(
            id="test-id",
            stage="ore",
            created_at=1000,
            updated_at=1000,
            state="idle",
            tmux_window=None,
        ),
    ]
    state = TUIState(sessions=sessions, cursor_index=0)  # cursor on "new session" action
    state = state.rebuild_rows()

    new_state = handle_archive(state)

    # Session should still be there
    assert len(new_state.sessions) == 1
