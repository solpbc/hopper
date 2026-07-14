---
name: hop
description: >
  Complete CLI reference for hopper — lode management, waiting, diagnostics,
  status reporting. Covers external coordination and in-lode usage. TRIGGER:
  hop implement, hop submit, hop list, hop wait, hop lode, hop project, hop
  config — creating a scope, checking lode status, reviewing lode output.
---

# hop CLI Reference

Invoke via Bash: `hop <command> [flags]`.

## Context

- `HOPPER_LID` is set when Claude runs inside a hopper lode.
- Commands that query or mutate live lodes require the hopper server to be
  running (`hop ping` to check). Local commands such as `hop check` and config
  management do not.
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

## Starting hopper

`hop up` permits one server for its socket. If another server is responsive,
attach to its existing hopper session or stop it before retrying. A racing
second start reports the PID holding the singleton lock when available. If the
socket accepts connections but does not answer, `hop up` refuses to start a
replacement; retry after the existing server recovers, or stop it if it is
wedged.

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

Runner spawn problems remain visible in lode status. `spawn refused:` means
hopper did not launch a duplicate: attach when the recorded pane is live, or
verify tmux is running and retry when tmux liveness is unknown. `spawn failed:`
means tmux did not create the runner pane; verify tmux is running, then retry.
These messages do not change the lode's workflow state.

Backlog add/remove operations update the local backlog directly only when the
server is provably down. If a server socket is listening but unresponsive, they
refuse instead of risking an update concurrent with the live server.

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

## Running validation checks

Run a build/test/lint command, print only the tail of its output, and exit with
the command's **real** status. Use this instead of piping to a pager — a plain
`make ci 2>&1 | tail -30` reports `tail`'s exit code, not make's, so a red build
silently looks green.

```bash
hop check -- make ci                # run make ci; last 50 lines + explicit "exited N"
hop check -- make test
hop check -n 20 -- make ci          # keep only the last 20 lines of output
```

`hop check` buffers combined stdout+stderr, prints the trailing lines, then
prints `hop check: `<cmd>` exited N` and returns N. A non-zero exit is a failed
check. Runs locally in the current directory; does not need the server.

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
hop screenshot                      # render TUI window as ANSI text
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

**New project, first lode: workspace-trust dialog.** The very first lode run
against a freshly `hop project add`-ed directory can wedge silently on Claude
Code's one-time workspace-trust prompt ("Do you trust the files in this
folder?"). The pane shows only the prompt and produces zero output, so
Hopper's liveness model times it out (~351s) and errors the lode; restarting
reproduces the same wedge. `hop lode peek <lode-id>` confirms it's the trust
dialog. Recovery: `hop lode answer <lode-id> 1` (accepts the prompt); the
lode then proceeds normally, and later lodes on the same project don't hit
it again.

**Long output-silent test runs are protected by `hop check` heartbeats.** While
its child runs, `hop check` emits a socket progress heartbeat every 30 seconds,
so a healthy output-silent run is no longer killed with `No output or progress
for 351s`. Output is still buffered and only the tail is printed after the
command exits; the heartbeat supplies liveness without changing that contract.

Hopper's liveness model uses pane-diff activity, in-flight Codex exec
heartbeats, and descendant-process CPU activity. Pane and heartbeat silence are
the real foreground signals; descendant CPU can keep a lode `running` while
background work is active. Heartbeat or CPU activity can carry a quiet stage,
but neither bypasses the 60-minute pane-silence cap.

If a senior Claude stage stays stuck past the runner timeout, Hopper terminates
it, marks the lode `error`, and releases `active` so `hop restart <id>` can
retry. Every stuck-kill writes `recovery.json` under the lode directory with a
snapshot outcome (committed SHA, clean, no worktree, or failed with the git
error); `hop lode status <id>` surfaces the record. Worktree cleanup also
refuses to destroy a dirty worktree; it retains the path and logs a warning
instead.

On a fresh Make-based worktree, refine setup prefers `make hopper-install` when
the project declares that target and otherwise falls back to `make install`.
Use `hopper-install` for the dependencies and agent tooling needed to edit and
run unit CI; keep host runtime provisioning and large model/artifact downloads
in the project's normal install target.

The selected setup target is bounded by a 20-minute **inactivity** timeout and
a 60-minute absolute cap. Command output and descendant CPU count as progress
on every host; Linux also observes process-tree I/O. A moving artifact download
can therefore cross 20 minutes while a wedged download still fails. The lode
error distinguishes inactivity from the absolute cap and includes the bounded
output tail instead of remaining active at the setup status. Codex bootstrap
is bounded separately.

`hop code` prints a `CODEX TURN FAILED` banner when the backend fails a turn.
Usage-limit failures are fleet-wide because Hopper uses one shared Codex seat
across all hosts, so the in-lode agent should implement the stage directly
under the same review bar rather than retrying `hop code`.

If the action is safe (e.g. a routine permission prompt, a test confirmation),
use the recovery primitives:

    hop lode nudge <lode-id>
    hop lode answer <lode-id> 1

If the pane shows something you're not comfortable resolving (destructive action,
ambiguous approval, sensitive operation), leave it for the founder to resolve.
