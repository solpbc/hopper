# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for the TUI module."""

from unittest.mock import PropertyMock, patch

import pytest
from textual.app import App

from hopper.projects import Project
from hopper.tui import (
    AUTO_OFF,
    AUTO_ON,
    STAGE_MILL,
    STAGE_REFINE,
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
    format_auto_text,
    format_stage_text,
    format_status_label,
    format_status_text,
    lode_to_row,
    strip_ansi,
)

# Tests for lode_to_row


def test_lode_to_row_new():
    """New session has new status indicator."""
    session = {"id": "abcd1234", "stage": "mill", "created_at": 1000, "state": "new"}
    row = lode_to_row(session)
    assert row.id == "abcd1234"
    assert row.status == STATUS_NEW
    assert row.stage == STAGE_MILL


def test_lode_to_row_running():
    """Running session has running status indicator."""
    session = {"id": "abcd1234", "stage": "mill", "created_at": 1000, "state": "running"}
    row = lode_to_row(session)
    assert row.id == "abcd1234"
    assert row.status == STATUS_RUNNING
    assert row.stage == STAGE_MILL


def test_lode_to_row_stuck():
    """Stuck session has stuck status indicator."""
    session = {"id": "abcd1234", "stage": "mill", "created_at": 1000, "state": "stuck"}
    row = lode_to_row(session)
    assert row.id == "abcd1234"
    assert row.status == STATUS_STUCK
    assert row.stage == STAGE_MILL


def test_lode_to_row_error():
    """Error session has error status indicator."""
    session = {"id": "abcd1234", "stage": "mill", "created_at": 1000, "state": "error"}
    row = lode_to_row(session)
    assert row.id == "abcd1234"
    assert row.status == STATUS_ERROR
    assert row.stage == STAGE_MILL


def test_lode_to_row_active():
    """Active session has active=True in row."""
    session = {
        "id": "abcd1234",
        "stage": "mill",
        "created_at": 1000,
        "state": "running",
        "active": True,
    }
    row = lode_to_row(session)
    assert row.active is True


def test_lode_to_row_inactive():
    """Inactive session has active=False in row."""
    session = {"id": "abcd1234", "stage": "mill", "created_at": 1000, "state": "new"}
    row = lode_to_row(session)
    assert row.active is False


def test_lode_to_row_auto():
    """Auto field is passed through to Row."""
    session = {"id": "abcd1234", "stage": "mill", "created_at": 1000, "auto": True}
    row = lode_to_row(session)
    assert row.auto is True


def test_lode_to_row_refine_stage():
    """Refine session has gear stage indicator."""
    session = {"id": "abcd1234", "stage": "refine", "created_at": 1000, "state": "new"}
    row = lode_to_row(session)
    assert row.stage == STAGE_REFINE


def test_lode_to_row_completed():
    """Completed session shows running indicator (transient state)."""
    session = {"id": "abcd1234", "stage": "mill", "created_at": 1000, "state": "completed"}
    row = lode_to_row(session)
    assert row.status == STATUS_RUNNING


def test_lode_to_row_ready():
    """Ready session shows running indicator (active work)."""
    session = {"id": "abcd1234", "stage": "refine", "created_at": 1000, "state": "ready"}
    row = lode_to_row(session)
    assert row.status == STATUS_RUNNING


def test_lode_to_row_task_state():
    """Task-name state shows running indicator (active work)."""
    session = {"id": "abcd1234", "stage": "refine", "created_at": 1000, "state": "audit"}
    row = lode_to_row(session)
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


def test_format_auto_text_on():
    """Auto on shows bright green indicator."""
    text = format_auto_text(True)
    assert str(text) == AUTO_ON
    assert text.style == "bright_green"


def test_format_auto_text_off():
    """Auto off shows dim indicator."""
    text = format_auto_text(False)
    assert str(text) == AUTO_OFF
    assert text.style == "bright_black"


# Tests for format_stage_text


def test_format_stage_text_mill():
    """format_stage_text returns bright_blue for mill."""
    text = format_stage_text(STAGE_MILL)
    assert str(text) == STAGE_MILL
    assert text.style == "bright_blue"


def test_format_stage_text_refine():
    """format_stage_text returns bright_yellow for refine."""
    text = format_stage_text(STAGE_REFINE)
    assert str(text) == STAGE_REFINE
    assert text.style == "bright_yellow"


# Tests for format_status_label


def test_format_status_label_running():
    """format_status_label returns bright_green for running status."""
    text = format_status_label("Claude running", STATUS_RUNNING)
    assert str(text) == "Claude running"
    assert text.style == "bright_green"


def test_format_status_label_stuck():
    """format_status_label returns bright_yellow for stuck status."""
    text = format_status_label("No output for 30s", STATUS_STUCK)
    assert str(text) == "No output for 30s"
    assert text.style == "bright_yellow"


def test_format_status_label_error():
    """format_status_label returns bright_red for error status."""
    text = format_status_label("Process exited", STATUS_ERROR)
    assert str(text) == "Process exited"
    assert text.style == "bright_red"


def test_format_status_label_new():
    """format_status_label returns no style for new status."""
    text = format_status_label("", STATUS_NEW)
    assert str(text) == ""
    assert text.style == "bright_black"


def test_format_status_label_strips_newlines():
    """format_status_label replaces newlines with spaces."""
    text = format_status_label("line1\nline2", STATUS_RUNNING)
    assert str(text) == "line1 line2"


def test_format_status_label_strips_ansi():
    """format_status_label removes ANSI escape codes."""
    ansi_text = "\x1b[31mError: Something failed\x1b[39m"
    text = format_status_label(ansi_text, STATUS_ERROR)
    assert str(text) == "Error: Something failed"


def test_format_status_label_strips_ansi_and_newlines():
    """format_status_label handles both ANSI codes and newlines."""
    ansi_text = "\x1b[31mError: line1\x1b[39m\n\x1b[31mline2\x1b[39m"
    text = format_status_label(ansi_text, STATUS_ERROR)
    assert str(text) == "Error: line1 line2"


# Tests for strip_ansi


def test_strip_ansi_removes_color_codes():
    """strip_ansi removes ANSI color codes."""
    assert strip_ansi("\x1b[31mred\x1b[0m") == "red"
    assert strip_ansi("\x1b[1;32mbold green\x1b[0m") == "bold green"


def test_strip_ansi_preserves_plain_text():
    """strip_ansi leaves plain text unchanged."""
    assert strip_ansi("plain text") == "plain text"
    assert strip_ansi("") == ""


# Tests for Row dataclass


def test_row_dataclass():
    """Row dataclass stores all fields."""
    row = Row(
        id="test1234",
        stage=STAGE_MILL,
        age="1m",
        status=STATUS_RUNNING,
        auto=True,
        project="proj",
        status_text="Working on it",
    )
    assert row.id == "test1234"
    assert row.stage == STAGE_MILL
    assert row.age == "1m"
    assert row.status == STATUS_RUNNING
    assert row.auto is True
    assert row.project == "proj"
    assert row.status_text == "Working on it"


# Tests for HopperApp


class MockServer:
    """Mock server for testing."""

    def __init__(
        self,
        sessions: list[dict] | None = None,
        backlog: list | None = None,
        git_hash: str | None = None,
        started_at: int | None = None,
    ):
        self.lodes = sessions if sessions is not None else []
        self.backlog = backlog if backlog is not None else []
        self.git_hash = git_hash
        self.started_at = started_at
        self.broadcasts: list[dict] = []

    def broadcast(self, message: dict) -> bool:
        self.broadcasts.append(message)
        return True


@pytest.mark.asyncio
async def test_app_starts():
    """App should start and have basic structure."""
    app = HopperApp()
    async with app.run_test():
        # Should have header
        assert app.title == "HOPPER"
        # Should have unified session table
        table = app.query_one("#lode-table")
        assert table is not None


@pytest.mark.asyncio
async def test_app_with_empty_lodes():
    """App should show hint row when no sessions."""
    server = MockServer([])
    app = HopperApp(server=server)
    async with app.run_test():
        table = app.query_one("#lode-table")
        # Table always visible, hint row present
        assert table.display is True
        assert table.row_count == 1  # hint row only


@pytest.mark.asyncio
async def test_app_shows_git_hash_and_uptime_in_subtitle():
    """App should show git hash and uptime in sub_title."""
    from hopper.lodes import current_time_ms

    started_at = current_time_ms() - 2 * 60 * 60_000  # 2 hours ago
    server = MockServer([], git_hash="abc1234", started_at=started_at)
    app = HopperApp(server=server)
    async with app.run_test():
        assert app.sub_title == "abc1234 · 2h"


@pytest.mark.asyncio
async def test_app_shows_uptime_only_when_no_git_hash():
    """App should show just uptime when no git hash."""
    from hopper.lodes import current_time_ms

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
async def test_app_with_lodes():
    """App should display all sessions in unified table."""
    sessions = [
        {"id": "aaaa1111", "stage": "mill", "created_at": 1000},
        {"id": "bbbb2222", "stage": "refine", "created_at": 2000},
    ]
    server = MockServer(sessions)
    app = HopperApp(server=server)
    async with app.run_test():
        table = app.query_one("#lode-table")
        # 2 sessions + 1 hint row
        assert table.row_count == 3


@pytest.mark.asyncio
async def test_app_lode_table_has_auto_column():
    """Lode table includes the auto column."""
    from hopper.tui import LodeTable

    server = MockServer([])
    app = HopperApp(server=server)
    async with app.run_test():
        table = app.query_one("#lode-table", LodeTable)
        assert LodeTable.COL_AUTO in table.columns


@pytest.mark.asyncio
async def test_toggle_auto_with_a(temp_config):
    """a toggles auto on selected lode."""
    sessions = [{"id": "aaaa1111", "stage": "mill", "created_at": 1000, "auto": False}]
    server = MockServer(sessions)
    app = HopperApp(server=server)
    async with app.run_test() as pilot:
        await pilot.press("a")
        assert sessions[0]["auto"] is True
        assert server.broadcasts[-1]["type"] == "lode_updated"
        assert server.broadcasts[-1]["lode"]["auto"] is True


@pytest.mark.asyncio
async def test_archive_with_d(temp_config):
    """d archives selected lode when lode table is focused."""
    sessions = [
        {"id": "aaaa1111", "stage": "mill", "created_at": 1000},
        {"id": "bbbb2222", "stage": "mill", "created_at": 2000},
    ]
    server = MockServer(sessions)
    app = HopperApp(server=server)
    async with app.run_test() as pilot:
        table = app.query_one("#lode-table")
        assert table.row_count == 3  # 2 lodes + hint
        await pilot.press("d")
        assert table.row_count == 2  # 1 lode + hint


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
    """down should move cursor down."""
    sessions = [
        {"id": "aaaa1111", "stage": "mill", "created_at": 1000},
        {"id": "bbbb2222", "stage": "mill", "created_at": 2000},
    ]
    server = MockServer(sessions)
    app = HopperApp(server=server)
    async with app.run_test() as pilot:
        table = app.query_one("#lode-table")
        # Should start at row 0
        assert table.cursor_row == 0
        # Press j to move down
        await pilot.press("down")
        assert table.cursor_row == 1


@pytest.mark.asyncio
async def test_cursor_up_navigation():
    """up should move cursor up."""
    sessions = [
        {"id": "aaaa1111", "stage": "mill", "created_at": 1000},
        {"id": "bbbb2222", "stage": "mill", "created_at": 2000},
    ]
    server = MockServer(sessions)
    app = HopperApp(server=server)
    async with app.run_test() as pilot:
        table = app.query_one("#lode-table")
        # Move down first
        await pilot.press("down")
        assert table.cursor_row == 1
        # Press k to move up
        await pilot.press("up")
        assert table.cursor_row == 0


@pytest.mark.asyncio
async def test_cursor_preserved_after_refresh():
    """Cursor position should be preserved when table is refreshed."""
    sessions = [
        {"id": "aaaa1111", "stage": "mill", "created_at": 1000},
        {"id": "bbbb2222", "stage": "mill", "created_at": 2000},
        {"id": "cccc3333", "stage": "mill", "created_at": 3000},
    ]
    server = MockServer(sessions)
    app = HopperApp(server=server)
    async with app.run_test() as pilot:
        table = app.query_one("#lode-table")
        # Move to row 2
        await pilot.press("down")
        await pilot.press("down")
        assert table.cursor_row == 2
        # Refresh table (simulates polling update)
        app.refresh_table()
        # Cursor should still be at row 2
        assert table.cursor_row == 2


@pytest.mark.asyncio
async def test_get_lode():
    """_get_lode should find session by ID."""
    sessions = [
        {"id": "aaaa1111", "stage": "mill", "created_at": 1000},
        {"id": "bbbb2222", "stage": "refine", "created_at": 2000},
    ]
    server = MockServer(sessions)
    app = HopperApp(server=server)
    async with app.run_test():
        session = app._get_lode("aaaa1111")
        assert session is not None
        assert session["id"] == "aaaa1111"

        session = app._get_lode("nonexistent")
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
    """Arrow keys should navigate the project list."""
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
        await pilot.press("down")
        assert option_list.highlighted == 1
        # Move back up
        await pilot.press("up")
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
        {"id": "aaaa1111", "stage": "mill", "created_at": 1000},
    ]
    server = MockServer(sessions)
    app = HopperApp(server=server)
    async with app.run_test() as pilot:
        table = app.query_one("#lode-table")
        # Move to hint row (row 1, after the one session)
        await pilot.press("down")
        assert table.cursor_row == 1
        # Simulate polling refresh
        app.refresh_table()
        # Cursor should still be on hint row
        assert table.cursor_row == 1


@pytest.mark.asyncio
async def test_enter_on_session_hint_triggers_new_session():
    """Enter on session hint row should trigger new session action."""
    sessions = [
        {"id": "aaaa1111", "stage": "mill", "created_at": 1000},
    ]
    server = MockServer(sessions)
    app = HopperApp(server=server)
    async with app.run_test() as pilot:
        called = []
        app.action_new_lode = lambda: called.append(True)
        # Move to hint row and press enter
        await pilot.press("down")
        await pilot.press("enter")
        assert len(called) == 1


@pytest.mark.asyncio
async def test_enter_on_backlog_hint_triggers_new_backlog():
    """Enter on backlog hint row should trigger new backlog action."""
    from hopper.backlog import BacklogItem

    items = [
        BacklogItem(id="bl111111", project="proj", description="Item", created_at=1000),
    ]
    server = MockServer([], backlog=items)
    app = HopperApp(server=server)
    async with app.run_test() as pilot:
        called = []
        app.action_new_backlog = lambda: called.append(True)
        # Switch to backlog, move to hint row
        await pilot.press("tab")
        await pilot.press("down")
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
        BacklogItem(id="bl111111", project="proj-a", description="Fix bug", created_at=1000),
        BacklogItem(id="bl222222", project="proj-b", description="Add feature", created_at=2000),
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
    from hopper.tui import BacklogTable, LodeTable

    items = [
        BacklogItem(id="bl111111", project="proj", description="Item", created_at=1000),
    ]
    sessions = [
        {"id": "aaaa1111", "stage": "mill", "created_at": 1000},
    ]
    server = MockServer(sessions, backlog=items)
    app = HopperApp(server=server)
    async with app.run_test() as pilot:
        # Should start focused on session table
        assert isinstance(app.focused, LodeTable)
        # Tab to switch
        await pilot.press("tab")
        assert isinstance(app.focused, BacklogTable)
        # Tab back
        await pilot.press("tab")
        assert isinstance(app.focused, LodeTable)


@pytest.mark.asyncio
async def test_tab_switches_to_backlog_even_when_empty():
    """Tab should switch to backlog table even when it has no items (hint row visible)."""
    from hopper.tui import BacklogTable, LodeTable

    sessions = [
        {"id": "aaaa1111", "stage": "mill", "created_at": 1000},
    ]
    server = MockServer(sessions)
    app = HopperApp(server=server)
    async with app.run_test() as pilot:
        assert isinstance(app.focused, LodeTable)
        await pilot.press("tab")
        # Backlog is always visible, so Tab switches to it
        assert isinstance(app.focused, BacklogTable)


@pytest.mark.asyncio
async def test_arrow_navigation_in_backlog():
    """Arrow keys should navigate within the backlog table when focused."""
    from hopper.backlog import BacklogItem
    from hopper.tui import BacklogTable

    items = [
        BacklogItem(id="bl111111", project="proj", description="First", created_at=1000),
        BacklogItem(id="bl222222", project="proj", description="Second", created_at=2000),
    ]
    server = MockServer([], backlog=items)
    app = HopperApp(server=server)
    async with app.run_test() as pilot:
        # Switch to backlog table
        await pilot.press("tab")
        table = app.query_one("#backlog-table", BacklogTable)
        assert table.cursor_row == 0
        await pilot.press("down")
        assert table.cursor_row == 1
        await pilot.press("up")
        assert table.cursor_row == 0


@pytest.mark.asyncio
async def test_delete_backlog_item(temp_config):
    """d should delete selected backlog item when backlog is focused."""
    from hopper.backlog import BacklogItem
    from hopper.tui import BacklogTable

    items = [
        BacklogItem(id="bl111111", project="proj", description="To delete", created_at=1000),
        BacklogItem(id="bl222222", project="proj", description="To keep", created_at=2000),
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
        assert app._backlog[0].id == "bl222222"


@pytest.mark.asyncio
async def test_delete_archives_on_session_table():
    """d should archive selected lode when session table is focused."""
    from hopper.backlog import BacklogItem

    items = [
        BacklogItem(id="bl111111", project="proj", description="Item", created_at=1000),
    ]
    sessions = [
        {"id": "aaaa1111", "stage": "mill", "created_at": 1000},
    ]
    server = MockServer(sessions, backlog=items)
    app = HopperApp(server=server)
    async with app.run_test() as pilot:
        # Focus is on session table by default
        session_table = app.query_one("#lode-table")
        assert session_table.row_count == 2  # 1 session + hint
        # Press d - should archive session and leave backlog unchanged
        await pilot.press("d")
        assert session_table.row_count == 1  # hint only
        assert len(app._backlog) == 1


def test_action_delete_archives_lode():
    """action_delete archives lode when lode table is focused."""
    from hopper.tui import LodeTable

    sessions = [{"id": "aaaa1111", "stage": "mill", "created_at": 1000}]
    server = MockServer(sessions)
    app = HopperApp(server=server)
    with (
        patch.object(HopperApp, "focused", new_callable=PropertyMock, return_value=LodeTable()),
        patch.object(app, "_get_selected_lode_id", return_value="aaaa1111"),
        patch("hopper.tui.archive_lode") as mock_archive,
        patch.object(app, "refresh_table") as mock_refresh,
    ):
        app.action_delete()
    mock_archive.assert_called_once_with(app._lodes, "aaaa1111")
    mock_refresh.assert_called_once()


def test_action_delete_removes_backlog():
    """action_delete removes backlog item when backlog table is focused."""
    from hopper.backlog import BacklogItem
    from hopper.tui import BacklogTable

    items = [
        BacklogItem(id="bl111111", project="proj", description="To delete", created_at=1000),
    ]
    server = MockServer([], backlog=items)
    app = HopperApp(server=server)
    with (
        patch.object(HopperApp, "focused", new_callable=PropertyMock, return_value=BacklogTable()),
        patch.object(app, "_get_selected_backlog_id", return_value="bl111111"),
        patch("hopper.tui.remove_backlog_item", return_value=items[0]) as mock_remove,
        patch.object(app, "refresh_backlog") as mock_refresh,
    ):
        app.action_delete()
    mock_remove.assert_called_once_with(app._backlog, "bl111111")
    mock_refresh.assert_called_once()


def test_action_delete_noop_when_neither_focused():
    """action_delete should noop when focus is not lode/backlog table."""
    app = HopperApp()
    with (
        patch.object(HopperApp, "focused", new_callable=PropertyMock, return_value=object()),
        patch("hopper.tui.archive_lode") as mock_archive,
        patch("hopper.tui.remove_backlog_item") as mock_remove,
    ):
        app.action_delete()
    mock_archive.assert_not_called()
    mock_remove.assert_not_called()


# Tests for BacklogEditScreen


class BacklogEditTestApp(App):
    """Test app wrapper for BacklogEditScreen."""

    def __init__(self, initial_text: str = ""):
        super().__init__()
        self.edit_result = "not_set"  # sentinel value
        self._initial_text = initial_text

    def on_mount(self) -> None:
        from hopper.tui import BacklogEditScreen

        def capture_result(r):
            self.edit_result = r

        self.push_screen(BacklogEditScreen(initial_text=self._initial_text), capture_result)


@pytest.mark.asyncio
async def test_backlog_edit_prefills_text():
    """BacklogEditScreen should show pre-filled text."""
    from textual.widgets import TextArea

    app = BacklogEditTestApp(initial_text="Existing description")
    async with app.run_test():
        ta = app.screen.query_one(TextArea)
        assert ta.text == "Existing description"


@pytest.mark.asyncio
async def test_backlog_edit_cancel_escape():
    """Escape should dismiss the edit screen with None."""
    app = BacklogEditTestApp(initial_text="Some text")
    async with app.run_test() as pilot:
        await pilot.press("escape")
        assert app.edit_result is None


@pytest.mark.asyncio
async def test_backlog_edit_save():
    """Save button should return ('save', text)."""
    from textual.widgets import TextArea

    app = BacklogEditTestApp(initial_text="Original")
    async with app.run_test() as pilot:
        ta = app.screen.query_one(TextArea)
        ta.clear()
        ta.insert("Updated text")
        # Tab to Cancel, Promote, Save (3rd button)
        await pilot.press("tab")  # Cancel
        await pilot.press("tab")  # Promote
        await pilot.press("tab")  # Save
        await pilot.press("enter")
        assert app.edit_result == ("save", "Updated text")


@pytest.mark.asyncio
async def test_backlog_edit_promote():
    """Promote button should return ('promote', text)."""
    from textual.widgets import TextArea

    app = BacklogEditTestApp(initial_text="Task to promote")
    async with app.run_test() as pilot:
        ta = app.screen.query_one(TextArea)
        assert ta.text == "Task to promote"
        # Tab to Cancel, then Promote (2nd button)
        await pilot.press("tab")  # Cancel
        await pilot.press("tab")  # Promote
        await pilot.press("enter")
        assert app.edit_result == ("promote", "Task to promote")


@pytest.mark.asyncio
async def test_backlog_edit_empty_validation():
    """Empty text should not submit."""
    app = BacklogEditTestApp(initial_text="")
    async with app.run_test() as pilot:
        # Tab to Save button
        await pilot.press("tab")  # Cancel
        await pilot.press("tab")  # Promote
        await pilot.press("tab")  # Save
        await pilot.press("enter")
        assert app.edit_result == "not_set"


@pytest.mark.asyncio
async def test_backlog_edit_arrow_navigation():
    """Arrow keys should navigate between buttons."""
    app = BacklogEditTestApp(initial_text="Text")
    async with app.run_test() as pilot:
        await pilot.press("tab")
        assert app.screen.focused.id == "btn-cancel"
        await pilot.press("right")
        assert app.screen.focused.id == "btn-promote"
        await pilot.press("right")
        assert app.screen.focused.id == "btn-save"
        await pilot.press("right")  # wraps
        assert app.screen.focused.id == "btn-cancel"


@pytest.mark.asyncio
async def test_enter_on_backlog_item_opens_edit(temp_config):
    """Enter on a backlog item should open BacklogEditScreen."""
    from hopper.backlog import BacklogItem
    from hopper.tui import BacklogEditScreen

    items = [
        BacklogItem(id="bl111111", project="proj", description="Edit me", created_at=1000),
    ]
    server = MockServer([], backlog=items)
    app = HopperApp(server=server)
    async with app.run_test() as pilot:
        await pilot.press("tab")  # Focus backlog table
        await pilot.press("enter")  # Enter on first item
        assert isinstance(app.screen, BacklogEditScreen)


@pytest.mark.asyncio
async def test_backlog_edit_save_updates_item(temp_config):
    """Saving from edit modal should update the backlog item description."""
    from textual.widgets import TextArea

    from hopper.backlog import BacklogItem
    from hopper.tui import BacklogEditScreen

    items = [
        BacklogItem(id="bl111111", project="proj", description="Original", created_at=1000),
    ]
    server = MockServer([], backlog=items)
    app = HopperApp(server=server)
    async with app.run_test() as pilot:
        await pilot.press("tab")
        await pilot.press("enter")
        assert isinstance(app.screen, BacklogEditScreen)
        ta = app.screen.query_one(TextArea)
        assert ta.text == "Original"
        ta.clear()
        ta.insert("Updated")
        # Tab to Save (3rd button)
        await pilot.press("tab")  # Cancel
        await pilot.press("tab")  # Promote
        await pilot.press("tab")  # Save
        await pilot.press("enter")
        assert app._backlog[0].description == "Updated"


@pytest.mark.asyncio
async def test_backlog_promote_creates_session(monkeypatch, temp_config):
    """Promote should create a session, remove backlog item, and spawn in background."""
    from textual.widgets import TextArea

    from hopper.backlog import BacklogItem
    from hopper.tui import BacklogEditScreen

    items = [
        BacklogItem(id="bl111111", project="testproj", description="Promote me", created_at=1000),
    ]
    server = MockServer([], backlog=items)
    app = HopperApp(server=server)

    spawned = []
    monkeypatch.setattr(
        "hopper.tui.spawn_claude",
        lambda sid, path, foreground=True: spawned.append(
            {"sid": sid, "path": path, "fg": foreground}
        ),
    )
    monkeypatch.setattr("hopper.tui.find_project", lambda name: None)

    async with app.run_test() as pilot:
        await pilot.press("tab")
        await pilot.press("enter")
        assert isinstance(app.screen, BacklogEditScreen)
        ta = app.screen.query_one(TextArea)
        assert ta.text == "Promote me"
        # Tab to Promote (2nd button)
        await pilot.press("tab")  # Cancel
        await pilot.press("tab")  # Promote
        await pilot.press("enter")

        # Backlog item should be removed
        assert len(app._backlog) == 0
        # Lode should be created
        assert len(app._lodes) == 1
        session = app._lodes[0]
        assert session["project"] == "testproj"
        assert session["scope"] == "Promote me"
        assert session["backlog"] is not None
        assert session["backlog"]["id"] == "bl111111"
        assert session["backlog"]["description"] == "Promote me"
        # Should have spawned in background
        assert len(spawned) == 1
        assert spawned[0]["fg"] is False


# Tests for MillReviewScreen


class MillReviewTestApp(App):
    """Test app wrapper for MillReviewScreen."""

    def __init__(self, initial_text: str = ""):
        super().__init__()
        self.review_result = "not_set"  # sentinel value
        self._initial_text = initial_text

    def on_mount(self) -> None:
        from hopper.tui import MillReviewScreen

        def capture_result(r):
            self.review_result = r

        self.push_screen(MillReviewScreen(initial_text=self._initial_text), capture_result)


@pytest.mark.asyncio
async def test_mill_review_prefills_text():
    """MillReviewScreen should show pre-filled text."""
    from textual.widgets import TextArea

    app = MillReviewTestApp(initial_text="Mill output content")
    async with app.run_test():
        ta = app.screen.query_one(TextArea)
        assert ta.text == "Mill output content"


@pytest.mark.asyncio
async def test_mill_review_cancel_escape():
    """Escape should dismiss the review screen with None."""
    app = MillReviewTestApp(initial_text="Some text")
    async with app.run_test() as pilot:
        await pilot.press("escape")
        assert app.review_result is None


@pytest.mark.asyncio
async def test_mill_review_save():
    """Save button should return ('save', text)."""
    from textual.widgets import TextArea

    app = MillReviewTestApp(initial_text="Original prompt")
    async with app.run_test() as pilot:
        ta = app.screen.query_one(TextArea)
        ta.clear()
        ta.insert("Edited prompt")
        # Tab to Cancel, Process, Save (3rd button)
        await pilot.press("tab")  # Cancel
        await pilot.press("tab")  # Process
        await pilot.press("tab")  # Save
        await pilot.press("enter")
        assert app.review_result == ("save", "Edited prompt")


@pytest.mark.asyncio
async def test_mill_review_process():
    """Process button should return ('process', text)."""
    from textual.widgets import TextArea

    app = MillReviewTestApp(initial_text="Process this prompt")
    async with app.run_test() as pilot:
        ta = app.screen.query_one(TextArea)
        assert ta.text == "Process this prompt"
        # Tab to Cancel, then Process (2nd button)
        await pilot.press("tab")  # Cancel
        await pilot.press("tab")  # Process
        await pilot.press("enter")
        assert app.review_result == ("process", "Process this prompt")


@pytest.mark.asyncio
async def test_mill_review_empty_validation():
    """Empty text should not submit."""
    app = MillReviewTestApp(initial_text="")
    async with app.run_test() as pilot:
        await pilot.press("tab")  # Cancel
        await pilot.press("tab")  # Process
        await pilot.press("tab")  # Save
        await pilot.press("enter")
        assert app.review_result == "not_set"


@pytest.mark.asyncio
async def test_mill_review_arrow_navigation():
    """Arrow keys should navigate between buttons."""
    app = MillReviewTestApp(initial_text="Text")
    async with app.run_test() as pilot:
        await pilot.press("tab")
        assert app.screen.focused.id == "btn-cancel"
        await pilot.press("right")
        assert app.screen.focused.id == "btn-process"
        await pilot.press("right")
        assert app.screen.focused.id == "btn-save"
        await pilot.press("right")  # wraps
        assert app.screen.focused.id == "btn-cancel"


@pytest.mark.asyncio
async def test_enter_on_refine_ready_opens_mill_review(temp_config):
    """Enter on a refine/ready session should open MillReviewScreen."""
    from hopper.lodes import get_lode_dir
    from hopper.tui import MillReviewScreen

    session = {"id": "aaaa1111", "stage": "refine", "state": "ready", "created_at": 1000}
    # Write mill_out.md for this session
    session_dir = get_lode_dir(session["id"])
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "mill_out.md").write_text("The mill output")

    server = MockServer([session])
    app = HopperApp(server=server)
    async with app.run_test() as pilot:
        await pilot.press("enter")
        assert isinstance(app.screen, MillReviewScreen)
        from textual.widgets import TextArea

        ta = app.screen.query_one(TextArea)
        assert ta.text == "The mill output"


@pytest.mark.asyncio
async def test_mill_review_save_writes_file(temp_config):
    """Save from review should write edited text back to mill_out.md."""
    from textual.widgets import TextArea

    from hopper.lodes import get_lode_dir
    from hopper.tui import MillReviewScreen

    session = {"id": "aaaa1111", "stage": "refine", "state": "ready", "created_at": 1000}
    session_dir = get_lode_dir(session["id"])
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "mill_out.md").write_text("Original mill output")

    server = MockServer([session])
    app = HopperApp(server=server)
    async with app.run_test() as pilot:
        await pilot.press("enter")
        assert isinstance(app.screen, MillReviewScreen)
        ta = app.screen.query_one(TextArea)
        ta.clear()
        ta.insert("Edited mill output")
        await pilot.press("tab")  # Cancel
        await pilot.press("tab")  # Process
        await pilot.press("tab")  # Save
        await pilot.press("enter")
        assert (session_dir / "mill_out.md").read_text() == "Edited mill output"


@pytest.mark.asyncio
async def test_mill_review_process_spawns_refine(monkeypatch, temp_config):
    """Process from review should write file and spawn refine in background."""
    from textual.widgets import TextArea

    from hopper.lodes import get_lode_dir
    from hopper.tui import MillReviewScreen

    session = {
        "id": "aaaa1111",
        "stage": "refine",
        "state": "ready",
        "created_at": 1000,
        "project": "testproj",
    }
    session_dir = get_lode_dir(session["id"])
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "mill_out.md").write_text("Mill output content")

    spawned = []
    monkeypatch.setattr(
        "hopper.tui.spawn_claude",
        lambda sid, path, foreground=True: spawned.append(
            {"sid": sid, "path": path, "fg": foreground}
        ),
    )
    monkeypatch.setattr("hopper.tui.find_project", lambda name: None)

    server = MockServer([session])
    app = HopperApp(server=server)
    async with app.run_test() as pilot:
        await pilot.press("enter")
        assert isinstance(app.screen, MillReviewScreen)
        ta = app.screen.query_one(TextArea)
        assert ta.text == "Mill output content"
        ta.clear()
        ta.insert("Edited for processing")
        # Tab to Process button
        await pilot.press("tab")  # Cancel
        await pilot.press("tab")  # Process
        await pilot.press("enter")

        # File should be updated
        assert (session_dir / "mill_out.md").read_text() == "Edited for processing"
        # Should have spawned refine in background
        assert len(spawned) == 1
        assert spawned[0]["sid"] == session["id"]
        assert spawned[0]["fg"] is False


@pytest.mark.asyncio
async def test_enter_on_non_ready_refine_spawns_directly(monkeypatch, temp_config):
    """Enter on a refine session that is NOT ready should spawn directly."""
    session = {"id": "aaaa1111", "stage": "refine", "state": "running", "created_at": 1000}
    spawned = []
    monkeypatch.setattr(
        "hopper.tui.spawn_claude",
        lambda sid, path, foreground=True: spawned.append({"sid": sid}),
    )
    monkeypatch.setattr("hopper.tui.find_project", lambda name: None)

    server = MockServer([session])
    app = HopperApp(server=server)
    async with app.run_test() as pilot:
        await pilot.press("enter")
        # Should spawn directly, not open modal
        assert not isinstance(app.screen, type(None))
        assert len(spawned) == 1
        assert spawned[0]["sid"] == session["id"]


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
        assert STAGE_MILL in text
        assert STAGE_REFINE in text
        assert AUTO_ON in text
        assert AUTO_OFF in text
        assert "▸" in text
        assert "▹" in text


# Tests for ShipReviewScreen


class ShipReviewTestApp(App):
    """Test app wrapper for ShipReviewScreen."""

    def __init__(self, diff_stat: str = ""):
        super().__init__()
        self.review_result = "not_set"  # sentinel value
        self._diff_stat = diff_stat

    def on_mount(self) -> None:
        from hopper.tui import ShipReviewScreen

        def capture_result(r):
            self.review_result = r

        self.push_screen(ShipReviewScreen(diff_stat=self._diff_stat), capture_result)


@pytest.mark.asyncio
async def test_ship_review_shows_diff_stat():
    """ShipReviewScreen should display the diff stat."""
    from textual.widgets import Static

    diff = " file.py | 10 ++++------\n 1 file changed"
    app = ShipReviewTestApp(diff_stat=diff)
    async with app.run_test():
        body = app.screen.query_one("#ship-diff", Static)
        text = str(body.render())
        assert "file.py" in text


@pytest.mark.asyncio
async def test_ship_review_shows_no_changes():
    """ShipReviewScreen should show 'No changes' when diff is empty."""
    from textual.widgets import Static

    app = ShipReviewTestApp(diff_stat="")
    async with app.run_test():
        body = app.screen.query_one("#ship-diff", Static)
        text = str(body.render())
        assert "No changes" in text


@pytest.mark.asyncio
async def test_ship_review_cancel_escape():
    """Escape should dismiss the review screen with None."""
    app = ShipReviewTestApp(diff_stat="file.py | 1 +")
    async with app.run_test() as pilot:
        await pilot.press("escape")
        assert app.review_result is None


@pytest.mark.asyncio
async def test_ship_review_cancel_button():
    """Cancel button should dismiss with None."""
    app = ShipReviewTestApp(diff_stat="file.py | 1 +")
    async with app.run_test() as pilot:
        await pilot.press("left")  # Ship -> Refine
        await pilot.press("left")  # Refine -> Cancel
        await pilot.press("enter")
        assert app.review_result is None


@pytest.mark.asyncio
async def test_ship_review_ship_button():
    """Ship button should return 'ship'."""
    app = ShipReviewTestApp(diff_stat="file.py | 1 +")
    async with app.run_test() as pilot:
        # Ship button is focused by default
        await pilot.press("enter")
        assert app.review_result == "ship"


@pytest.mark.asyncio
async def test_ship_review_refine_button():
    """Refine button should return 'refine'."""
    app = ShipReviewTestApp(diff_stat="file.py | 1 +")
    async with app.run_test() as pilot:
        await pilot.press("left")  # Ship -> Refine
        await pilot.press("enter")
        assert app.review_result == "refine"


@pytest.mark.asyncio
async def test_ship_review_arrow_navigation():
    """Arrow keys should navigate between buttons."""
    app = ShipReviewTestApp(diff_stat="file.py | 1 +")
    async with app.run_test() as pilot:
        # Ship is focused by default
        assert app.screen.focused.id == "btn-ship"
        await pilot.press("left")
        assert app.screen.focused.id == "btn-refine"
        await pilot.press("left")
        assert app.screen.focused.id == "btn-cancel"
        await pilot.press("left")  # wraps
        assert app.screen.focused.id == "btn-ship"
        await pilot.press("right")  # wraps other way
        assert app.screen.focused.id == "btn-cancel"


@pytest.mark.asyncio
async def test_enter_on_ship_ready_opens_ship_review(temp_config):
    """Enter on a ship/ready session should open ShipReviewScreen."""
    from hopper.lodes import get_lode_dir
    from hopper.tui import ShipReviewScreen

    session = {"id": "aaaa1111", "stage": "ship", "state": "ready", "created_at": 1000}
    # Create worktree directory for this session
    session_dir = get_lode_dir(session["id"])
    worktree = session_dir / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)

    server = MockServer([session])
    app = HopperApp(server=server)

    with patch("hopper.tui.get_diff_stat", return_value=" file.py | 5 +++++"):
        async with app.run_test() as pilot:
            await pilot.press("enter")
            assert isinstance(app.screen, ShipReviewScreen)


@pytest.mark.asyncio
async def test_ship_review_ship_spawns_ship(monkeypatch, temp_config):
    """Ship from review should spawn ship in background."""
    from hopper.lodes import get_lode_dir
    from hopper.tui import ShipReviewScreen

    session = {
        "id": "aaaa1111",
        "stage": "ship",
        "state": "ready",
        "created_at": 1000,
        "project": "testproj",
    }
    session_dir = get_lode_dir(session["id"])
    worktree = session_dir / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)

    spawned = []
    monkeypatch.setattr(
        "hopper.tui.spawn_claude",
        lambda sid, path, foreground=True: spawned.append(
            {"sid": sid, "path": path, "fg": foreground}
        ),
    )
    monkeypatch.setattr("hopper.tui.find_project", lambda name: None)

    server = MockServer([session])
    app = HopperApp(server=server)

    with patch("hopper.tui.get_diff_stat", return_value=" file.py | 5 +++++"):
        async with app.run_test() as pilot:
            await pilot.press("enter")
            assert isinstance(app.screen, ShipReviewScreen)
            # Ship is focused by default
            await pilot.press("enter")

            assert len(spawned) == 1
            assert spawned[0]["sid"] == session["id"]
            assert spawned[0]["fg"] is False


@pytest.mark.asyncio
async def test_ship_review_refine_changes_stage_and_spawns(monkeypatch, temp_config):
    """Refine from review should change stage back and spawn refine."""
    from hopper.lodes import get_lode_dir
    from hopper.tui import ShipReviewScreen

    session = {
        "id": "aaaa1111",
        "stage": "ship",
        "state": "ready",
        "created_at": 1000,
        "project": "testproj",
    }
    session_dir = get_lode_dir(session["id"])
    worktree = session_dir / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)

    spawned = []
    monkeypatch.setattr(
        "hopper.tui.spawn_claude",
        lambda sid, path, foreground=True: spawned.append(
            {"sid": sid, "path": path, "fg": foreground}
        ),
    )
    monkeypatch.setattr("hopper.tui.find_project", lambda name: None)

    server = MockServer([session])
    app = HopperApp(server=server)

    with patch("hopper.tui.get_diff_stat", return_value=" file.py | 5 +++++"):
        async with app.run_test() as pilot:
            await pilot.press("enter")
            assert isinstance(app.screen, ShipReviewScreen)
            await pilot.press("left")  # Ship -> Refine
            await pilot.press("enter")

            # Lode stage should be changed back to refine
            assert session["stage"] == "refine"
            assert session["state"] == "running"
            assert session["status"] == "Resuming refine"

            # Should have spawned in background
            assert len(spawned) == 1
            assert spawned[0]["sid"] == session["id"]
            assert spawned[0]["fg"] is False
