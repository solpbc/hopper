---
name: hop
description: Complete CLI reference for hopper — lode management, waiting, diagnostics, and status reporting. Covers both external coordination and in-lode usage. TRIGGER: submitting new work to hopper — hop implement, creating a lode, writing or submitting a scope.
---

# hop CLI Reference

## Context

- `HOPPER_LID` is set when Claude runs inside a hopper lode.
- All commands require the hopper server to be running (`hop ping` to check).
- Commands marked **(outside lode only)** are blocked when `HOPPER_LID` is set.

## Creating work

Submit scope for immediate implementation **(outside lode only)**. Scope is always provided via stdin:

```bash
cat scope.md | hop implement myproject

hop implement myproject <<'EOF'
Fix login timeout and add regression coverage
EOF
```

`hop implement` is an alias for `hop lode create`. Use `--force` to override dirty-repo checks.

## Waiting and monitoring

Block until one or more lodes ship **(outside lode only)**:

```bash
hop wait <lode-id>
hop wait <id1> <id2> <id3>
hop wait <lode-id> --timeout 300
```

Prints a status line as each lode resolves. Exit 0 if all shipped, 1 on error, 2 on timeout.

Watch live status events for a lode **(outside lode only)**:

```bash
hop lode watch <lode-id>
```

Practical create + wait workflow:

```bash
cat scope.md | hop implement myproject
# note the lode ID from output
hop wait <lode-id>
```

## Lode management

List active lodes (`hop lode` defaults to `list`):

```bash
hop lode
hop lode list
hop lode list -a          # include archived
```

Show detailed status for a lode:

```bash
hop lode status <lode-id>
hop lode show <lode-id>   # alias for status
```

Restart an inactive lode (error, stuck, or failed ship):

```bash
hop lode restart <lode-id>
```

## Status reporting (inside a lode)

```bash
hop status                          # show current status and title
hop status [-t TITLE] <text...>     # update status text, optionally set title
```

## Diagnostics

```bash
hop ping                            # check server connectivity
hop screenshot                      # capture TUI window as ANSI text
```
