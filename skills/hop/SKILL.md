---
name: hop
description: >
  Complete CLI reference for hopper — lode management, waiting, diagnostics,
  status reporting. Covers external coordination and in-lode usage. TRIGGER:
  hop implement, hop submit, hop list, hop wait, hop lode, hop backlog, hop
  project, hop config — creating a scope, checking lode status, reviewing
  lode output.
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

`hop implement` is an alias for `hop lode create`. `hop submit` is an alias for `hop implement`. Use `--force` to override dirty-repo checks. Scope must be at least 42 characters.

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
hop lode list -p PROJECT  # filter by project name
hop list                  # alias for lode list (same flags)
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

## Backlog management

Manage backlog items for a project. Items track future work:

```bash
hop backlog                           # list items (default action)
hop backlog list -p PROJECT           # list items for a specific project
hop backlog add -p PROJECT "desc"     # add an item
hop backlog remove PREFIX             # remove by ID prefix
hop backlog promote PREFIX            # promote item to a lode
hop backlog queue PREFIX              # assign item to queue for next pickup
hop backlog queue PREFIX --clear      # clear queued assignment
```

`-p PROJECT` is required when not inside a lode. Inside a lode, project is inferred from context.

## Project management

Manage hopper projects. Projects are git directories where lodes run:

```bash
hop project                           # list projects (default action)
hop project list
hop projects                          # alias for project list
hop project add /path/to/repo         # register a project
hop project remove NAME               # unregister a project
hop project rename NAME NEW_NAME      # rename a project
```

## Configuration

Get or set hopper config values. Config values are available as `$variables` in prompts:

```bash
hop config                            # list all config (default action)
hop config list
hop config get KEY                    # get a specific value
hop config set KEY VALUE              # set a value
hop config delete KEY                 # remove a value
hop config json                       # dump config as JSON
hop config path                       # show config file path
```

## Status reporting (inside a lode)

```bash
hop status                          # show current status and title
hop status [-t TITLE] <text...>     # update status text, optionally set title
```

## Internal lode commands (inside a lode only)

These commands only work when `HOPPER_LID` is set (i.e., inside a running lode):

```bash
hop processed <<'EOF'                 # signal stage completion with output
<stage output>
EOF

hop gate <<'EOF'                      # pause lode at a review gate
<review document>
EOF

hop code <stage>                      # run prompts/<stage>.md via Codex
```

## Responding to a gate

- Use these after a lode prints a gate banner and waits for your reply.
- `hop feedback <lode-id>` is an alias and accepts the same inline and stdin forms.

```bash
hop gate show <lode-id>                        # view the gate prompt
hop gate feedback <lode-id> "approved, ship it"
hop gate feedback <lode-id> < feedback.md
hop gate feedback <lode-id> - < feedback.md
cat feedback.md | hop gate feedback <lode-id> -

hop feedback <lode-id> "approved, ship it"
hop feedback <lode-id> < feedback.md
hop feedback <lode-id> - < feedback.md
cat feedback.md | hop feedback <lode-id> -
```

## Diagnostics

```bash
hop ping                            # check server connectivity
hop screenshot                      # capture TUI window as ANSI text
```

### Stuck lodes

When `hop lode status` shows a lode in `stuck` state, the `pane:` field tells
you where to look. Capture the pane to see what's happening:

    tmux capture-pane -t <pane> -p -S -50

Common causes: permission prompt waiting for input, process hung, or waiting for
human approval.

For Codex refine runs, recent JSON heartbeat progress can keep a lode in
`running` even if the tmux pane text has not changed yet. Pane-diff remains the
primary stuck signal for the senior Claude-driven mill/refine/ship runners.

If the action is safe (e.g. a routine permission prompt, a test confirmation),
use send-keys to unblock it:

    tmux send-keys -t <pane> '1' Enter

If the pane shows something you're not comfortable resolving (destructive action,
ambiguous approval, sensitive operation), leave it for the founder to resolve.
