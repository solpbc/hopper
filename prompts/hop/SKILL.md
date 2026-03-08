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

Create a new lode with required `project` arg and scope from stdin (`hop implement` is an alias for `hop lode create`):

```bash
cat scope.md | hop implement myproject

hop implement myproject <<'EOF'
Fix login timeout and add regression coverage
EOF

cat scope.md | hop lode create myproject
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
echo "Improve retry logic for the auth service when upstream returns 503" | hop implement myproject
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
