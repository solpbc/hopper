---
name: hop
description: Reference card for the hop CLI in hopper covering lode management, backlog items, and status updates inside a lode.
---

# hop Command Reference

## Context

- `HOPPER_LID` is set automatically when Claude runs inside a hopper lode.
- All commands below require the hopper server to be running.

## Status reporting

- `hop status` - Show current status and title.
- `hop status [-t TITLE] <text...>` - Update status text, optionally set title.
- `hop screenshot` - Capture TUI window content as ANSI text.
- `hop ping` - Check server connectivity and show tmux/lode info.

## Implementation request

Request new implementation work from outside your current lode:

```bash
hop implement myproject Fix login timeout
```

Or provide scope via stdin:

```bash
hop implement myproject <<'EOF'
Fix login timeout and add regression coverage
EOF
```

## Backlog management

- `hop backlog list` - List backlog items with ID, project, description, and age.
- `hop backlog add [-p project] <text...>` - Add a backlog item; if `-p` is omitted, project resolves from the current lode. Can also read description from stdin.
- `hop backlog remove <id-prefix>` - Remove a backlog item by ID prefix.

## Important note

- `hop implement` (and `hop lode create`) is **blocked inside a lode**. Use `hop backlog add` to queue future work instead.
