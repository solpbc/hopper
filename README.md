# hopper

Hopper pairs Claude Code with a selectable coding CLI in a staged feature-delivery workflow.

## What it does
Hopper runs a dual-agent workflow through a terminal dashboard inside tmux.
Claude Code handles scoping in `mill` and landing in `ship`.
Grok handles implementation in `refine` by default; a lode can select Codex instead.
`hop code` resumes the coding provider selected when the lode was created.
Each feature is a lode that moves `mill` -> `refine` -> `ship`, with a background server persisting state over a Unix socket and broadcasting updates to the TUI.

## Prerequisites
- Python >= 3.11
- tmux
- uv (Python package manager)
- git
- Grok CLI, or Codex CLI for lodes created with `--coder codex`

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
| `hop remote` | Manage ordered project host pools for remote hopper instances |
| `hop config` | Get or set config values |
| `hop screenshot` | Capture TUI window as ANSI text |
| `hop backlog` | Manage backlog items |
| `hop lode` | Manage lodes |
| `hop implement` | Create a lode for an implementation request |
| `hop coder` | Check whether a coding provider is installed and runnable |
| `hop ping` | Check if server is running |

**Inside a lode**
| Command | Description |
|---------|-------------|
| `hop status` | Show or update lode status |
| `hop processed` | Durably submit stage output; return after acceptance |
| `hop gate` | Pause lode at a review gate |
| `hop code` | Run a stage prompt via the lode's selected coding provider |

**Aliases**
| Command | Description |
|---------|-------------|
| `hop submit` | Create a lode (alias for implement) |
| `hop list` | List lodes (alias for lode list) |
| `hop projects` | List projects (alias for project list) |
| `hop wait` | Supervise one lode to a final outcome (one-hour default and maximum) |
| `hop show` | Show lode details (alias for lode show) |
| `hop watch` | Exact alias for `hop wait` |
| `hop restart` | Restart an inactive lode (alias for lode restart) |
Run `hop <command> -h` for detailed usage.

Grok is the creation default. Select Codex per lode with any create alias:

```bash
cat scope.md | hop implement myproject --coder codex
hop coder check codex --json
```

Hopper does not pass a Grok model name, so the CLI uses the current default model
available to the authenticated account. Hopper disables Grok auto-update during a
lode run; update the installed CLI through the normal host provisioning process.

Run `hop wait ID` or `hop watch ID` as a bare command to supervise exactly one
lode, and read the complete final record, not only its exit code. The record
explains why supervision ended and includes recovery, owning server, lode status,
pane context, and worktree context.
`hop lode watch ID` is the separate streaming event view.

Useful lode subcommands include `hop lode peek`, `hop lode nudge`, `hop lode
answer`, and `hop lode path` for pane inspection, prompt recovery, and locating
the exact worktree. `hop lode pause ID` closes the owned pane, proves containment
is empty, and retains the active lode, worktree, branch, and stage session;
`hop lode resume ID` continues it. Watch, pause, and resume route to the lode's
resident host. `hop lode archive ID` removes an already-inactive stale row when
Hopper cannot prove its recorded run ownership, while retaining its worktree and
branch. `hop lode kill` proves containment and durability before
archiving the lode while retaining its worktree and branch for recovery.

Use `hop remote` plus the global `-H/--host` flag for remote hopper hosts.
Quote remote-home paths (`hop -H host project add '~/src/repo'`): an unquoted
tilde expands locally and is rejected before SSH. `hop lode status` exits 2
when a remote host is unreadable, distinct from exit 1 for a confirmed absence.

### Remote pools and resident routes

Configure an ordered pool for a project, or remove it:

```bash
hop remote set <project> <host> [host ...]
hop remote rm <project>
hop remote list
hop remote list --json
```

`hop remote set` replaces the pool and removes duplicate hosts while preserving
their first-seen order. JSON keeps the top-level `remotes` key and returns rows
shaped as `{"project": str, "hosts": [str, ...]}`. In JSON output, `host`
on session and create results names one selected or resident host; inside
`unavailable_hosts`, it names the source that failed. `hosts` always names the
complete ordered pool. Host values beginning with `-`, containing control
characters, or equal to the reserved local-source name `local` are refused.
`hop remote set` also refuses active local projects; disable a moved project
before assigning its remote pool.

Pooled creation checks project readiness and active-lode load on every member,
then creates once on a least-loaded eligible host. It does not reserve capacity
and never tries another host after a create attempt. These probes require `hop
project list --json` on every remote host. Upgrade the fleet when deploying this
version. An older host is unavailable to pooled creation; there is no
compatibility fallback.
For `--coder codex`, readiness also requires `hop coder check codex --json`; a host
without a runnable Codex CLI is excluded before Hopper selects a destination.

After creation, Hopper stores a resident route from the lode ID to its resident
host. That route survives pool replacement or removal, so status, waiting, pane
actions, and lifecycle commands continue to reach the same host. Use `-H` for
explicit recovery when the resident route cannot be read or verified.

A single-source `hop lode list -p PROJECT` refuses when the project has a
configured remote pool because the local server cannot vouch for the complete
answer. Use `--all-hosts` to query the local server and all configured pool
hosts; `-p` filters returned rows, not which hosts are contacted. Unknown
project names report close registered-name suggestions.

`hop lode list --all-hosts` keeps rows from sources that answered and reports
the local and configured pool sources searched on stderr. Successful local-only
lists report their local source there as well, including JSON and empty results.
JSON still has the same keys: `lodes`, plus `unavailable_hosts` only for
`--all-hosts`. Each unavailable row contains `{"host": str, "reason": str}`,
and partial results exit 2.

`hop project list --json` and its `hop projects` alias emit project records with
`name`, `path`, `disabled`, and `disabled_reason`. Remote readiness requires
those fields while ignoring additive payload and row keys.

### Ship completion proof

During the ship stage, `hop processed` refuses completion unless the canonical
session worktree is clean and its HEAD is contained in a freshly fetched
upstream `main`, falling back to upstream `master` only when `main` is absent.
Without `origin`, the same stable, clean HEAD must be contained in local `main`,
or local `master` only when `main` is absent; missing or unlanded local defaults
fail closed. `hop processed` performs this proof only; it never merges, rebases,
commits, or pushes. A refusal keeps the session and its worktree intact and
prints recovery guidance for inspecting, cleaning, fetching, or landing before
retrying. Once accepted, the server closes the owned pane, proves the recorded
runner containment is empty, and publishes the terminal stage disposition.

## Key concepts
**Lode** -- a Claude Code session with a unique ID, selected refine-stage coder,
workflow stage, status, and associated tmux window.

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
make ci         # Auto-format, lint, and run all tests
make clean      # Remove build artifacts and caches
```
Single test: `pytest test/test_file.py::test_name`

## License
AGPL-3.0-only. Copyright (c) 2026 [sol pbc](https://solpbc.org).
