---
name: using-hopper
description: Teaches agents to use the hop CLI for reporting status, signaling stage completion, managing the backlog, and coordinating lodes within hopper. Use this skill when working inside a hopper lode and needing to communicate with the hopper server.
---

## A. Environment

`HOPPER_LID` is set automatically when hopper spawns the agent and identifies the current lode.

- Required by `hop status` and `hop processed`.
- Used optionally by `hop backlog add` to auto-resolve project.
- Used optionally by `hop ping` to validate the lode.

## B. Status Reporting

`hop status` requires `HOPPER_LID` and a running hopper server.

Show current status and title:

```bash
hop status
```

Set status text visible in the TUI dashboard:

```bash
hop status Investigating auth bug
```

Set a short title for the lode:

```bash
hop status -t "auth-fix"
```

Combine title and status in one command:

```bash
hop status -t "auth-fix" Investigating auth module
```

## C. Stage Completion

Signal stage completion. Reads output from stdin, saves it to `<lode_dir>/<stage>_out.md`, and auto-advances to the next stage. Requires `HOPPER_LID`, a running server, and non-empty stdin.

```bash
hop processed <<'EOF'
Summary of what was accomplished in this stage.
EOF
```

## D. Backlog Management

List backlog items (`hop backlog` defaults to list, and `hop backlog list` is equivalent):

```bash
hop backlog
hop backlog list
```

Add a backlog item with project auto-resolved from the current lode (`HOPPER_LID`):

```bash
hop backlog add Refactor auth module for clarity
```

Add a backlog item with explicit project (`--project` / `-p`):

```bash
hop backlog add -p myproject Add rate limiting to API
```

Add a backlog item from stdin/heredoc:

```bash
hop backlog add -p myproject <<'EOF'
Refactor auth module for clarity
EOF
```

## E. Lode Management

List active lodes (`hop lode` defaults to list, and `hop lode list` is equivalent):

```bash
hop lode
hop lode list
```

Create a new lode and start processing immediately:

```bash
hop lode create myproject Add user authentication flow
```

Create a lode with scope from stdin/heredoc:

```bash
hop lode create myproject <<'EOF'
Add user authentication flow
EOF
```

Watch a lode until it reaches `shipped`, enters `error`, or is archived (exit code `1` on error):

```bash
hop lode watch abc123
```

Practical create + watch pattern:

```bash
hop lode create myproject Fix login timeout
# note the lode ID from output, then:
hop lode watch <lode_id>
```

## F. Diagnostics

Capture the TUI window as ANSI text:

```bash
hop screenshot
```

Check if the server is running; with `HOPPER_LID` set, also validate the lode and print `pong` with tmux and lode info:

```bash
hop ping
```
