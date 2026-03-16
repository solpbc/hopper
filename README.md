# hopper

Hopper pairs Claude Code and Codex for automated end-to-end feature delivery.

## What it does
Hopper runs a dual-agent workflow through a terminal dashboard inside tmux.
Claude Code handles scoping in `mill` and merging in `ship` automatically.
Codex handles implementation in `refine` via `hop code`.
Each feature is a lode that moves `mill` -> `refine` -> `ship`, with a background server persisting state over a Unix socket and broadcasting updates to the TUI.

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
make install-user  # symlink hop to ~/.local/bin, skills to ~/.claude/skills
```

## Quick start
1. `hop config set name <your-name>`
2. `hop project add <path-to-git-repo>`
3. `tmux new 'hop up'`
4. Use the TUI to create lodes and navigate with keyboard. Tab switches between the lodes and backlog tables.

## CLI reference
**Commands**
| Command | Description |
|---------|-------------|
| `hop up` | Start the server and TUI |
| `hop project` | Manage projects |
| `hop config` | Get or set config values |
| `hop screenshot` | Capture TUI window as ANSI text |
| `hop backlog` | Manage backlog items |
| `hop lode` | Manage lodes |
| `hop implement` | Create a lode for an implementation request |
| `hop ping` | Check if server is running |

**Inside a lode**
| Command | Description |
|---------|-------------|
| `hop status` | Show or update lode status |
| `hop processed` | Signal stage completion with output |
| `hop gate` | Pause lode at a review gate |
| `hop code` | Run a stage prompt via Codex |

**Aliases**
| Command | Description |
|---------|-------------|
| `hop submit` | Create a lode (alias for implement) |
| `hop list` | List lodes (alias for lode list) |
| `hop projects` | List projects (alias for project list) |
| `hop wait` | Wait for a lode to ship (alias for lode wait) |
| `hop show` | Show lode details (alias for lode show) |
| `hop watch` | Watch lode status events (alias for lode watch) |
| `hop restart` | Restart an inactive lode (alias for lode restart) |
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
make clean      # Remove build artifacts and caches
```
Single test: `pytest test/test_file.py::test_name`

## License
AGPL-3.0-only. Copyright (c) 2026 [sol pbc](https://solpbc.org).
