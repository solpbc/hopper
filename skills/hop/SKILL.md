---
name: hop
description: >
  Complete CLI reference for hopper — lode management, waiting, diagnostics,
  status reporting. Covers external coordination and in-lode usage. TRIGGER:
  hop implement, hop submit, hop list, hop wait, hop lode, hop project, hop
  config — creating a scope, checking lode status, reviewing lode output.
---

# hop CLI Reference

## Context

- `HOPPER_LID` is set when Claude runs inside a hopper lode.
- All commands require the hopper server to be running (`hop ping` to check).
- Commands marked **(outside lode only)** are blocked when `HOPPER_LID` is set.
- Remote hopper hosts are reached through the same `hop` CLI. Use `hop remote`
  for project routing and `hop -H <host> ...` for an explicit one-off host.
  Routed commands print `→ <host> (...)` to stderr; remote lodes are cached in
  `remote-lodes.json` so follow-up status/recovery commands do not need host
  rediscovery.

## Creating work

Submit scope for immediate implementation **(outside lode only)**. Scope is always provided via stdin:

```bash
cat scope.md | hop implement myproject

hop implement myproject <<'EOF'
Fix login timeout and add regression coverage
EOF
```

`hop implement` is an alias for `hop lode create`. `hop submit` is an alias for `hop implement`. Use `--force` to override dirty-repo checks. Scope must be at least 42 characters. Add `--json` when a wrapper needs the lode id as data.

If `remote.<project>` is configured and the project is disabled or absent
locally, `hop implement <project>` forwards to that host automatically. A
locally active project always wins.

## Remote host registry

```bash
hop remote list
hop remote list --json
hop remote set solstone-android suze.local
hop remote rm solstone-android

hop -H pro5e.local lode list
hop -H local lode list
```

`hop remote set` refuses active local projects; disable a moved project first.
The remote install contract is `$HOME/.local/bin/hop`, installed by
`make install-user`.

## Waiting and monitoring

Block until one or more lodes ship **(outside lode only)**:

```bash
hop wait <lode-id>
hop wait <id1> <id2> <id3>
hop wait <lode-id> --timeout 300
hop wait <lode-id> --poll 30 --json
```

Prints a status line as each lode resolves. Exit codes are disambiguated:
`0` all shipped, `1` error/not-found/not-active, `2` gated, `3` stuck,
`4` timeout. For remote lodes, `hop wait` polls remote status internally at the
configured `--poll` interval and applies the same terminal-state rules. Do not
hand-roll SSH polling loops for remote lodes.

### Reading the status — three traps

`hop lode status` prints a `stage:` line and a `state:` line. `stage` walks `mill → refine → ship → shipped`; `state` is the within-stage condition (`new`, `running`, `stuck`, `completed`, `error`, `gated`).

1. **`state: completed` is a STAGE boundary, not the finish.** State flips to `completed` at the end of *each* stage (mill done, refine done, ship done), then the next stage begins. **The only terminal success signal is `stage: shipped`** — key your loop on that, never on `state: completed`.
2. **Debounce `stuck` — one poll is not a wedge.** A single `state: stuck` reading is usually the model thinking mid-stage, not a hang; `hop wait` itself waits ~2 min before treating stuck as terminal. Require it to persist (~4 consecutive polls) before diagnosing the pane (see § Stuck lodes).
3. **`hop wait` timeout is exit `4`, not gate.** Gate is exit `2`; wrappers can now branch cleanly.

Watch live status events for a lode **(outside lode only)**:

```bash
hop lode watch <lode-id>
```

Practical create + wait workflow:

```bash
cat scope.md | hop implement myproject
# note the lode ID from output, then poll to completion:
hop lode status <lode-id>   # repeat on an interval until: stage: shipped
```

## Lode management

List active lodes (`hop lode` defaults to `list`):

```bash
hop lode
hop lode list
hop lode list -a          # include archived
hop lode list -p PROJECT  # filter by project name
hop lode list --json
hop lode list --all-hosts # aggregate configured remote hosts
hop list                  # alias for lode list (same flags)
```

Show detailed status for a lode:

```bash
hop lode status <lode-id>
hop lode status <lode-id> --json
hop lode show <lode-id>   # alias for status
```

Restart an inactive lode (error, stuck, or failed ship):

```bash
hop lode restart <lode-id>
hop lode restart <lode-id> --force   # also restarts active lodes with a dead pane
```

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
hop lode peek <lode-id>             # plain-text tail of the lode pane
hop lode nudge <lode-id>            # submit "continue" via buffer paste
hop lode nudge <lode-id> --text "..."
hop lode answer <lode-id> 1         # answer numbered prompts
```

### Stuck lodes

When `hop lode status` shows a lode in `stuck` state, inspect it through hop:

    hop lode peek <lode-id>

Common causes: permission prompt waiting for input, process hung, or waiting for
human approval.

For Codex refine runs, recent JSON heartbeat progress can keep a lode in
`running` even if the tmux pane text has not changed yet. Pane-diff remains the
primary stuck signal for the senior Claude-driven mill/refine/ship runners. If
a senior Claude stage stays stuck past the runner timeout, Hopper terminates it,
marks the lode `error`, and releases `active` so `hop restart <id>` can retry.

Refine setup also bounds `make install` and Codex bootstrap. If setup hits its
timeout, the lode errors with the captured output tail instead of remaining
active at "Running make install...".

If the action is safe (e.g. a routine permission prompt, a test confirmation),
use the recovery primitives:

    hop lode nudge <lode-id>
    hop lode answer <lode-id> 1

If the pane shows something you're not comfortable resolving (destructive action,
ambiguous approval, sensitive operation), leave it for the founder to resolve.
