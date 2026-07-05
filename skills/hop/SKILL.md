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

Prints a status line as each lode resolves. Exit `0` if all shipped, `1` on error, `2` on timeout or gate, `3` if a lode is stuck (after a ~2-min grace period).

### For any wait past a few minutes, poll `hop lode status` — don't lean on a single long-lived `hop wait`

A lode routinely runs far longer than one blocking call survives, and both ways of holding a `hop wait` open fail on long lodes:

- **Foreground:** the Bash tool caps foreground calls at ~10 minutes. A longer lode trips a timeout → retry loop that burns wake cycles and replays the conversation past the ~5-minute prompt-cache TTL.
- **Background (`run_in_background: true`):** the harness reaps a quiet background process (~1 min in practice). `hop wait` is event-driven — it prints only on a state change — so a busy-but-silent lode produces no output, looks idle, and gets killed. (An earlier version of this doc claimed background + Monitor "stays in-process for the full lode duration" — that was wrong and cost sessions their waits.)

**Do this instead:** drive a poll loop (e.g. a Monitor loop) over `hop lode status <lode-id>` on an interval (~30–60s). `hop lode status` is a one-shot read that returns immediately, so it never trips the foreground cap or the idle reaper.

### Reading the status — three traps

`hop lode status` prints a `stage:` line and a `state:` line. `stage` walks `mill → refine → ship → shipped`; `state` is the within-stage condition (`new`, `running`, `stuck`, `completed`, `error`, `gated`).

1. **`state: completed` is a STAGE boundary, not the finish.** State flips to `completed` at the end of *each* stage (mill done, refine done, ship done), then the next stage begins. **The only terminal success signal is `stage: shipped`** — key your loop on that, never on `state: completed`.
2. **Debounce `stuck` — one poll is not a wedge.** A single `state: stuck` reading is usually the model thinking mid-stage, not a hang; `hop wait` itself waits ~2 min before treating stuck as terminal. Require it to persist (~4 consecutive polls) before diagnosing the pane (see § Stuck lodes).
3. **`hop wait` exit `0` is not proof of a ship.** A wait can return before the lode genuinely shipped — reaped in the background, or the lode archived without a clean ship. **Never treat exit `0` (or a vanished background wait) as done on its own** — confirm `stage: shipped` via `hop lode status` first.

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
primary stuck signal for the senior Claude-driven mill/refine/ship runners. If
a senior Claude stage stays stuck past the runner timeout, Hopper terminates it,
marks the lode `error`, and releases `active` so `hop restart <id>` can retry.

Refine setup also bounds `make install` and Codex bootstrap. If setup hits its
timeout, the lode errors with the captured output tail instead of remaining
active at "Running make install...".

If the action is safe (e.g. a routine permission prompt, a test confirmation),
use send-keys to unblock it:

    tmux send-keys -t <pane> '1' Enter

If the pane shows something you're not comfortable resolving (destructive action,
ambiguous approval, sensitive operation), leave it for the founder to resolve.
