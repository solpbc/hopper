"""Tests for the TUI module."""

import pytest
from textual.app import App

from hopper.projects import Project
from hopper.sessions import Session
from hopper.tui import (
    STAGE_ORE,
    STAGE_PROCESSING,
    STATUS_ERROR,
    STATUS_NEW,
    STATUS_RUNNING,
    STATUS_STUCK,
    BacklogInputScreen,
    HopperApp,
    LegendScreen,
    ProjectPickerScreen,
    Row,
    ScopeInputScreen,
    format_active_text,
    format_stage_text,
    format_status_text,
    session_to_row,
)

# Tests for session_to_row


def test_session_to_row_new():
    """New session has new status indicator."""
    session = Session(id="abcd1234-5678-uuid", stage="ore", created_at=1000, state="new")
    row = session_to_row(session)
    assert row.short_id == "abcd1234"
    assert row.status == STATUS_NEW
    assert row.stage == STAGE_ORE


def test_session_to_row_running():
    """Running session has running status indicator."""
    session = Session(id="abcd1234-5678-uuid", stage="ore", created_at=1000, state="running")
    row = session_to_row(session)
    assert row.short_id == "abcd1234"
    assert row.status == STATUS_RUNNING
    assert row.stage == STAGE_ORE


def test_session_to_row_stuck():
    """Stuck session has stuck status indicator."""
    session = Session(id="abcd1234-5678-uuid", stage="ore", created_at=1000, state="stuck")
    row = session_to_row(session)
    assert row.short_id == "abcd1234"
    assert row.status == STATUS_STUCK
    assert row.stage == STAGE_ORE


def test_session_to_row_error():
    """Error session has error status indicator."""
    session = Session(id="abcd1234-5678-uuid", stage="ore", created_at=1000, state="error")
    row = session_to_row(session)
    assert row.short_id == "abcd1234"
    assert row.status == STATUS_ERROR
    assert row.stage == STAGE_ORE


def test_session_to_row_active():
    """Active session has active=True in row."""
    session = Session(
        id="abcd1234-5678-uuid", stage="ore", created_at=1000, state="running", active=True
    )
    row = session_to_row(session)
    assert row.active is True


def test_session_to_row_inactive():
    """Inactive session has active=False in row."""
    session = Session(id="abcd1234-5678-uuid", stage="ore", created_at=1000, state="new")
    row = session_to_row(session)
    assert row.active is False


def test_session_to_row_processing_stage():
    """Processing session has gear stage indicator."""
    session = Session(id="abcd1234-5678-uuid", stage="processing", created_at=1000, state="new")
    row = session_to_row(session)
    assert row.stage == STAGE_PROCESSING


def test_session_to_row_completed():
    """Completed session shows running indicator (transient state)."""
    session = Session(id="abcd1234-5678-uuid", stage="ore", created_at=1000, state="completed")
    row = session_to_row(session)
    assert row.status == STATUS_RUNNING


def test_session_to_row_ready():
    """Ready session shows running indicator (active work)."""
    session = Session(id="abcd1234-5678-uuid", stage="processing", created_at=1000, state="ready")
    row = session_to_row(session)
    assert row.status == STATUS_RUNNING


def test_session_to_row_task_state():
    """Task-name state shows running indicator (active work)."""
    session = Session(id="abcd1234-5678-uuid", stage="processing", created_at=1000, state="audit")
    row = session_to_row(session)
    assert row.status == STATUS_RUNNING


# Tests for format_status_text


def test_format_status_text_running():
    """format_status_text returns bright_green for running."""
    text = format_status_text(STATUS_RUNNING)
    assert str(text) == STATUS_RUNNING
    assert text.style == "bright_green"


def test_format_status_text_stuck():
    """format_status_text returns bright_yellow for stuck."""
    text = format_status_text(STATUS_STUCK)
    assert str(text) == STATUS_STUCK
    assert text.style == "bright_yellow"


def test_format_status_text_error():
    """format_status_text returns bright_red for error."""
    text = format_status_text(STATUS_ERROR)
    assert str(text) == STATUS_ERROR
    assert text.style == "bright_red"


def test_format_status_text_new():
    """format_status_text returns bright_black for new."""
    text = format_status_text(STATUS_NEW)
    assert str(text) == STATUS_NEW
    assert text.style == "bright_black"


# Tests for format_active_text


def test_format_active_text_active():
    """format_active_text returns bright_cyan for active."""
    text = format_active_text(True)
    assert str(text) == "▸"
    assert text.style == "bright_cyan"


def test_format_active_text_inactive():
    """format_active_text returns bright_black for inactive."""
    text = format_active_text(False)
    assert str(text) == "▹"
    assert text.style == "bright_black"


# Tests for format_stage_text


def test_format_stage_text_ore():
    """format_stage_text returns bright_blue for ore."""
    text = format_stage_text(STAGE_ORE)
    assert str(text) == STAGE_ORE
    assert text.style == "bright_blue"


def test_format_stage_text_processing():
    """format_stage_text returns bright_yellow for processing."""
    text = format_stage_text(STAGE_PROCESSING)
    assert str(text) == STAGE_PROCESSING
    assert text.style == "bright_yellow"


# Tests for Row dataclass


def test_row_dataclass():
    """Row dataclass stores all fields."""
    row = Row(
        id="test-id",
        short_id="test-sho",
        stage=STAGE_ORE,
        age="1m",
        status=STATUS_RUNNING,
        project="proj",
        status_text="Working on it",
    )
    assert row.id == "test-id"
    assert row.short_id == "test-sho"
    assert row.stage == STAGE_ORE
    assert row.age == "1m"
    assert row.status == STATUS_RUNNING
    assert row.project == "proj"
    assert row.status_text == "Working on it"


# Tests for HopperApp


class MockServer:
    """Mock server for testing."""

    def __init__(
        self,
        sessions: list[Session] | None = None,
        backlog: list | None = None,
        git_hash: str | None = None,
        started_at: int | None = None,
    ):
        self.sessions = sessions if sessions is not None else []
        self.backlog = backlog if backlog is not None else []
        self.git_hash = git_hash
        self.started_at = started_at


@pytest.mark.asyncio
async def test_app_starts():
    """App should start and have basic structure."""
    app = HopperApp()
    async with app.run_test():
        # Should have header
        assert app.title == "HOPPER"
        # Should have unified session table
        table = app.query_one("#session-table")
        assert table is not None


@pytest.mark.asyncio
async def test_app_with_empty_sessions():
    """App should show hint row when no sessions."""
    server = MockServer([])
    app = HopperApp(server=server)
    async with app.run_test():
        table = app.query_one("#session-table")
        # Table always visible, hint row present
        assert table.display is True
        assert table.row_count == 1  # hint row only


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
    """App should display all sessions in unified table."""
    sessions = [
        Session(id="aaaa1111-uuid", stage="ore", created_at=1000),
        Session(id="bbbb2222-uuid", stage="processing", created_at=2000),
    ]
    server = MockServer(sessions)
    app = HopperApp(server=server)
    async with app.run_test():
        table = app.query_one("#session-table")
        # 2 sessions + 1 hint row
        assert table.row_count == 3


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
        table = app.query_one("#session-table")
        # Should start at row 0
        assert table.cursor_row == 0
        # Press j to move down
        await pilot.press("j")
        assert table.cursor_row == 1


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
        table = app.query_one("#session-table")
        # Move down first
        await pilot.press("j")
        assert table.cursor_row == 1
        # Press k to move up
        await pilot.press("k")
        assert table.cursor_row == 0


@pytest.mark.asyncio
async def test_cursor_preserved_after_refresh():
    """Cursor position should be preserved when table is refreshed."""
    sessions = [
        Session(id="aaaa1111-uuid", stage="ore", created_at=1000),
        Session(id="bbbb2222-uuid", stage="ore", created_at=2000),
        Session(id="cccc3333-uuid", stage="ore", created_at=3000),
    ]
    server = MockServer(sessions)
    app = HopperApp(server=server)
    async with app.run_test() as pilot:
        table = app.query_one("#session-table")
        # Move to row 2
        await pilot.press("j")
        await pilot.press("j")
        assert table.cursor_row == 2
        # Refresh table (simulates polling update)
        app.refresh_table()
        # Cursor should still be at row 2
        assert table.cursor_row == 2


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


# Tests for ProjectPickerScreen


class PickerTestApp(App):
    """Test app wrapper for ProjectPickerScreen."""

    def __init__(self, projects: list[Project]):
        super().__init__()
        self._projects = projects
        self.picker_result = "not_set"  # sentinel value

    def on_mount(self) -> None:
        def capture_result(r):
            self.picker_result = r

        self.push_screen(ProjectPickerScreen(self._projects), capture_result)


@pytest.mark.asyncio
async def test_project_picker_displays_projects():
    """ProjectPickerScreen should display all projects."""
    from textual.widgets import OptionList

    projects = [
        Project(path="/path/to/proj1", name="proj1"),
        Project(path="/path/to/proj2", name="proj2"),
    ]
    app = PickerTestApp(projects)
    async with app.run_test():
        # Query through the active screen
        screen = app.screen
        option_list = screen.query_one("#project-list", OptionList)
        assert option_list.option_count == 2


@pytest.mark.asyncio
async def test_project_picker_cancel():
    """Escape should dismiss the project picker with None result."""
    projects = [Project(path="/path/to/proj1", name="proj1")]
    app = PickerTestApp(projects)
    async with app.run_test() as pilot:
        await pilot.press("escape")
        assert app.picker_result is None


@pytest.mark.asyncio
async def test_project_picker_select():
    """Enter should select the highlighted project."""
    projects = [
        Project(path="/path/to/proj1", name="proj1"),
        Project(path="/path/to/proj2", name="proj2"),
    ]
    app = PickerTestApp(projects)
    async with app.run_test() as pilot:
        await pilot.press("enter")
        assert app.picker_result is not None
        assert app.picker_result.name == "proj1"


@pytest.mark.asyncio
async def test_project_picker_navigation():
    """j/k should navigate the project list."""
    from textual.widgets import OptionList

    projects = [
        Project(path="/path/to/proj1", name="proj1"),
        Project(path="/path/to/proj2", name="proj2"),
    ]
    app = PickerTestApp(projects)
    async with app.run_test() as pilot:
        # Query through the active screen
        screen = app.screen
        option_list = screen.query_one("#project-list", OptionList)
        # Should start at 0
        assert option_list.highlighted == 0
        # Move down
        await pilot.press("j")
        assert option_list.highlighted == 1
        # Move back up
        await pilot.press("k")
        assert option_list.highlighted == 0


# Tests for ScopeInputScreen


class ScopeTestApp(App):
    """Test app wrapper for ScopeInputScreen."""

    def __init__(self):
        super().__init__()
        self.scope_result = "not_set"  # sentinel value

    def on_mount(self) -> None:
        def capture_result(r):
            self.scope_result = r

        self.push_screen(ScopeInputScreen(), capture_result)


@pytest.mark.asyncio
async def test_scope_input_cancel_escape():
    """Escape should dismiss the scope input with None result."""
    app = ScopeTestApp()
    async with app.run_test() as pilot:
        await pilot.press("escape")
        assert app.scope_result is None


@pytest.mark.asyncio
async def test_scope_input_cancel_button():
    """Cancel button should dismiss the scope input with None result."""

    app = ScopeTestApp()
    async with app.run_test() as pilot:
        # Tab to Cancel button (first button after TextArea)
        await pilot.press("tab")
        # Press enter to activate
        await pilot.press("enter")
        assert app.scope_result is None


@pytest.mark.asyncio
async def test_scope_input_foreground():
    """Foreground button should return scope and True."""
    from textual.widgets import TextArea

    app = ScopeTestApp()
    async with app.run_test() as pilot:
        # Type some text
        screen = app.screen
        text_area = screen.query_one(TextArea)
        text_area.insert("Test task scope")
        # Tab to Foreground button (third button)
        await pilot.press("tab")  # Cancel
        await pilot.press("tab")  # Background
        await pilot.press("tab")  # Foreground
        await pilot.press("enter")
        assert app.scope_result == ("Test task scope", True)


@pytest.mark.asyncio
async def test_scope_input_background():
    """Background button should return scope and False."""
    from textual.widgets import TextArea

    app = ScopeTestApp()
    async with app.run_test() as pilot:
        # Type some text
        screen = app.screen
        text_area = screen.query_one(TextArea)
        text_area.insert("Test task scope")
        # Tab to Background button (second button)
        await pilot.press("tab")  # Cancel
        await pilot.press("tab")  # Background
        await pilot.press("enter")
        assert app.scope_result == ("Test task scope", False)


@pytest.mark.asyncio
async def test_scope_input_empty_validation():
    """Empty scope should not submit - result stays as sentinel."""
    app = ScopeTestApp()
    async with app.run_test() as pilot:
        # Tab to Foreground button without typing anything
        await pilot.press("tab")  # Cancel
        await pilot.press("tab")  # Background
        await pilot.press("tab")  # Foreground
        await pilot.press("enter")
        # Should not have dismissed - still sentinel
        assert app.scope_result == "not_set"


@pytest.mark.asyncio
async def test_scope_input_arrow_keys_navigate_buttons():
    """Left/right arrows should cycle focus between buttons."""
    app = ScopeTestApp()
    async with app.run_test() as pilot:
        # Tab from TextArea to first button (Cancel)
        await pilot.press("tab")
        assert app.screen.focused.id == "btn-cancel"
        # Right arrow to Background
        await pilot.press("right")
        assert app.screen.focused.id == "btn-background"
        # Right arrow to Foreground
        await pilot.press("right")
        assert app.screen.focused.id == "btn-foreground"
        # Right arrow wraps to Cancel
        await pilot.press("right")
        assert app.screen.focused.id == "btn-cancel"
        # Left arrow wraps to Foreground
        await pilot.press("left")
        assert app.screen.focused.id == "btn-foreground"
        # Left arrow to Background
        await pilot.press("left")
        assert app.screen.focused.id == "btn-background"


@pytest.mark.asyncio
async def test_scope_input_shift_tab_returns_to_textarea():
    """Shift+Tab from first button should return focus to TextArea."""
    from textual.widgets import TextArea

    app = ScopeTestApp()
    async with app.run_test() as pilot:
        # Tab to first button
        await pilot.press("tab")
        assert app.screen.focused.id == "btn-cancel"
        # Shift+Tab back to TextArea
        await pilot.press("shift+tab")
        assert isinstance(app.screen.focused, TextArea)


@pytest.mark.asyncio
async def test_scope_input_shift_tab_between_buttons():
    """Shift+Tab should move backwards through buttons."""
    app = ScopeTestApp()
    async with app.run_test() as pilot:
        # Tab to Foreground (third button)
        await pilot.press("tab")  # Cancel
        await pilot.press("tab")  # Background
        await pilot.press("tab")  # Foreground
        assert app.screen.focused.id == "btn-foreground"
        # Shift+Tab back to Background
        await pilot.press("shift+tab")
        assert app.screen.focused.id == "btn-background"
        # Shift+Tab back to Cancel
        await pilot.press("shift+tab")
        assert app.screen.focused.id == "btn-cancel"


@pytest.mark.asyncio
async def test_scope_input_arrow_key_select():
    """Arrow to a button then Enter should activate it."""
    from textual.widgets import TextArea

    app = ScopeTestApp()
    async with app.run_test() as pilot:
        screen = app.screen
        text_area = screen.query_one(TextArea)
        text_area.insert("Arrow test")
        # Tab to Cancel, then right twice to Foreground
        await pilot.press("tab")
        await pilot.press("right")
        await pilot.press("right")
        assert app.screen.focused.id == "btn-foreground"
        await pilot.press("enter")
        assert app.scope_result == ("Arrow test", True)


# Tests for hint rows


@pytest.mark.asyncio
async def test_hint_row_stays_highlighted():
    """Cursor should stay on hint row across refresh cycles."""
    sessions = [
        Session(id="aaaa1111-uuid", stage="ore", created_at=1000),
    ]
    server = MockServer(sessions)
    app = HopperApp(server=server)
    async with app.run_test() as pilot:
        table = app.query_one("#session-table")
        # Move to hint row (row 1, after the one session)
        await pilot.press("j")
        assert table.cursor_row == 1
        # Simulate polling refresh
        app.refresh_table()
        # Cursor should still be on hint row
        assert table.cursor_row == 1


@pytest.mark.asyncio
async def test_enter_on_session_hint_triggers_new_session():
    """Enter on session hint row should trigger new session action."""
    sessions = [
        Session(id="aaaa1111-uuid", stage="ore", created_at=1000),
    ]
    server = MockServer(sessions)
    app = HopperApp(server=server)
    async with app.run_test() as pilot:
        called = []
        app.action_new_session = lambda: called.append(True)
        # Move to hint row and press enter
        await pilot.press("j")
        await pilot.press("enter")
        assert len(called) == 1


@pytest.mark.asyncio
async def test_enter_on_backlog_hint_triggers_new_backlog():
    """Enter on backlog hint row should trigger new backlog action."""
    from hopper.backlog import BacklogItem

    items = [
        BacklogItem(id="bl-1111-uuid", project="proj", description="Item", created_at=1000),
    ]
    server = MockServer([], backlog=items)
    app = HopperApp(server=server)
    async with app.run_test() as pilot:
        called = []
        app.action_new_backlog = lambda: called.append(True)
        # Switch to backlog, move to hint row
        await pilot.press("tab")
        await pilot.press("j")
        await pilot.press("enter")
        assert len(called) == 1


# Tests for BacklogInputScreen


class BacklogInputTestApp(App):
    """Test app wrapper for BacklogInputScreen."""

    def __init__(self):
        super().__init__()
        self.backlog_result = "not_set"  # sentinel value

    def on_mount(self) -> None:
        def capture_result(r):
            self.backlog_result = r

        self.push_screen(BacklogInputScreen(), capture_result)


@pytest.mark.asyncio
async def test_backlog_input_cancel_escape():
    """Escape should dismiss the backlog input with None result."""
    app = BacklogInputTestApp()
    async with app.run_test() as pilot:
        await pilot.press("escape")
        assert app.backlog_result is None


@pytest.mark.asyncio
async def test_backlog_input_cancel_button():
    """Cancel button should dismiss the backlog input with None result."""
    app = BacklogInputTestApp()
    async with app.run_test() as pilot:
        await pilot.press("tab")
        await pilot.press("enter")
        assert app.backlog_result is None


@pytest.mark.asyncio
async def test_backlog_input_add():
    """Add button should return the description text."""
    from textual.widgets import TextArea

    app = BacklogInputTestApp()
    async with app.run_test() as pilot:
        screen = app.screen
        text_area = screen.query_one(TextArea)
        text_area.insert("Fix the login bug")
        # Tab to Add button (second button after Cancel)
        await pilot.press("tab")  # Cancel
        await pilot.press("tab")  # Add
        await pilot.press("enter")
        assert app.backlog_result == "Fix the login bug"


@pytest.mark.asyncio
async def test_backlog_input_empty_validation():
    """Empty description should not submit."""
    app = BacklogInputTestApp()
    async with app.run_test() as pilot:
        # Tab to Add button without typing anything
        await pilot.press("tab")  # Cancel
        await pilot.press("tab")  # Add
        await pilot.press("enter")
        assert app.backlog_result == "not_set"


@pytest.mark.asyncio
async def test_backlog_input_arrow_navigation():
    """Arrow keys should navigate between buttons."""
    app = BacklogInputTestApp()
    async with app.run_test() as pilot:
        await pilot.press("tab")
        assert app.screen.focused.id == "btn-cancel"
        await pilot.press("right")
        assert app.screen.focused.id == "btn-add"
        await pilot.press("right")  # wraps
        assert app.screen.focused.id == "btn-cancel"


# Tests for BacklogTable


@pytest.mark.asyncio
async def test_backlog_shows_hint_when_empty():
    """Backlog should show hint row when no items."""
    server = MockServer([])
    app = HopperApp(server=server)
    async with app.run_test():
        table = app.query_one("#backlog-table")
        assert table.display is True
        assert table.row_count == 1  # hint row only


@pytest.mark.asyncio
async def test_backlog_shown_with_items():
    """Backlog table should display items plus hint row."""
    from hopper.backlog import BacklogItem

    items = [
        BacklogItem(id="bl-1111-uuid", project="proj-a", description="Fix bug", created_at=1000),
        BacklogItem(
            id="bl-2222-uuid", project="proj-b", description="Add feature", created_at=2000
        ),
    ]
    server = MockServer([], backlog=items)
    app = HopperApp(server=server)
    async with app.run_test():
        table = app.query_one("#backlog-table")
        assert table.display is True
        # 2 items + 1 hint row
        assert table.row_count == 3


@pytest.mark.asyncio
async def test_tab_switches_focus_to_backlog():
    """Tab should switch focus from session table to backlog table."""
    from hopper.backlog import BacklogItem
    from hopper.tui import BacklogTable, SessionTable

    items = [
        BacklogItem(id="bl-1111-uuid", project="proj", description="Item", created_at=1000),
    ]
    sessions = [
        Session(id="aaaa1111-uuid", stage="ore", created_at=1000),
    ]
    server = MockServer(sessions, backlog=items)
    app = HopperApp(server=server)
    async with app.run_test() as pilot:
        # Should start focused on session table
        assert isinstance(app.focused, SessionTable)
        # Tab to switch
        await pilot.press("tab")
        assert isinstance(app.focused, BacklogTable)
        # Tab back
        await pilot.press("tab")
        assert isinstance(app.focused, SessionTable)


@pytest.mark.asyncio
async def test_tab_switches_to_backlog_even_when_empty():
    """Tab should switch to backlog table even when it has no items (hint row visible)."""
    from hopper.tui import BacklogTable, SessionTable

    sessions = [
        Session(id="aaaa1111-uuid", stage="ore", created_at=1000),
    ]
    server = MockServer(sessions)
    app = HopperApp(server=server)
    async with app.run_test() as pilot:
        assert isinstance(app.focused, SessionTable)
        await pilot.press("tab")
        # Backlog is always visible, so Tab switches to it
        assert isinstance(app.focused, BacklogTable)


@pytest.mark.asyncio
async def test_jk_navigation_in_backlog():
    """j/k should navigate within the backlog table when focused."""
    from hopper.backlog import BacklogItem
    from hopper.tui import BacklogTable

    items = [
        BacklogItem(id="bl-1111-uuid", project="proj", description="First", created_at=1000),
        BacklogItem(id="bl-2222-uuid", project="proj", description="Second", created_at=2000),
    ]
    server = MockServer([], backlog=items)
    app = HopperApp(server=server)
    async with app.run_test() as pilot:
        # Switch to backlog table
        await pilot.press("tab")
        table = app.query_one("#backlog-table", BacklogTable)
        assert table.cursor_row == 0
        await pilot.press("j")
        assert table.cursor_row == 1
        await pilot.press("k")
        assert table.cursor_row == 0


@pytest.mark.asyncio
async def test_delete_backlog_item(temp_config):
    """d should delete selected backlog item when backlog is focused."""
    from hopper.backlog import BacklogItem
    from hopper.tui import BacklogTable

    items = [
        BacklogItem(id="bl-1111-uuid", project="proj", description="To delete", created_at=1000),
        BacklogItem(id="bl-2222-uuid", project="proj", description="To keep", created_at=2000),
    ]
    server = MockServer([], backlog=items)
    app = HopperApp(server=server)
    async with app.run_test() as pilot:
        # Switch to backlog table
        await pilot.press("tab")
        table = app.query_one("#backlog-table", BacklogTable)
        assert table.row_count == 3  # 2 items + hint
        # Delete first item
        await pilot.press("d")
        assert table.row_count == 2  # 1 item + hint
        assert len(app._backlog) == 1
        assert app._backlog[0].id == "bl-2222-uuid"


@pytest.mark.asyncio
async def test_delete_noop_on_session_table():
    """d should do nothing when session table is focused."""
    from hopper.backlog import BacklogItem

    items = [
        BacklogItem(id="bl-1111-uuid", project="proj", description="Item", created_at=1000),
    ]
    sessions = [
        Session(id="aaaa1111-uuid", stage="ore", created_at=1000),
    ]
    server = MockServer(sessions, backlog=items)
    app = HopperApp(server=server)
    async with app.run_test() as pilot:
        # Focus is on session table by default
        session_table = app.query_one("#session-table")
        assert session_table.row_count == 2  # 1 session + hint
        # Press d - should not delete session or backlog item
        await pilot.press("d")
        assert session_table.row_count == 2  # unchanged
        assert len(app._backlog) == 1


# Tests for LegendScreen


@pytest.mark.asyncio
async def test_legend_opens_with_l_key():
    """Pressing l should open the legend modal."""
    app = HopperApp()
    async with app.run_test() as pilot:
        await pilot.press("l")
        assert isinstance(app.screen, LegendScreen)


@pytest.mark.asyncio
async def test_legend_dismiss_with_escape():
    """Escape should dismiss the legend modal."""
    app = HopperApp()
    async with app.run_test() as pilot:
        await pilot.press("l")
        assert isinstance(app.screen, LegendScreen)
        await pilot.press("escape")
        assert not isinstance(app.screen, LegendScreen)


@pytest.mark.asyncio
async def test_legend_contains_all_symbols():
    """Legend should contain all status, stage, and connection symbols."""
    from textual.widgets import Static

    app = HopperApp()
    async with app.run_test() as pilot:
        await pilot.press("l")
        body = app.screen.query_one("#legend-body", Static)
        text = str(body.render())
        assert STATUS_RUNNING in text
        assert STATUS_STUCK in text
        assert STATUS_ERROR in text
        assert STATUS_NEW in text
        assert STAGE_ORE in text
        assert STAGE_PROCESSING in text
        assert "▸" in text
        assert "▹" in text
