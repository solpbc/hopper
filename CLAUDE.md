# CLAUDE.md

Development guidelines for Hopper, a TUI for managing coding agents.

## Project Overview

Hopper manages multiple Claude Code sessions through a terminal interface. It runs inside tmux, spawning each Claude instance in its own window while providing a central dashboard for navigation and status. The server persists state and broadcasts changes over a Unix socket; the TUI renders from that state.

## Key Concepts

- **Session** - A Claude Code instance with a unique ID, workflow stage, freeform state, active flag, and associated tmux window
- **Stage** - Workflow position: "ore" (new/unprocessed), "processing" (in progress), or "ship" (complete)
- **Backlog** - Future work items with project and description

## Architecture

```
CLI (hop up)
    |
    +-- Server (background thread)
    |   +-- Unix socket listener
    |   +-- Session + backlog state (in-memory + JSONL persistence)
    |   +-- Broadcast to connected clients
    |
    +-- TUI (main thread)
        +-- Renders from server's session list
        +-- Handles keyboard input
        +-- Spawns Claude in tmux windows
```

**Data flow:** User input -> TUI -> Session mutation -> Server broadcast -> TUI re-render

**Persistence:** JSONL files in the data directory (via platformdirs). See `hopper/config.py` for paths.

## Commands

```bash
make install    # Install package in editable mode with dev dependencies
make test       # Run all tests with pytest
make ci         # Auto-format and lint with ruff
pytest test/test_file.py::test_name  # Run a single test
```

## Development Principles

- **Simple code** - Prefer plain functions over classes. Use dicts, lists, and simple data containers. Only use classes when managing stateful lifecycle (server, TUI widgets, runners).
- **DRY, KISS** - Extract common logic, prefer simple solutions.
- **Atomic writes** - Write to `.tmp` then `os.replace()` for persistence.
- **Fail fast** - Validate external state early (tmux presence, server running). Clear error messages.
- **Test everything, mock everything** - All new code paths need tests. Tests must never read real user config, files, or system state. Use fixtures and monkeypatch to isolate completely.

## TUI Conventions

- **Framework**: [Textual](https://textual.textualize.io/) with `DataTable` for session/backlog lists
- **Unicode only, no emoji** - Use Unicode symbols for status indicators. Never use emoji.
- **Color for meaning** - Green=running, red=error, cyan=action, dim=new/secondary
- **Status at row start** - Put status indicators at the beginning of rows for quick scanning
- **Two-table layout** - Sessions table and Backlog table, Tab switches focus
