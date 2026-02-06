# hopper

TUI for managing coding agents.

## What it does
Hopper manages multiple Claude Code sessions ("lodes") through a terminal dashboard inside tmux.
Each lode follows a three-stage workflow -- mill (scoping), refine (implementing), ship (merging back to main).
A background server persists state over a Unix socket and broadcasts changes; the TUI renders from that state.

## Prerequisites
- Python >= 3.11
- tmux
- uv (Python package manager)
- git

## Install
```bash
git clone <repo-url>
cd hopper
make install
hop --version
```

## Quick start
1. `hop config set name <your-name>`
2. `hop project add <path-to-git-repo>`
3. `tmux new 'hop up'`
4. Use the TUI to create lodes and navigate with keyboard. Tab switches between the lodes and backlog tables.

## CLI reference
| Command | Description |
|---------|-------------|
| `hop up` | Start the server and TUI |
| `hop process` | Run Claude for a lode's current stage |
| `hop status` | Show or update lode status |
| `hop project` | Manage projects |
| `hop config` | Get or set config values |
| `hop screenshot` | Capture TUI window as ANSI text |
| `hop processed` | Signal stage completion with output |
| `hop code` | Run a stage prompt via Codex |
| `hop backlog` | Manage backlog items |
| `hop ping` | Check if server is running |

Run `hop <command> -h` for detailed usage.

## Key concepts
**Lode** -- a Claude Code session with a unique ID, workflow stage, status, and associated tmux window.

**Stage** -- workflow position: mill (scoping), refine (implementing), or ship (merging back to main).

**Backlog** -- future work items associated with a project.

## Architecture
```text
CLI (hop)
    |
    +-- Server (background thread)
    |   +-- Unix socket listener
    |   +-- Lode + backlog state (in-memory + JSONL persistence)
    |   +-- Broadcast to connected clients
    |
    +-- TUI (main thread)
        +-- Renders from server's lode list
        +-- Handles keyboard input
        +-- Spawns Claude in tmux windows
```

User input flows through the TUI to mutate lode state, which the server broadcasts back for re-render.

## Development
```bash
make install    # Install in editable mode with dev dependencies
make test       # Run all tests with pytest
make ci         # Auto-format and lint with ruff
```
Single test: `pytest test/test_file.py::test_name`

## License
AGPL-3.0-only. Copyright (c) 2026 sol pbc.

