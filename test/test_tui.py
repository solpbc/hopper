"""Tests for the TUI module."""

import pytest

from hopper.projects import Project
from hopper.sessions import Session
from hopper.tui import (
    STATUS_ACTION,
    STATUS_ERROR,
    STATUS_IDLE,
    STATUS_RUNNING,
    HopperApp,
    Row,
    format_status_text,
    new_shovel_row,
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


# Tests for new_shovel_row


def test_new_shovel_row():
    """new_shovel_row creates action row with project name."""
    row = new_shovel_row("myproj")
    assert row.id == "new"
    assert row.short_id == "new"
    assert row.status == STATUS_ACTION
    assert row.project == "myproj"
    assert row.is_action is True


def test_new_shovel_row_empty_project():
    """new_shovel_row works with empty project name."""
    row = new_shovel_row()
    assert row.id == "new"
    assert row.project == ""
    assert row.is_action is True


# Tests for format_status_text


def test_format_status_text_running():
    """format_status_text returns green for running."""
    text = format_status_text(STATUS_RUNNING)
    assert str(text) == STATUS_RUNNING
    assert text.style == "green"


def test_format_status_text_error():
    """format_status_text returns red for error."""
    text = format_status_text(STATUS_ERROR)
    assert str(text) == STATUS_ERROR
    assert text.style == "red"


def test_format_status_text_action():
    """format_status_text returns cyan for action."""
    text = format_status_text(STATUS_ACTION)
    assert str(text) == STATUS_ACTION
    assert text.style == "cyan"


def test_format_status_text_idle():
    """format_status_text returns dim for idle."""
    text = format_status_text(STATUS_IDLE)
    assert str(text) == STATUS_IDLE
    assert text.style == "dim"


# Tests for Row dataclass


def test_row_dataclass():
    """Row dataclass stores all fields."""
    row = Row(
        id="test-id",
        short_id="test-sho",
        age="1m",
        updated="30s",
        status=STATUS_RUNNING,
        project="proj",
        message="Working on it",
        is_action=False,
    )
    assert row.id == "test-id"
    assert row.short_id == "test-sho"
    assert row.age == "1m"
    assert row.updated == "30s"
    assert row.status == STATUS_RUNNING
    assert row.project == "proj"
    assert row.message == "Working on it"
    assert row.is_action is False


# Tests for HopperApp


class MockServer:
    """Mock server for testing."""

    def __init__(
        self,
        sessions: list[Session] | None = None,
        git_hash: str | None = None,
        started_at: int | None = None,
    ):
        self.sessions = sessions if sessions is not None else []
        self.git_hash = git_hash
        self.started_at = started_at


@pytest.mark.asyncio
async def test_app_starts():
    """App should start and have basic structure."""
    app = HopperApp()
    async with app.run_test():
        # Should have header
        assert app.title == "HOPPER"
        # Should have ore and processing tables
        ore_table = app.query_one("#ore-table")
        assert ore_table is not None
        processing_table = app.query_one("#processing-table")
        assert processing_table is not None


@pytest.mark.asyncio
async def test_app_with_empty_sessions():
    """App should display new row when no sessions."""
    server = MockServer([])
    app = HopperApp(server=server)
    async with app.run_test():
        ore_table = app.query_one("#ore-table")
        # Should have exactly one row (the "new" action row)
        assert ore_table.row_count == 1


@pytest.mark.asyncio
async def test_app_shows_git_hash_and_uptime_in_subtitle():
    """App should show git hash and uptime in sub_title."""
    from hopper.sessions import current_time_ms

    started_at = current_time_ms() - 2 * 60 * 60_000  # 2 hours ago
    server = MockServer([], git_hash="abc1234", started_at=started_at)
    app = HopperApp(server=server)
    async with app.run_test():
        assert app.sub_title == "abc1234 · 2h"


@pytest.mark.asyncio
async def test_app_shows_uptime_only_when_no_git_hash():
    """App should show just uptime when no git hash."""
    from hopper.sessions import current_time_ms

    started_at = current_time_ms() - 15 * 60_000  # 15 minutes ago
    server = MockServer([], git_hash=None, started_at=started_at)
    app = HopperApp(server=server)
    async with app.run_test():
        assert app.sub_title == "15m"


@pytest.mark.asyncio
async def test_app_handles_no_git_hash_or_uptime():
    """App should handle missing git hash and uptime gracefully."""
    server = MockServer([], git_hash=None, started_at=None)
    app = HopperApp(server=server)
    async with app.run_test():
        assert app.sub_title == ""


@pytest.mark.asyncio
async def test_app_with_sessions():
    """App should display sessions in correct tables."""
    sessions = [
        Session(id="aaaa1111-uuid", stage="ore", created_at=1000),
        Session(id="bbbb2222-uuid", stage="processing", created_at=2000),
    ]
    server = MockServer(sessions)
    app = HopperApp(server=server)
    async with app.run_test():
        ore_table = app.query_one("#ore-table")
        processing_table = app.query_one("#processing-table")
        # Ore table: new row + 1 session
        assert ore_table.row_count == 2
        # Processing table: 1 session
        assert processing_table.row_count == 1


@pytest.mark.asyncio
async def test_quit_with_q():
    """q should quit the app."""
    app = HopperApp()
    async with app.run_test() as pilot:
        await pilot.press("q")
        # App should be exiting
        assert app._exit


@pytest.mark.asyncio
async def test_cursor_down_navigation():
    """j/down should move cursor down."""
    sessions = [
        Session(id="aaaa1111-uuid", stage="ore", created_at=1000),
        Session(id="bbbb2222-uuid", stage="ore", created_at=2000),
    ]
    server = MockServer(sessions)
    app = HopperApp(server=server)
    async with app.run_test() as pilot:
        ore_table = app.query_one("#ore-table")
        # Should start at row 0
        assert ore_table.cursor_row == 0
        # Press j to move down
        await pilot.press("j")
        assert ore_table.cursor_row == 1


@pytest.mark.asyncio
async def test_cursor_up_navigation():
    """k/up should move cursor up."""
    sessions = [
        Session(id="aaaa1111-uuid", stage="ore", created_at=1000),
        Session(id="bbbb2222-uuid", stage="ore", created_at=2000),
    ]
    server = MockServer(sessions)
    app = HopperApp(server=server)
    async with app.run_test() as pilot:
        ore_table = app.query_one("#ore-table")
        # Move down first
        await pilot.press("j")
        await pilot.press("j")
        assert ore_table.cursor_row == 2
        # Press k to move up
        await pilot.press("k")
        assert ore_table.cursor_row == 1


@pytest.mark.asyncio
async def test_cross_table_navigation_down():
    """Cursor should cross from ore to processing table."""
    sessions = [
        Session(id="aaaa1111-uuid", stage="ore", created_at=1000),
        Session(id="bbbb2222-uuid", stage="processing", created_at=2000),
    ]
    server = MockServer(sessions)
    app = HopperApp(server=server)
    async with app.run_test() as pilot:
        # Start in ore table (new row + 1 session = 2 rows)
        assert app._active_table == "ore"
        # Move to bottom of ore table
        await pilot.press("j")  # row 1
        await pilot.press("j")  # should cross to processing
        assert app._active_table == "processing"


@pytest.mark.asyncio
async def test_cross_table_navigation_up():
    """Cursor should cross from processing to ore table."""
    sessions = [
        Session(id="aaaa1111-uuid", stage="ore", created_at=1000),
        Session(id="bbbb2222-uuid", stage="processing", created_at=2000),
    ]
    server = MockServer(sessions)
    app = HopperApp(server=server)
    async with app.run_test() as pilot:
        # Navigate to processing table
        await pilot.press("j")
        await pilot.press("j")
        assert app._active_table == "processing"
        # Move up should go back to ore
        await pilot.press("k")
        assert app._active_table == "ore"


@pytest.mark.asyncio
async def test_project_cycling():
    """h/l should cycle projects on action row."""
    server = MockServer([])
    app = HopperApp(server=server)
    async with app.run_test() as pilot:
        # Inject projects after mounting (on_mount overwrites _projects)
        app._projects = [
            Project(path="/path/to/proj1", name="proj1"),
            Project(path="/path/to/proj2", name="proj2"),
        ]
        # Should start with first project
        assert app._selected_project_index == 0
        # Press l to cycle right
        await pilot.press("l")
        assert app._selected_project_index == 1
        # Press l again to go to "add..."
        await pilot.press("l")
        assert app._selected_project_index == 2
        assert app.is_add_project_selected
        # Press l to wrap to first
        await pilot.press("l")
        assert app._selected_project_index == 0
        # Press h to go back to "add..."
        await pilot.press("h")
        assert app._selected_project_index == 2


@pytest.mark.asyncio
async def test_project_cycling_only_on_action_row():
    """h/l should not cycle projects when not on action row."""
    sessions = [
        Session(id="aaaa1111-uuid", stage="ore", created_at=1000),
    ]
    server = MockServer(sessions)
    app = HopperApp(server=server)
    async with app.run_test() as pilot:
        # Inject projects after mounting
        app._projects = [
            Project(path="/path/to/proj1", name="proj1"),
            Project(path="/path/to/proj2", name="proj2"),
        ]
        # Move off action row
        await pilot.press("j")
        assert app._selected_project_index == 0
        # Press l should not change project
        await pilot.press("l")
        assert app._selected_project_index == 0


@pytest.mark.asyncio
async def test_is_on_action_row():
    """_is_on_action_row should detect action row correctly."""
    sessions = [
        Session(id="aaaa1111-uuid", stage="ore", created_at=1000),
    ]
    server = MockServer(sessions)
    app = HopperApp(server=server)
    async with app.run_test() as pilot:
        # Should start on action row
        assert app._is_on_action_row()
        # Move down
        await pilot.press("j")
        assert not app._is_on_action_row()


@pytest.mark.asyncio
async def test_selected_project():
    """selected_project should return the correct project."""
    server = MockServer([])
    app = HopperApp(server=server)
    async with app.run_test():
        # Inject projects after mounting
        app._projects = [
            Project(path="/path/to/proj1", name="proj1"),
            Project(path="/path/to/proj2", name="proj2"),
        ]
        assert app.selected_project is not None
        assert app.selected_project.name == "proj1"
        app._selected_project_index = 1
        assert app.selected_project.name == "proj2"
        # Past end = no project selected
        app._selected_project_index = 2
        assert app.selected_project is None


@pytest.mark.asyncio
async def test_get_session():
    """_get_session should find session by ID."""
    sessions = [
        Session(id="aaaa1111-uuid", stage="ore", created_at=1000),
        Session(id="bbbb2222-uuid", stage="processing", created_at=2000),
    ]
    server = MockServer(sessions)
    app = HopperApp(server=server)
    async with app.run_test():
        session = app._get_session("aaaa1111-uuid")
        assert session is not None
        assert session.id == "aaaa1111-uuid"

        session = app._get_session("nonexistent")
        assert session is None
