# CLAUDE.md

Development guidelines for Hopper, a TUI for managing coding agents.

## Project Overview

Hopper manages multiple Claude Code sessions through a terminal interface. It runs inside tmux, spawning each Claude instance in its own window while providing a central dashboard for navigation and status.

## Key Concepts

- **Session** - A Claude Code instance with unique ID, workflow stage, state (idle/running/error), and associated tmux window
- **Stage** - Workflow position: "ore" (new/unprocessed) or "processing" (in progress)
- **Server** - Background Unix socket server (JSONL protocol) that owns session state and broadcasts changes to clients
- **TUI** - Terminal interface built with `blessed` for viewing and managing sessions

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
        └── Spawns Claude in tmux windows via handle_enter()
```

**Data flow:** User input → TUI → Session mutation → Server broadcast → TUI re-render

**Persistence:** Sessions stored in `~/.local/share/hopper/sessions.jsonl` (via platformdirs)

**Key modules:**
- `cli.py` - Command dispatch, guards (require_server, is_inside_tmux)
- `server.py` - Socket server, message handling, `start_server_with_tui()`
- `tui.py` - Rendering, state management, `handle_enter()` for session actions
- `sessions.py` - Session dataclass, load/save/create/archive
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

### API Validation

When using external libraries (especially `blessed`), **validate API usage by introspecting the module** before using unfamiliar patterns:

```python
# Check if a capability is callable or a string
from blessed import Terminal
t = Terminal()
print(type(t.bold))      # <class 'FormattingString'> - callable
print(type(t.dim))       # <class 'str'> - not callable, use concatenation
```

Don't assume APIs based on similar-looking patterns. When in doubt, verify.

### Testing

- All new code paths should have tests
- TUI rendering code should be tested with mock Terminal objects
- Use `temp_config` fixture pattern (see `test_sessions.py`) for file-based tests
- **ALWAYS mock external state** - Tests must NEVER read real user config, files, or system state. Use fixtures/monkeypatch to isolate tests completely. A test that passes on your machine but fails on another due to user-specific state is a critical bug.

### Error Handling

- Guard functions return `int | None` (exit code or None for success)
- Validate external state (tmux presence, server running) before operations
- Fail fast with clear user messages

## Development Principles

- **DRY, KISS** - Extract common logic, prefer simple solutions
- **Immutable state** - TUIState methods return new instances
- **Atomic writes** - Write to `.tmp` then `os.replace()` for persistence
- **Test the render path** - TUI bugs are easy to miss without render tests

## TUI Design Principles

- **Unicode only, no emoji** - Use Unicode symbols (●, ○, ✗, +, ─, ━) for indicators and box-drawing. Never use emoji.
- **Color for meaning** - Green=running, red=error, cyan=action, dim=idle/secondary
- **Dynamic width** - Adapt to `term.width`, don't hardcode layouts
- **Status at row start** - Put status indicators at the beginning of rows for quick scanning
- **Visual hierarchy** - Title bar, section headers with column labels, footer with keybindings
- **Box-drawing for structure** - Use ─ and ━ for separators and borders

## Quick Reference

### File Locations

- **Entry point:** `hopper/cli.py:main()`
- **Data directory:** `~/.local/share/hopper/` (platformdirs)
- **Socket:** `~/.local/share/hopper/server.sock`
- **Sessions:** `~/.local/share/hopper/sessions.jsonl`
- **Tests:** `test/test_*.py`
