# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for the TUI module."""

from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from textual.app import App

from hopper.lodes import format_age, parse_diff_numstat
from hopper.projects import Project
from hopper.tui import (
    SHIPPED_24H_MS,
    STATUS_DISCONNECTED,
    STATUS_ERROR,
    STATUS_NEW,
    STATUS_RUNNING,
    STATUS_SHIPPED,
    STATUS_STUCK,
    BacklogInputScreen,
    FileViewerScreen,
    HopperApp,
    LegendScreen,
    ProjectPickerScreen,
    Row,
    ScopeInputScreen,
    ShippedTable,
    format_diff_summary,
    format_stage_text,
    format_status_label,
    format_status_text,
    lode_to_row,
    strip_ansi,
)

# Tests for lode_to_row


def test_lode_to_row_new():
    """New session has new status indicator."""
    session = {
        "id": "abcd1234",
        "stage": "mill",
        "created_at": 1000,
        "updated_at": 1000,
        "state": "new",
        "active": True,
    }
    row = lode_to_row(session)
    assert row.id == "abcd1234"
    assert row.status == STATUS_NEW
    assert row.stage == "mill"
    assert row.last == row.age


def test_lode_to_row_last_uses_updated_at():
    """last uses updated_at while age uses created_at."""
    session = {
        "id": "abcd1234",
        "stage": "mill",
        "created_at": 1000,
        "updated_at": 2000,
        "state": "new",
        "active": True,
    }
    row = lode_to_row(session)
    assert row.age == format_age(1000)
    assert row.last == format_age(2000)


def test_lode_to_row_last_fallback_no_updated_at():
    """last falls back to created_at when updated_at is missing."""
    session = {
        "id": "abcd1234",
        "stage": "mill",
        "created_at": 1000,
        "state": "new",
        "active": True,
    }
    row = lode_to_row(session)
    assert row.last == row.age


def test_lode_to_row_running():
    """Running session has running status indicator."""
    session = {
        "id": "abcd1234",
        "stage": "mill",
        "created_at": 1000,
        "updated_at": 1000,
        "state": "running",
        "active": True,
    }
    row = lode_to_row(session)
    assert row.id == "abcd1234"
    assert row.status == STATUS_RUNNING
    assert row.stage == "mill"


def test_lode_to_row_stuck():
    """Stuck session has stuck status indicator."""
    session = {
        "id": "abcd1234",
        "stage": "mill",
        "created_at": 1000,
        "updated_at": 1000,
        "state": "stuck",
        "active": True,
    }
    row = lode_to_row(session)
    assert row.id == "abcd1234"
    assert row.status == STATUS_STUCK
    assert row.stage == "mill"


def test_lode_to_row_error():
    """Error session has error status indicator."""
    session = {
        "id": "abcd1234",
        "stage": "mill",
        "created_at": 1000,
        "updated_at": 1000,
        "state": "error",
        "active": True,
    }
    row = lode_to_row(session)
    assert row.id == "abcd1234"
    assert row.status == STATUS_ERROR
    assert row.stage == "mill"


def test_lode_to_row_refine_stage():
    """Refine session has refine stage indicator."""
    session = {
        "id": "abcd1234",
        "stage": "refine",
        "created_at": 1000,
        "updated_at": 1000,
        "state": "new",
    }
    row = lode_to_row(session)
    assert row.stage == "refine"


def test_lode_to_row_completed():
    """Completed session shows running indicator (transient state)."""
    session = {
        "id": "abcd1234",
        "stage": "mill",
        "created_at": 1000,
        "updated_at": 1000,
        "state": "completed",
        "active": True,
    }
    row = lode_to_row(session)
    assert row.status == STATUS_RUNNING


def test_lode_to_row_ready():
    """Ready session shows running indicator (active work)."""
    session = {
        "id": "abcd1234",
        "stage": "refine",
        "created_at": 1000,
        "updated_at": 1000,
        "state": "ready",
        "active": True,
    }
    row = lode_to_row(session)
    assert row.status == STATUS_RUNNING


def test_lode_to_row_task_state():
    """Task-name state shows running indicator (active work)."""
    session = {
        "id": "abcd1234",
        "stage": "refine",
        "created_at": 1000,
        "updated_at": 1000,
        "state": "audit",
        "active": True,
    }
    row = lode_to_row(session)
    assert row.status == STATUS_RUNNING


def test_lode_to_row_shipped_stage():
    """Shipped stage always shows shipped icon regardless of state."""
    session = {
        "id": "abcd1234",
        "stage": "shipped",
        "created_at": 1000,
        "updated_at": 1000,
        "state": "ready",
    }
    row = lode_to_row(session)
    assert row.status == STATUS_SHIPPED
    assert row.stage == "shipped"


def test_lode_to_row_title():
    """lode_to_row maps title and defaults missing title to empty string."""
    session_with_title = {
        "id": "abcd1234",
        "stage": "mill",
        "created_at": 1000,
        "updated_at": 1000,
        "state": "new",
        "title": "Auth Flow",
    }
    row_with_title = lode_to_row(session_with_title)
    assert row_with_title.title == "Auth Flow"

    session_without_title = {
        "id": "efgh5678",
        "stage": "mill",
        "created_at": 1000,
        "updated_at": 1000,
        "state": "new",
    }
    row_without_title = lode_to_row(session_without_title)
    assert row_without_title.title == ""


@pytest.mark.asyncio
async def test_title_column_width_adjusts_to_content(make_lode):
    """Title column width should shrink to fit short titles and cap at MAX_TITLE_WIDTH."""
    from hopper.tui import LodeTable, Row

    short = [make_lode(id="aaa11111", title="Fix")]
    server = MockServer(short)
    app = HopperApp(server=server)
    async with app.run_test():
        table = app.query_one("#lode-table", LodeTable)
        col = table.columns[LodeTable.COL_TITLE]
        # "Fix" is 3 chars, clamped to MIN_TITLE_WIDTH=5
        assert col.width == LodeTable.MIN_TITLE_WIDTH

        # Now add a lode with a longer title
        server.lodes.append(make_lode(id="bbb22222", title="A" * 20))
        app.refresh_table()
        assert col.width == 20

        # Direct Row input should also clamp to MAX_TITLE_WIDTH.
        rows = [
            Row(
                id="ccc33333",
                stage="mill",
                age="",
                last="",
                status=STATUS_NEW,
                title="B" * 40,
            )
        ]
        table.update_title_width(rows)
        assert col.width == LodeTable.MAX_TITLE_WIDTH


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


def test_lode_to_row_disconnected():
    """Non-shipped inactive lode gets STATUS_DISCONNECTED icon."""
    lode = {
        "id": "abc",
        "stage": "refine",
        "state": "running",
        "active": False,
        "created_at": 1000,
        "updated_at": 1000,
    }
    row = lode_to_row(lode)
    assert row.status == STATUS_DISCONNECTED


def test_lode_to_row_shipped_inactive():
    """Shipped lode shows STATUS_SHIPPED even when inactive."""
    lode = {
        "id": "abc",
        "stage": "shipped",
        "state": "running",
        "active": False,
        "created_at": 1000,
        "updated_at": 1000,
    }
    row = lode_to_row(lode)
    assert row.status == STATUS_SHIPPED


def test_lode_to_row_active_shows_state_icon():
    """Active non-shipped lode shows normal state-based icon."""
    lode = {
        "id": "abc",
        "stage": "refine",
        "state": "running",
        "active": True,
        "created_at": 1000,
        "updated_at": 1000,
    }
    row = lode_to_row(lode)
    assert row.status == STATUS_RUNNING


# Tests for format_stage_text


def test_format_stage_text_mill():
    """format_stage_text returns bright_blue for mill."""
    text = format_stage_text("mill")
    assert str(text) == "mill"
    assert text.style == "bright_blue"


def test_format_stage_text_refine():
    """format_stage_text returns bright_yellow for refine."""
    text = format_stage_text("refine")
    assert str(text) == "refine"
    assert text.style == "bright_yellow"


def test_format_stage_text_ship():
    """format_stage_text returns bright_green for ship."""
    text = format_stage_text("ship")
    assert str(text) == "ship"
    assert text.style == "bright_green"


def test_format_stage_text_shipped():
    """format_stage_text returns bright_green for shipped."""
    text = format_stage_text("shipped")
    assert str(text) == "shipped"
    assert text.style == "bright_green"


def test_format_diff_summary():
    """format_diff_summary colorizes summary counts and handles empty input."""
    from rich.text import Span, Text

    text = format_diff_summary("+30 -8")
    assert text.plain == "+30 -8"
    assert text.spans == [Span(0, 3, "bright_green"), Span(4, 6, "bright_red")]

    assert format_diff_summary("") == Text("")
    assert format_diff_summary(None) == Text("")  # type: ignore[arg-type]


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
        stage="mill",
        age="1m",
        last="5m",
        status=STATUS_RUNNING,
        project="proj",
        title="Auth Flow",
        status_text="Working on it",
    )
    assert row.id == "test1234"
    assert row.stage == "mill"
    assert row.age == "1m"
    assert row.last == "5m"
    assert row.status == STATUS_RUNNING
    assert row.project == "proj"
    assert row.title == "Auth Flow"
    assert row.status_text == "Working on it"


# Tests for HopperApp


class MockServer:
    """Mock server for testing."""

    def __init__(
        self,
        sessions: list[dict] | None = None,
        archived_lodes: list[dict] | None = None,
        backlog: list | None = None,
        projects: list[Project] | None = None,
        git_hash: str | None = None,
        started_at: int | None = None,
    ):
        self.lodes = sessions if sessions is not None else []
        self.archived_lodes = archived_lodes if archived_lodes is not None else []
        self.backlog = backlog if backlog is not None else []
        self.projects = projects if projects is not None else []
        self.git_hash = git_hash
        self.started_at = started_at
        self.broadcasts: list[dict] = []
        self.events: list[dict] = []

    def broadcast(self, message: dict) -> bool:
        self.broadcasts.append(message)
        return True

    def enqueue(self, message: dict) -> None:
        self.events.append(message)


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
async def test_app_with_shipped_lode():
    """Shipped lodes should be displayed in the lode table."""
    sessions = [
        {"id": "aaaa1111", "stage": "shipped", "created_at": 1000},
    ]
    server = MockServer(sessions)
    app = HopperApp(server=server)
    async with app.run_test():
        table = app.query_one("#lode-table")
        assert table.row_count == 2  # 1 lode + 1 hint row


@pytest.mark.asyncio
async def test_archive_view_toggle(make_lode):
    """Left/right should toggle archive view on the lode table."""
    server = MockServer([make_lode(id="active01")], archived_lodes=[make_lode(id="arch0001")])
    app = HopperApp(server=server)
    async with app.run_test() as pilot:
        assert app._archive_view is False
        await pilot.press("left")
        assert app._archive_view is True
        await pilot.press("right")
        assert app._archive_view is False
        await pilot.press("right")
        assert app._archive_view is False


@pytest.mark.asyncio
async def test_archive_view_label_updates():
    """Archive view toggling should update the lodes section label."""
    app = HopperApp(server=MockServer([]))
    async with app.run_test() as pilot:
        label = app.query_one("#lodes_label")
        assert label.content == "lodes"
        await pilot.press("left")
        assert label.content == "lodes · archived"
        await pilot.press("right")
        assert label.content == "lodes"


@pytest.mark.asyncio
async def test_archive_view_shows_archived_lodes(make_lode):
    """Archive view should render archived lodes instead of active lodes."""
    archived = [
        make_lode(id="arch0001", updated_at=1000),
        make_lode(id="arch0002", updated_at=2000),
    ]
    server = MockServer([make_lode(id="active01")], archived_lodes=archived)
    app = HopperApp(server=server)
    async with app.run_test() as pilot:
        await pilot.press("left")
        table = app.query_one("#lode-table")
        row_keys = [str(k.value) for k in table.rows]
        assert "arch0001" in row_keys
        assert "arch0002" in row_keys
        assert "active01" not in row_keys
        top_key = str(table.coordinate_to_cell_key((0, 0)).row_key.value)
        assert top_key == "arch0002"


@pytest.mark.asyncio
async def test_archive_view_hint_row():
    """Archive view should show a back-to-active hint row."""
    app = HopperApp(server=MockServer([]))
    async with app.run_test() as pilot:
        await pilot.press("left")
        table = app.query_one("#lode-table")
        hint_row = table.get_row("_hint_lode")
        assert str(hint_row[-1]) == "← back to active lodes"


@pytest.mark.asyncio
async def test_archive_view_guards_actions(make_lode):
    """Create/delete actions should be guarded while archive view is active."""
    server = MockServer(
        [make_lode(id="active01")],
        archived_lodes=[make_lode(id="arch0001")],
    )
    app = HopperApp(server=server)
    with (
        patch.object(app, "_require_projects") as mock_require_projects,
        patch.object(app, "_get_selected_lode_id") as mock_selected_lode_id,
        patch.object(app, "_get_lode") as mock_get_lode,
    ):
        async with app.run_test() as pilot:
            await pilot.press("left")
            assert app._archive_view is True
            await pilot.press("c")
            await pilot.press("d")
    mock_require_projects.assert_not_called()
    mock_selected_lode_id.assert_not_called()
    mock_get_lode.assert_not_called()
    # No events should have been enqueued
    assert server.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["enter", "v"])
async def test_archive_view_opens_file_viewer(make_lode, key):
    """Enter and v should open the file viewer in archive view."""
    server = MockServer(
        [make_lode(id="active01")],
        archived_lodes=[make_lode(id="arch0001")],
    )
    app = HopperApp(server=server)
    with patch.object(app, "push_screen") as mock_push:
        async with app.run_test() as pilot:
            await pilot.press("left")
            assert app._archive_view is True
            await pilot.press(key)

    mock_push.assert_called_once()
    screen = mock_push.call_args.args[0]
    assert isinstance(screen, FileViewerScreen)
    assert screen.lode_id == "arch0001"


@pytest.mark.asyncio
async def test_archive_view_backlog_unaffected():
    """Backlog actions should still work while archive view is active."""
    from hopper.backlog import BacklogItem
    from hopper.tui import BacklogTable

    items = [
        BacklogItem(id="bl111111", project="proj", description="First", created_at=1000),
        BacklogItem(id="bl222222", project="proj", description="Second", created_at=2000),
    ]
    server = MockServer([], backlog=items)
    app = HopperApp(server=server)
    async with app.run_test() as pilot:
        await pilot.press("left")
        assert app._archive_view is True
        await pilot.press("tab")
        await pilot.press("tab")
        assert isinstance(app.focused, BacklogTable)
        await pilot.press("d")
        assert server.events == [{"type": "backlog_remove", "item_id": "bl111111"}]
        await pilot.press("right")
        assert app._archive_view is True
        await pilot.press("left")
        assert app._archive_view is True


@pytest.mark.asyncio
async def test_left_right_only_on_lode_table():
    """Left/right should not toggle archive view when backlog table is focused."""
    from hopper.backlog import BacklogItem
    from hopper.tui import BacklogTable

    items = [BacklogItem(id="bl111111", project="proj", description="First", created_at=1000)]
    app = HopperApp(server=MockServer([], backlog=items))
    async with app.run_test() as pilot:
        assert app._archive_view is False
        await pilot.press("tab")
        await pilot.press("tab")
        assert isinstance(app.focused, BacklogTable)
        await pilot.press("left")
        assert app._archive_view is False
        await pilot.press("right")
        assert app._archive_view is False


@pytest.mark.asyncio
async def test_archive_view_handles_missing_updated_at(make_lode):
    """Archive view should handle archived rows that don't have updated_at."""
    archived_a = make_lode(id="arch0001", updated_at=3000)
    archived_b = make_lode(id="arch0002")
    archived_b.pop("updated_at")

    app = HopperApp(server=MockServer([], archived_lodes=[archived_b, archived_a]))
    async with app.run_test() as pilot:
        await pilot.press("left")
        table = app.query_one("#lode-table")
        row_keys = [str(k.value) for k in table.rows]
        assert "arch0001" in row_keys
        assert "arch0002" in row_keys
        top_key = str(table.coordinate_to_cell_key((0, 0)).row_key.value)
        assert top_key == "arch0001"


@pytest.mark.asyncio
async def test_archive_confirm_modal_arrows_do_not_toggle_archive_view(temp_config, make_lode):
    """Left/right in archive modal should not toggle archive view."""
    from hopper.lodes import get_lode_dir
    from hopper.tui import ArchiveConfirmScreen

    session = make_lode(id="aaaa1111")
    worktree = get_lode_dir(session["id"]) / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)

    app = HopperApp(server=MockServer([session]))
    with patch("hopper.tui.get_diff_stat", return_value=" file.py | 1 +"):
        async with app.run_test() as pilot:
            assert app._archive_view is False
            await pilot.press("d")
            assert isinstance(app.screen, ArchiveConfirmScreen)
            assert app.screen.focused.id == "btn-cancel"
            await pilot.press("right")
            assert app.screen.focused.id == "btn-archive"
            assert app._archive_view is False
            await pilot.press("left")
            assert app.screen.focused.id == "btn-cancel"
            assert app._archive_view is False


@pytest.mark.asyncio
async def test_archive_with_d(temp_config):
    """d enqueues archive for selected lode when lode table is focused."""
    sessions = [
        {"id": "aaaa1111", "stage": "mill", "created_at": 1000},
        {"id": "bbbb2222", "stage": "mill", "created_at": 2000},
    ]
    server = MockServer(sessions)
    app = HopperApp(server=server)
    async with app.run_test() as pilot:
        await pilot.press("d")
        assert len(server.events) == 1
        assert server.events[0]["type"] == "lode_archive"
        assert server.events[0]["lode_id"] == "aaaa1111"


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

        self.push_screen(ScopeInputScreen("testproject"), capture_result)


@pytest.mark.asyncio
async def test_scope_screen_title_includes_project_name():
    """ScopeInputScreen title includes the capitalized project name."""
    from textual.widgets import Static

    async with ScopeTestApp().run_test() as pilot:
        screen = pilot.app.screen
        title = screen.query_one(".text-input-title", Static)
        assert "Testproject" in str(title.render())


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
async def test_scope_input_start():
    """Start button should return scope and 'start'."""
    from textual.widgets import TextArea

    app = ScopeTestApp()
    async with app.run_test() as pilot:
        # Type some text
        screen = app.screen
        text_area = screen.query_one(TextArea)
        text_area.insert("Test task scope")
        # Tab to Start button (third button)
        await pilot.press("tab")  # Cancel
        await pilot.press("tab")  # Backlog
        await pilot.press("tab")  # Start
        await pilot.press("enter")
        assert app.scope_result == ("Test task scope", "start")


@pytest.mark.asyncio
async def test_scope_input_backlog():
    """Backlog button should return scope and 'backlog'."""
    from textual.widgets import TextArea

    app = ScopeTestApp()
    async with app.run_test() as pilot:
        # Type some text
        screen = app.screen
        text_area = screen.query_one(TextArea)
        text_area.insert("Test task scope")
        # Tab to Backlog button (second button)
        await pilot.press("tab")  # Cancel
        await pilot.press("tab")  # Backlog
        await pilot.press("enter")
        assert app.scope_result == ("Test task scope", "backlog")


@pytest.mark.asyncio
async def test_scope_input_empty_validation():
    """Empty scope should not submit - result stays as sentinel."""
    app = ScopeTestApp()
    async with app.run_test() as pilot:
        # Tab to Start button without typing anything
        await pilot.press("tab")  # Cancel
        await pilot.press("tab")  # Backlog
        await pilot.press("tab")  # Start
        await pilot.press("enter")
        # Should not have dismissed - still sentinel
        assert app.scope_result == "not_set"


@pytest.mark.asyncio
async def test_scope_input_ctrl_enter_submit():
    """Ctrl+Enter should submit using the primary action."""
    from textual.widgets import TextArea

    app = ScopeTestApp()
    async with app.run_test() as pilot:
        text_area = app.screen.query_one(TextArea)
        text_area.insert("test scope")
        await pilot.press("ctrl+enter")
        assert app.scope_result == ("test scope", "start")


@pytest.mark.asyncio
async def test_ctrl_enter_empty_no_submit():
    """Ctrl+Enter with empty input should not dismiss."""
    app = ScopeTestApp()
    async with app.run_test() as pilot:
        await pilot.press("ctrl+enter")
        assert app.scope_result == "not_set"


@pytest.mark.asyncio
async def test_scope_input_arrow_keys_navigate_buttons():
    """Left/right arrows should cycle focus between buttons."""
    app = ScopeTestApp()
    async with app.run_test() as pilot:
        # Tab from TextArea to first button (Cancel)
        await pilot.press("tab")
        assert app.screen.focused.id == "btn-cancel"
        # Right arrow to Backlog
        await pilot.press("right")
        assert app.screen.focused.id == "btn-backlog"
        # Right arrow to Start
        await pilot.press("right")
        assert app.screen.focused.id == "btn-start"
        # Right arrow wraps to Cancel
        await pilot.press("right")
        assert app.screen.focused.id == "btn-cancel"
        # Left arrow wraps to Start
        await pilot.press("left")
        assert app.screen.focused.id == "btn-start"
        # Left arrow to Backlog
        await pilot.press("left")
        assert app.screen.focused.id == "btn-backlog"


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
        # Tab to Start (third button)
        await pilot.press("tab")  # Cancel
        await pilot.press("tab")  # Backlog
        await pilot.press("tab")  # Start
        assert app.screen.focused.id == "btn-start"
        # Shift+Tab back to Backlog
        await pilot.press("shift+tab")
        assert app.screen.focused.id == "btn-backlog"
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
        text_area.insert("Test task scope")
        # Tab to Cancel, then right twice to Start
        await pilot.press("tab")
        await pilot.press("right")
        await pilot.press("right")
        assert app.screen.focused.id == "btn-start"
        await pilot.press("enter")
        assert app.scope_result == ("Test task scope", "start")


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


@pytest.mark.asyncio
async def test_backlog_input_ctrl_enter_submit():
    """Ctrl+Enter should submit using Add."""
    from textual.widgets import TextArea

    app = BacklogInputTestApp()
    async with app.run_test() as pilot:
        text_area = app.screen.query_one(TextArea)
        text_area.insert("test backlog")
        await pilot.press("ctrl+enter")
        assert app.backlog_result == "test backlog"


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
async def test_shipped_table_filters_by_stage_and_time(make_lode):
    """Shipped table only shows archived lodes with stage=shipped within 24h."""
    from hopper.lodes import current_time_ms

    now = current_time_ms()
    shipped_recent = make_lode(id="ship0001", stage="shipped", updated_at=now - 1000)
    shipped_old = make_lode(
        id="ship0002",
        stage="shipped",
        updated_at=now - SHIPPED_24H_MS - 1,
    )
    refine_recent = make_lode(id="refi0001", stage="refine", updated_at=now - 1000)

    server = MockServer([], archived_lodes=[shipped_recent, shipped_old, refine_recent])
    app = HopperApp(server=server)
    async with app.run_test():
        table = app.query_one("#shipped-table", ShippedTable)
        assert table.row_count == 1
        # Verify it's the recent shipped one
        cell_key = table.coordinate_to_cell_key((0, 0))
        assert str(cell_key.row_key.value) == "ship0001"


@pytest.mark.asyncio
async def test_shipped_table_enter_opens_file_viewer(make_lode):
    """Enter on a shipped row should open FileViewerScreen."""
    from hopper.lodes import current_time_ms

    now = current_time_ms()
    shipped = make_lode(id="ship0001", stage="shipped", updated_at=now - 1000)
    server = MockServer([], archived_lodes=[shipped])
    app = HopperApp(server=server)
    with patch.object(app, "push_screen") as mock_push:
        async with app.run_test() as pilot:
            await pilot.press("tab")  # lode -> shipped
            await pilot.press("enter")

    mock_push.assert_called_once()
    screen = mock_push.call_args.args[0]
    assert isinstance(screen, FileViewerScreen)
    assert screen.lode_id == "ship0001"


@pytest.mark.asyncio
async def test_shipped_table_columns():
    """Shipped table should have project, age, id, diff, title columns."""
    server = MockServer([])
    app = HopperApp(server=server)
    async with app.run_test():
        table = app.query_one("#shipped-table", ShippedTable)
        col_keys = [str(k.value) for k in table.columns]
        assert col_keys == ["project", "age", "id", "diff", "title"]


@pytest.mark.asyncio
async def test_shipped_table_populates_diff_column(temp_config, make_lode):
    """Shipped table should display parsed diff summaries from diff.txt."""
    from hopper.lodes import current_time_ms

    lode_id = "ship0001"
    lode_dir = temp_config / "lodes" / lode_id
    lode_dir.mkdir(parents=True, exist_ok=True)
    (lode_dir / "diff.txt").write_text("10\t5\tfile.py\n20\t3\tother.py")

    now = current_time_ms()
    shipped = make_lode(id=lode_id, stage="shipped", updated_at=now - 1000)
    server = MockServer([], archived_lodes=[shipped])
    app = HopperApp(server=server)
    async with app.run_test():
        table = app.query_one("#shipped-table", ShippedTable)
        row = table.get_row(lode_id)
        assert str(row[3]) == "+30 -8"


@pytest.mark.asyncio
async def test_tab_cycles_three_tables(make_lode):
    """Tab should cycle focus: lode -> shipped -> backlog -> lode."""
    from hopper.backlog import BacklogItem
    from hopper.lodes import current_time_ms
    from hopper.tui import BacklogTable, LodeTable

    now = current_time_ms()
    items = [BacklogItem(id="bl111111", project="proj", description="Item", created_at=1000)]
    shipped = make_lode(id="ship0001", stage="shipped", updated_at=now - 1000)
    sessions = [make_lode(id="aaaa1111", stage="mill", created_at=1000)]
    server = MockServer(sessions, backlog=items, archived_lodes=[shipped])
    app = HopperApp(server=server)
    async with app.run_test() as pilot:
        assert isinstance(app.focused, LodeTable)
        await pilot.press("tab")
        assert isinstance(app.focused, ShippedTable)
        await pilot.press("tab")
        assert isinstance(app.focused, BacklogTable)
        await pilot.press("tab")
        assert isinstance(app.focused, LodeTable)


@pytest.mark.asyncio
async def test_shipped_table_empty_when_no_recent():
    """Shipped table should be empty when no recently shipped lodes."""
    server = MockServer([])
    app = HopperApp(server=server)
    async with app.run_test():
        table = app.query_one("#shipped-table", ShippedTable)
        assert table.row_count == 0


@pytest.mark.asyncio
async def test_shipped_table_updates_dynamically(make_lode):
    """Shipped table refresh should pick up new archived shipped lodes in sorted order."""
    from hopper.lodes import current_time_ms

    server = MockServer([make_lode(id="active01", stage="mill")], archived_lodes=[])
    app = HopperApp(server=server)
    async with app.run_test():
        table = app.query_one("#shipped-table", ShippedTable)
        assert table.row_count == 0

        now = current_time_ms()
        first = make_lode(id="ship0001", stage="shipped", updated_at=now - 2000)
        server.archived_lodes.append(first)
        app.refresh_shipped()

        assert table.row_count == 1
        first_key = table.coordinate_to_cell_key((0, 0))
        assert str(first_key.row_key.value) == "ship0001"

        second = make_lode(id="ship0002", stage="shipped", updated_at=now - 1000)
        server.archived_lodes.append(second)
        app.refresh_shipped()

        assert table.row_count == 2
        top_key = table.coordinate_to_cell_key((0, 0))
        assert str(top_key.row_key.value) == "ship0002"


def test_parse_diff_numstat_normal_input():
    """parse_diff_numstat sums additions and deletions across valid lines."""
    text = "10\t5\tfile.py\n20\t3\tother.py"
    assert parse_diff_numstat(text) == "+30 -8"


def test_parse_diff_numstat_skips_binary_entries():
    """Binary numstat rows are skipped."""
    text = "-\t-\tbinary.bin\n10\t5\tfile.py"
    assert parse_diff_numstat(text) == "+10 -5"


def test_parse_diff_numstat_empty_input():
    """Empty input should return empty summary."""
    assert parse_diff_numstat("") == ""


def test_parse_diff_numstat_skips_malformed_lines():
    """Non-numeric malformed entries are skipped."""
    assert parse_diff_numstat("not\ta\tvalid") == ""


def test_parse_diff_numstat_only_binary():
    """Only binary entries should return empty summary."""
    assert parse_diff_numstat("-\t-\tbinary.bin") == ""


def test_parse_diff_numstat_whitespace_only():
    """Whitespace-only input should return empty summary."""
    assert parse_diff_numstat("  \n  ") == ""


@pytest.mark.asyncio
async def test_tab_switches_focus_to_backlog():
    """Tab should switch focus from lode table to shipped then backlog."""
    from hopper.backlog import BacklogItem
    from hopper.lodes import current_time_ms
    from hopper.tui import BacklogTable, LodeTable

    now = current_time_ms()
    items = [
        BacklogItem(id="bl111111", project="proj", description="Item", created_at=1000),
    ]
    shipped = [
        {"id": "ship0001", "stage": "shipped", "created_at": 1000, "updated_at": now - 1000},
    ]
    sessions = [
        {"id": "aaaa1111", "stage": "mill", "created_at": 1000},
    ]
    server = MockServer(sessions, archived_lodes=shipped, backlog=items)
    app = HopperApp(server=server)
    async with app.run_test() as pilot:
        # Should start focused on lode table
        assert isinstance(app.focused, LodeTable)
        # Tab to shipped, then backlog
        await pilot.press("tab")
        assert isinstance(app.focused, ShippedTable)
        # Tab back
        await pilot.press("tab")
        assert isinstance(app.focused, BacklogTable)


@pytest.mark.asyncio
async def test_tab_switches_to_backlog_even_when_empty():
    """Tab should switch to backlog table even when it has no items."""
    from hopper.tui import BacklogTable, LodeTable

    sessions = [
        {"id": "aaaa1111", "stage": "mill", "created_at": 1000},
    ]
    server = MockServer(sessions)
    app = HopperApp(server=server)
    async with app.run_test() as pilot:
        assert isinstance(app.focused, LodeTable)
        await pilot.press("tab")
        await pilot.press("tab")
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
        await pilot.press("tab")
        table = app.query_one("#backlog-table", BacklogTable)
        assert table.cursor_row == 0
        await pilot.press("down")
        assert table.cursor_row == 1
        await pilot.press("up")
        assert table.cursor_row == 0


@pytest.mark.asyncio
async def test_delete_backlog_item(temp_config):
    """d should enqueue backlog_remove when backlog is focused."""
    from hopper.backlog import BacklogItem

    items = [
        BacklogItem(id="bl111111", project="proj", description="To delete", created_at=1000),
        BacklogItem(id="bl222222", project="proj", description="To keep", created_at=2000),
    ]
    server = MockServer([], backlog=items)
    app = HopperApp(server=server)
    async with app.run_test() as pilot:
        # Switch to backlog table
        await pilot.press("tab")
        await pilot.press("tab")
        # Delete first item
        await pilot.press("d")
        assert server.events == [{"type": "backlog_remove", "item_id": "bl111111"}]


@pytest.mark.asyncio
async def test_delete_archives_on_session_table():
    """d should enqueue lode_archive when session table is focused."""
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
        # Press d - should enqueue archive and leave backlog unchanged
        await pilot.press("d")
        assert server.events == [{"type": "lode_archive", "lode_id": "aaaa1111"}]
        assert len(app._backlog) == 1


def test_action_delete_archives_lode():
    """action_delete enqueues archive when lode table is focused."""
    from hopper.tui import LodeTable

    sessions = [{"id": "aaaa1111", "stage": "mill", "created_at": 1000}]
    server = MockServer(sessions)
    app = HopperApp(server=server)

    worktree_path = MagicMock()
    worktree_path.is_dir.return_value = False

    lode_dir = MagicMock()
    lode_dir.__truediv__.return_value = worktree_path

    with (
        patch.object(HopperApp, "focused", new_callable=PropertyMock, return_value=LodeTable()),
        patch.object(app, "_get_selected_lode_id", return_value="aaaa1111"),
        patch("hopper.tui.get_lode_dir", return_value=lode_dir),
    ):
        app.action_delete()
    assert len(server.events) == 1
    assert server.events[0] == {"type": "lode_archive", "lode_id": "aaaa1111"}


def test_action_delete_shows_modal_for_unmerged_changes():
    """action_delete shows confirmation modal when worktree has unmerged changes."""
    from hopper.tui import ArchiveConfirmScreen, LodeTable

    sessions = [{"id": "aaaa1111", "stage": "refine", "created_at": 1000}]
    server = MockServer(sessions)
    app = HopperApp(server=server)
    fake_diff = " file.py | 5 ++---"

    worktree_path = MagicMock()
    worktree_path.is_dir.return_value = True
    worktree_path.__str__.return_value = "/fake/worktree"

    lode_dir = MagicMock()
    lode_dir.__truediv__.return_value = worktree_path

    with (
        patch.object(HopperApp, "focused", new_callable=PropertyMock, return_value=LodeTable()),
        patch.object(app, "_get_selected_lode_id", return_value="aaaa1111"),
        patch("hopper.tui.get_lode_dir", return_value=lode_dir),
        patch("hopper.tui.get_diff_stat", return_value=fake_diff),
        patch.object(app, "push_screen") as mock_push,
    ):
        app.action_delete()

    assert server.events == []
    mock_push.assert_called_once()
    screen_arg = mock_push.call_args.args[0]
    assert isinstance(screen_arg, ArchiveConfirmScreen)


def test_action_delete_archives_immediately_without_worktree():
    """action_delete archives immediately when lode has no worktree directory."""
    from hopper.tui import LodeTable

    sessions = [{"id": "aaaa1111", "stage": "mill", "created_at": 1000}]
    server = MockServer(sessions)
    app = HopperApp(server=server)

    worktree_path = MagicMock()
    worktree_path.is_dir.return_value = False

    lode_dir = MagicMock()
    lode_dir.__truediv__.return_value = worktree_path

    with (
        patch.object(HopperApp, "focused", new_callable=PropertyMock, return_value=LodeTable()),
        patch.object(app, "_get_selected_lode_id", return_value="aaaa1111"),
        patch("hopper.tui.get_lode_dir", return_value=lode_dir),
        patch.object(app, "push_screen") as mock_push,
    ):
        app.action_delete()

    assert server.events == [{"type": "lode_archive", "lode_id": "aaaa1111"}]
    mock_push.assert_not_called()


def test_action_delete_archives_immediately_with_empty_diff():
    """action_delete archives immediately when worktree diff stat is empty (merged)."""
    from hopper.tui import LodeTable

    sessions = [{"id": "aaaa1111", "stage": "refine", "created_at": 1000}]
    server = MockServer(sessions)
    app = HopperApp(server=server)

    worktree_path = MagicMock()
    worktree_path.is_dir.return_value = True
    worktree_path.__str__.return_value = "/fake/worktree"

    lode_dir = MagicMock()
    lode_dir.__truediv__.return_value = worktree_path

    with (
        patch.object(HopperApp, "focused", new_callable=PropertyMock, return_value=LodeTable()),
        patch.object(app, "_get_selected_lode_id", return_value="aaaa1111"),
        patch("hopper.tui.get_lode_dir", return_value=lode_dir),
        patch("hopper.tui.get_diff_stat", return_value=""),
        patch.object(app, "push_screen") as mock_push,
    ):
        app.action_delete()

    assert server.events == [{"type": "lode_archive", "lode_id": "aaaa1111"}]
    mock_push.assert_not_called()


def test_action_delete_cancel_does_not_archive():
    """Cancelling the archive confirmation modal does not archive the lode."""
    from hopper.tui import LodeTable

    sessions = [{"id": "aaaa1111", "stage": "refine", "created_at": 1000}]
    server = MockServer(sessions)
    app = HopperApp(server=server)
    fake_diff = " file.py | 5 ++---"

    worktree_path = MagicMock()
    worktree_path.is_dir.return_value = True
    worktree_path.__str__.return_value = "/fake/worktree"

    lode_dir = MagicMock()
    lode_dir.__truediv__.return_value = worktree_path

    with (
        patch.object(HopperApp, "focused", new_callable=PropertyMock, return_value=LodeTable()),
        patch.object(app, "_get_selected_lode_id", return_value="aaaa1111"),
        patch("hopper.tui.get_lode_dir", return_value=lode_dir),
        patch("hopper.tui.get_diff_stat", return_value=fake_diff),
        patch.object(app, "push_screen") as mock_push,
    ):
        app.action_delete()
        callback = mock_push.call_args.args[1]
        callback(None)

    assert server.events == []


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
    ):
        app.action_delete()
    assert server.events == [{"type": "backlog_remove", "item_id": "bl111111"}]


def test_action_delete_noop_when_neither_focused():
    """action_delete should noop when focus is not lode/backlog table."""
    server = MockServer()
    app = HopperApp(server=server)
    with (
        patch.object(HopperApp, "focused", new_callable=PropertyMock, return_value=object()),
    ):
        app.action_delete()
    assert server.events == []


def test_format_diff_stat():
    """format_diff_stat colorizes +/- characters."""
    from rich.text import Text

    from hopper.tui import format_diff_stat

    result = format_diff_stat(" file.py | 3 ++-")
    assert isinstance(result, Text)
    plain = result.plain
    assert "file.py" in plain
    assert "+" in plain
    assert "-" in plain


def test_format_diff_stat_empty():
    """format_diff_stat returns 'No changes' for empty input."""
    from hopper.tui import format_diff_stat

    result = format_diff_stat("")
    assert "No changes" in result.plain


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
async def test_backlog_edit_ctrl_enter_submit():
    """Ctrl+Enter should submit using Save."""
    from textual.widgets import TextArea

    app = BacklogEditTestApp(initial_text="Original")
    async with app.run_test() as pilot:
        ta = app.screen.query_one(TextArea)
        ta.clear()
        ta.insert("Updated text")
        await pilot.press("ctrl+enter")
        assert app.edit_result == ("save", "Updated text")


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
        await pilot.press("tab")
        await pilot.press("tab")  # Focus backlog table
        await pilot.press("enter")  # Enter on first item
        assert isinstance(app.screen, BacklogEditScreen)


@pytest.mark.asyncio
async def test_backlog_edit_save_updates_item(temp_config):
    """Saving from edit modal should enqueue backlog_update."""
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
        assert server.events == [
            {"type": "backlog_update", "item_id": "bl111111", "description": "Updated"}
        ]


@pytest.mark.asyncio
async def test_backlog_promote_creates_session(monkeypatch, temp_config):
    """Promote should enqueue lode_promote_backlog."""
    from textual.widgets import TextArea

    from hopper.backlog import BacklogItem
    from hopper.tui import BacklogEditScreen

    items = [
        BacklogItem(id="bl111111", project="testproj", description="Promote me", created_at=1000),
    ]
    server = MockServer([], backlog=items)
    app = HopperApp(server=server)

    async with app.run_test() as pilot:
        await pilot.press("tab")
        await pilot.press("tab")
        await pilot.press("enter")
        assert isinstance(app.screen, BacklogEditScreen)
        ta = app.screen.query_one(TextArea)
        assert ta.text == "Promote me"
        # Tab to Promote (2nd button)
        await pilot.press("tab")  # Cancel
        await pilot.press("tab")  # Promote
        await pilot.press("enter")

        assert server.events == [
            {"type": "lode_promote_backlog", "item_id": "bl111111", "scope": "Promote me"}
        ]


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
async def test_mill_review_ctrl_enter_submit():
    """Ctrl+Enter should submit using Save."""
    from textual.widgets import TextArea

    app = MillReviewTestApp(initial_text="Original prompt")
    async with app.run_test() as pilot:
        ta = app.screen.query_one(TextArea)
        ta.clear()
        ta.insert("test review")
        await pilot.press("ctrl+enter")
        assert app.review_result == ("save", "test review")


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
    """Legend should contain all status symbols."""
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
        assert STATUS_SHIPPED in text
        assert STATUS_DISCONNECTED in text


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


class ShippedReviewTestApp(App):
    """Test app wrapper for ShippedReviewScreen."""

    def __init__(self, content: str = "", lode_title: str = ""):
        super().__init__()
        self.review_result = "not_set"  # sentinel value
        self._content = content
        self._lode_title = lode_title

    def on_mount(self) -> None:
        from hopper.tui import ShippedReviewScreen

        def capture_result(r):
            self.review_result = r

        self.push_screen(
            ShippedReviewScreen(content=self._content, lode_title=self._lode_title),
            capture_result,
        )


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
    """Refine from review should enqueue lode_resume_refine."""
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

            assert server.events == [{"type": "lode_resume_refine", "lode_id": "aaaa1111"}]


@pytest.mark.asyncio
async def test_shipped_review_has_buttons():
    """ShippedReviewScreen renders Cancel and Archive buttons."""
    from textual.widgets import Button

    app = ShippedReviewTestApp(content="Done", lode_title="Ship Title")
    async with app.run_test():
        cancel = app.screen.query_one("#shipped-cancel", Button)
        archive = app.screen.query_one("#shipped-archive", Button)
        assert cancel.label == "Cancel"
        assert archive.label == "Archive"


@pytest.mark.asyncio
async def test_shipped_review_cancel_button():
    """Cancel button dismisses shipped review with None."""
    app = ShippedReviewTestApp(content="Done")
    async with app.run_test() as pilot:
        # Cancel is focused by default
        await pilot.press("enter")
        assert app.review_result is None


@pytest.mark.asyncio
async def test_shipped_review_archive_button():
    """Archive button dismisses shipped review with True."""
    app = ShippedReviewTestApp(content="Done")
    async with app.run_test() as pilot:
        await pilot.press("right")
        await pilot.press("enter")
        assert app.review_result is True


def test_action_view_files_noop_when_backlog_focused():
    """action_view_files is a no-op when BacklogTable is focused."""
    from hopper.tui import BacklogTable

    app = HopperApp()
    with (
        patch.object(HopperApp, "focused", new_callable=PropertyMock, return_value=BacklogTable()),
        patch.object(app, "_get_selected_lode_id") as mock_selected,
        patch.object(app, "push_screen") as mock_push,
    ):
        app.action_view_files()
    mock_selected.assert_not_called()
    mock_push.assert_not_called()


def test_action_view_files_noop_when_no_lode_selected():
    """action_view_files is a no-op when no lode is selected."""
    from hopper.tui import LodeTable

    app = HopperApp()
    with (
        patch.object(HopperApp, "focused", new_callable=PropertyMock, return_value=LodeTable()),
        patch.object(app, "_get_selected_lode_id", return_value=None) as mock_selected,
        patch.object(app, "push_screen") as mock_push,
    ):
        app.action_view_files()
    mock_selected.assert_called_once()
    mock_push.assert_not_called()


def test_file_viewer_screen_init(tmp_path):
    """FileViewerScreen can be instantiated with a lode directory."""
    screen = FileViewerScreen(tmp_path, "test123")
    assert screen.lode_dir == tmp_path
    assert screen.lode_id == "test123"
