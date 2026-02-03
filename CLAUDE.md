# CLAUDE.md

Development guidelines for Hopper, a TUI for managing coding agents.

## Project Overview

Hopper manages multiple Claude Code sessions through a terminal interface. It runs inside tmux, spawning each Claude instance in its own window while providing a central dashboard for navigation and status.

## Key Concepts

- **Session** - A Claude Code instance with unique ID, workflow stage, state (idle/running/error), and associated tmux window
- **Stage** - Workflow position: "ore" (new/unprocessed) or "processing" (in progress)
- **Server** - Background Unix socket server (JSONL protocol) that owns session state and broadcasts changes to clients
- **TUI** - Terminal interface built with `Textual` for viewing and managing sessions

## Architecture

```
CLI (hop up)
    │
    ├── Server (background thread)
    │   ├── Unix socket listener
    │   ├── Session state (in-memory + JSONL persistence)
    │   └── Broadcast to connected clients
    │
    └── TUI (main thread)
        ├── Renders from server's session list
        ├── Handles keyboard input
        └── Spawns Claude in tmux windows via action_select_row()
```

**Data flow:** User input → TUI → Session mutation → Server broadcast → TUI re-render

**Persistence:** Sessions stored in `~/.local/share/hopper/sessions.jsonl` (via platformdirs)

**Key modules:**
- `cli.py` - Command dispatch, guards (require_server, is_inside_tmux)
- `server.py` - Socket server, message handling, `start_server_with_tui()`
- `client.py` - `HopperConnection` (persistent socket) and stateless helpers (`connect`, `ping`, etc.)
- `tui.py` - Textual App with `HopperApp` class, session tables, and action handlers
- `sessions.py` - Session dataclass, load/save/create/archive
- `ore.py` - `OreRunner` wraps Claude execution with session lifecycle management
- `tmux.py` - Window creation, selection, session listing
- `claude.py` - Spawning Claude Code with session ID

## Commands

```bash
make install    # Install package in editable mode with dev dependencies
make test       # Run all tests with pytest
make ci         # Auto-format and lint with ruff
pytest test/test_file.py::test_name  # Run a single test
```

## Coding Standards

### Textual Patterns

The TUI uses [Textual](https://textual.textualize.io/), a modern async TUI framework:

- **App structure**: `HopperApp(App)` with `compose()` for layout, `BINDINGS` for keys
- **Widgets**: Use `DataTable` for session lists, `Header`/`Footer` for chrome
- **Styling**: Use Rich Text for colored status indicators, CSS for layout
- **Testing**: Use `app.run_test()` async context with `pilot.press()` for input simulation

**IMPORTANT - DataTable Updates:**
- **Never use `table.clear()` for refreshing data** - it resets cursor position
- **Use `update_cell(row_key, column_key, value)`** for updating existing rows
- **Use `add_row()`/`remove_row()`** only when rows actually change
- **Define column keys explicitly** with `add_column("Label", key="col_key")` to enable `update_cell()`
- Always consult [Textual DataTable docs](https://textual.textualize.io/widgets/data_table/) before modifying table behavior

```python
# Textual async test pattern
@pytest.mark.asyncio
async def test_example():
    app = HopperApp()
    async with app.run_test() as pilot:
        await pilot.press("j")  # simulate keypress
        assert app.query_one("#session-table").cursor_row == 1
```

### Testing

- All new code paths should have tests
- TUI tests use Textual's async testing framework with `@pytest.mark.asyncio`
- Use `temp_config` fixture pattern (see `test_sessions.py`) for file-based tests
- **ALWAYS mock external state** - Tests must NEVER read real user config, files, or system state. Use fixtures/monkeypatch to isolate tests completely. A test that passes on your machine but fails on another due to user-specific state is a critical bug.

### Error Handling

- Guard functions return `int | None` (exit code or None for success)
- Validate external state (tmux presence, server running) before operations
- Fail fast with clear user messages

## Development Principles

- **DRY, KISS** - Extract common logic, prefer simple solutions
- **Atomic writes** - Write to `.tmp` then `os.replace()` for persistence
- **Test the render path** - TUI bugs are easy to miss without render tests

## TUI Design Principles

- **Unicode only, no emoji** - Use Unicode symbols (●, ○, ✗, +) for status indicators. Never use emoji.
- **Color for meaning** - Green=running, red=error, cyan=action, dim=idle/secondary
- **Status at row start** - Put status indicators at the beginning of rows for quick scanning
- **Two-table layout** - ORE (new/unprocessed) and PROCESSING sections with cursor crossing between them

## Quick Reference

### File Locations

- **Entry point:** `hopper/cli.py:main()`
- **Data directory:** `~/.local/share/hopper/` (platformdirs)
- **Socket:** `~/.local/share/hopper/server.sock`
- **Sessions:** `~/.local/share/hopper/sessions.jsonl`
- **Tests:** `test/test_*.py`
