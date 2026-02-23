---
name: hop-external
description: Commands for external agents to create lodes, manage backlog, and monitor progress via the hop CLI. Use this skill when coordinating with a running hopper instance from outside a lode.
---

## A. Lode Management

List active lodes (`hop lode` defaults to `list`; `hop lode list` is equivalent):

```bash
hop lode
hop lode list
hop lode list -a
```

Use `-a` or `--archived` to show archived lodes.

Create a new lode with required `project` and optional `scope` args (`hop implement` is an alias for `hop lode create`):

```bash
hop lode create myproject Fix login timeout
hop implement myproject Fix login timeout
```

If `scope` is omitted, provide it on stdin:

```bash
hop lode create myproject <<'EOF'
Fix login timeout and add regression coverage
EOF

hop implement myproject <<'EOF'
Fix login timeout and add regression coverage
EOF
```

Restart an inactive lode by ID (only valid for inactive lodes in `mill`, `refine`, or `ship`):

```bash
hop lode restart abc123
```

Watch a lode by ID until it reaches `shipped`, enters `error`, or is archived (exit code `1` on error):

```bash
hop lode watch abc123
```

Practical create + watch workflow:

```bash
hop implement myproject Improve retry logic
# note the new lode ID from output
hop lode watch <lode_id>
```

## B. Backlog Management

List backlog items (`hop backlog` defaults to `list`; `hop backlog list` is equivalent):

```bash
hop backlog
hop backlog list
```

Add a backlog item with explicit project (`-p` / `--project`) and inline description:

```bash
hop backlog add -p myproject Add request timeout metrics
```

`description` comes from positional args, or from stdin when omitted:

```bash
hop backlog add -p myproject <<'EOF'
Add request timeout metrics and dashboard alerts
EOF
```

Remove a backlog item by ID prefix:

```bash
hop backlog remove 7f3a
```

## C. Diagnostics

Check whether the server is running:

```bash
hop ping
```

On success, prints `pong` with tmux information.

Capture the TUI window as ANSI text:

```bash
hop screenshot
```
