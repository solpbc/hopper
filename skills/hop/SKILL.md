---
name: hop
description: >
  Complete CLI reference for hopper: lode management, waiting, diagnostics,
  status reporting. Covers external coordination and in-lode usage. TRIGGER:
  hop implement, hop submit, hop list, hop wait, hop lode, hop project, hop
  config: creating a scope, checking lode status, reviewing lode output.
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
  for ordered project host pools and `hop -H <host> ...` for an explicit
  one-off host. Routed commands print `→ <host> (...)` to stderr. Hopper stores
  a resident route for each remote lode so later commands return to its
  resident host even if the project's pool changes or is removed.

## Creating work

Submit scope for immediate implementation **(outside lode only)**. Scope is always provided via stdin:

```bash
cat scope.md | hop implement myproject

hop implement myproject <<'EOF'
Fix login timeout and add regression coverage
EOF
```

`hop implement` is an alias for `hop lode create`. `hop submit` is an alias for `hop implement`. Use `--force` to override dirty-repo checks. Scope must be at least 42 characters. Add `--json` when a wrapper needs the lode id as data. Grok is the default refine-stage coder; add `--coder codex` at creation time to select Codex for that lode. Hopper does not set a Grok model name, so Grok uses the authenticated account's current CLI default.

If `remote.<project>` has a pool and the project is disabled or absent locally,
`hop implement <project>` probes every pool member concurrently, compares
active-lode load, and creates once on a least-loaded eligible host. A locally
active project always wins. There is no reservation or fallback create after an
attempt.

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
hop remote set <project> <host> [host ...]
hop remote rm <project>

hop -H <host> lode list
hop -H local lode list
```

`hop remote set` replaces the ordered pool, deduplicating hosts by first
appearance. Host values beginning with `-`, containing control characters, or
equal to the reserved local-source name `local` are refused. `hop remote list
--json` returns
`{"remotes": [{"project": str, "hosts": [str, ...]}]}`. In JSON output,
`host` on session and create results is one selected or resident host; inside
`unavailable_hosts`, it is the source that failed. `hosts` is always the
complete pool. `hop remote set` refuses active local projects, so disable a
moved project first.

Pooled readiness depends on `hop project list --json` being installed on every
remote host. A Codex lode also requires `hop coder check codex --json`; hosts without
a runnable Codex CLI are excluded before selection. Upgrade the fleet after this
version lands. An older host probes as unavailable; do not add a compatibility
fallback. The remote install contract is `$HOME/.local/bin/hop`, installed by
`make install-user`.

## Waiting and monitoring

Supervise exactly one lode to a final outcome **(outside lode only)**:

```bash
hop wait <lode-id>
hop watch <lode-id>
hop lode wait <lode-id>
hop wait <lode-id> --timeout 300
hop wait <lode-id> --observer-timeout 300
```

Top-level `hop watch` is an exact alias for `hop wait`: same options, supervisor,
record, and exit status. `hop lode watch <id>` is different—it streams status
events and does not provide the bounded final-record contract.

`--timeout` limits the whole operation, including resolution and remote probes.
It defaults to 3,600 seconds and cannot exceed 3,600; zero, negative, non-finite,
and larger values are rejected before lookup or SSH. `--observer-timeout` limits
how long authoritative status may remain unavailable. Setting it to `0` disables
only that freshness failure; the overall one-hour deadline still applies. Socket
events only accelerate reconciliation, and durable active/archive status decides
the outcome. Do not hand-roll SSH polling loops.

### Run the wait bare

The command string must be the wait itself:

```bash
hop wait <lode-id>
```

Do not put it behind `timeout`, `nohup`, `ssh`, `sh -c`, `bash -lc`, a shell
function, command substitution, a background `&`, `; echo $?`, `&&`, output
redirection, a pipeline, `head`, `tail`, or a pager. Those wrappers can kill or
detach the real waiter, hide its output, or report a different process's status.
If the calling tool already launches commands through a shell internally, keep
the submitted command string to exactly `hop wait ...` or `hop watch ...`.

### Read the record, not only the exit code

The target emits one final human block, or one JSON object with `--json`. Read
the complete output even when the exit code is zero. The record carries:

- `outcome`, stable `reason_code`, plain-language `reason`, and `recovery`;
- owning `server`, `route`, and bounded source `probes`;
- exact `stage`, `state`, `status`, active/archive state, and freshness;
- current or last-known tmux pane; and
- exact known worktree path, provenance, and fresh existence result.

The command's numeric exit is the final record's exit code: `0` shipped, `1`
error/inactive/archive/resolution failure, `2` gated or durable action attention,
`3` confirmed stuck, `4` overall timeout or status/observer unavailability, and
`130` operator interrupt. The code alone is insufficient: exit `4`, for example,
covers materially different recoveries. Use `outcome`, `reason_code`, and
`recovery` to decide what happened and what to do next.

With `--json`, stdout is JSONL containing only the final record; the always-human
command summary remains on stderr. In a JavaScript executor, inspect and forward
both the returned `exit_code` and the complete `output`; never reduce the result
to one of them.

### Reading the status: three traps

`hop lode status` prints a `stage:` line and a `state:` line. `stage` walks `mill → refine → ship → shipped`; `state` is the within-stage condition (`new`, `running`, `stuck`, `teardown`, `error`, `gated`).

1. **`state: teardown` is not terminal.** Hopper has accepted an action and is durably closing and proving runner containment before the terminal disposition. For the final workflow stage, key status loops on `stage: shipped`, never on a teardown status line.
2. **Debounce `stuck`: one reading is not terminal.** `hop wait` allows a 120-second grace before treating `stuck` as terminal, including when the first snapshot is already stuck, then confirms it with another authoritative read. Wait through the full grace before diagnosing the pane (see § Stuck lodes).
3. **Use the complete exit-code table above; do not reconstruct it from this
   example.** In particular, `hop wait` timeout is exit `4`, not gate; gate is
   exit `2`.

Watch live status events for a lode **(outside lode only)**:

```bash
hop lode watch <lode-id>
```

Watch, wait, status, pane actions, and lifecycle commands first consult the
resident route. A retained route is authoritative even when its resident host
has left every pool. If that host reports the lode absent or is unavailable,
Hopper does not fan out to another pool member; use the printed `hop -H <host>`
recovery command.

Practical create + blocking-wait workflow:

```bash
cat scope.md | hop implement myproject
# note the lode ID from output, then block until it finishes:
hop wait <lode-id>
```

## Lode management

List active lodes (`hop lode` defaults to `list`):

```bash
hop lode
hop lode list
hop lode list -a             # archived, newest 20
hop lode list -a --offset 20 # the next 20 older
hop lode list -a -n 100      # a bigger page (max 200)
hop lode list -p PROJECT     # filter by project name
hop lode list --json
hop lode list --all-hosts    # aggregate local and all pool hosts
hop list                     # alias for lode list (same flags)
```

**The archive is paged; the active list is not.** `-a` returns the newest 20
rows and nothing else, because the archive only grows — on the busiest host it
is 3,400+ rows and 30 MB, which the whole listing used to try to carry across
the socket and fail. Every archived listing prints which slice of what it is
showing on stderr (`Showing archived 1-20 of 3456, newest first`) and the exact
command for the next page. `-n`/`--limit` resizes the page and `--offset` walks
backwards through older rows. In JSON, `total` is the whole filtered archive
and `offset`/`limit` are the page that was cut; `total` is `null` under
`--all-hosts`, where no single source knows the fleet's count.

⛔ **There is no "give me everything" flag, and `--limit` is capped at 200 — this
is a real bound, not an ergonomic default.** The server sends the whole response
under its own 2.0-second socket timeout, so an oversized payload is delivered
*partially* and the next broadcast is spliced onto the fragment. Measured on
fedora: 200 rows is 4.1 MB in 0.53s, 400 rows is 7.0 MB in 1.47s, **500 rows
fails** — and a loaded host reaches that cliff sooner. Walk the archive with
`--offset` instead. (Mechanism and the deferred server-side fix:
`cto/workspace/hop-hopper-archived-listing-and-archive-contract.md` in the extro
repo.)

⚠ `--offset` is refused with `--all-hosts`, because serving it would require
every host to send offset+limit rows — the oversized response the cap exists to
prevent. Page one host at a time with `hop -H <host>`.

⚠ A server older than paged listing answers `Lode listing unavailable`; restart
it after pulling rather than reading a partial page as the whole archive.

A single-source `hop lode list -p PROJECT` refuses when that project has a
configured remote pool because the local server cannot vouch for the complete
answer. Use `--all-hosts` to query the local server and all configured pool
hosts; `-p` filters returned rows, not which hosts are contacted. Unknown
project names report close registered-name suggestions.

Every successful list reports its searched sources on stderr, including JSON
and empty results. `--all-hosts` preserves rows from sources that answer. Its JSON
object includes `unavailable_hosts`, an array of `{"host": str, "reason": str}`
rows. Partial discovery exits 2; complete discovery exits 0. The JSON payload is
unchanged; source disclosure is never added to stdout. Do not discard proven rows
just because another source failed.

Show detailed status for a lode:

```bash
hop lode status <lode-id>
hop lode status <lode-id> --json
hop lode show <lode-id>   # alias for status
hop status <lode-id>
hop status <lode-id> --json
hop lode path <lode-id>
hop lode path <lode-id> --json
```

`hop lode list/status --json` lode objects and `hop wait --json` JSONL terminal
records include `status_display`, the human-facing derived status, and
`pane_liveness`, which is `alive`, `gone`, `unknown`, or `not_probed`. The existing
`status` field remains the stored string; consumers opt into the derived view through
`status_display`.

**An archived lode says so, and its live-looking fields are as-archived.**
`active`, `tmux_pane`, `state` and the park record are frozen at archive time
and nothing clears them, so `hop lode status` on an archived row prints
`archived: yes`, reports `active: no (archived)`, drops the stale pane, says the
agent is `gone; the lode was archived`, and offers no gate review. `hop lode
peek` names the archived condition instead of quoting the frozen fields. ⛔ Read
an archived row as a record of what happened, never as a live lode needing
recovery — an archived-eight-days row was reported and triaged as a live wedge
on 2026-08-15 exactly because these fields rendered as current.

`hop lode status` also prints an `unpushed:` line whenever the lode still has a
worktree. It is the number of commits that exist **only** in that worktree, reachable
from no remote-tracking ref. Read it before you believe a finished-looking lode
is finished: only `stage: shipped` merges and pushes, so a lode that stalled
earlier can be complete, committed, clean, and entirely absent from the remote.
`UNKNOWN` means the check could not be made; it never means zero. Kill and
archive enforce this durability proof at the server boundary, including a
second check after containment is empty and before archive publication.

Restart an inactive lode (error, stuck, or failed ship):

```bash
hop lode restart <lode-id>
hop lode restart <lode-id> --force   # also restarts active lodes with a dead pane
```

Archive an already-inactive lode whose recorded run ownership is unavailable:

```bash
hop lode archive <lode-id>
```

Status recommends `archive` only when the lode is inactive, its pane and process
handles are empty, and its generation ownership cannot be loaded. The server then
archives the stale row while retaining the worktree and branch. Submit the scope as
a new lode if it still needs to run. Restart remains the recovery when ownership is
available.

`hop lode archive` refuses unless the lode record's `active` flag is false and its
recorded `tmux_pane`, `pid`, and `oom_scope` fields are all empty. This checks the
lode record, not the machine: a recorded handle to a pane that has already been
killed still blocks archive, because the command reads recorded fields and does
not probe the pane or process. The refusal names the fields that blocked it.

Kill a lode (the pane and process go; the worktree and branch are retained):

```bash
hop lode kill <lode-id>
hop lode kill <lode-id> --force   # kill despite unpushed commits
hop kill <lode-id>                # alias
```

**Kill refuses when the branch carries commits that exist only in the
worktree**, and it refuses just as hard when it cannot prove the count. A clean
worktree is not evidence the work is safe. Commits fast-forwarded onto a
*local* main but never pushed still count. Pushing the branch clears the guard
even without merging, which is the fast way to make a stalled lode safe. The
refusal prints the worktree path, the commands to inspect and push, and the
`--force` escape. Push first, then kill.

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
hop project list --json
hop projects                          # alias for project list
hop project add /path/to/repo         # register a project
hop project remove NAME               # unregister a project
hop project rename NAME NEW_NAME      # rename a project
```

Project-list JSON is
`{"projects": [{"name": str, "path": str, "disabled": bool,
"disabled_reason": str}]}`. Pooled readiness requires these fields and ignores
additive keys in the remote payload and project rows.

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

Run a build/test/lint command bare in a terminal, print only the tail of its
output, and exit with the command's **real** status. Use this instead of piping
to a pager. A plain
`make ci 2>&1 | tail -30` reports `tail`'s exit code, not make's, so a red build
silently looks green.

```bash
hop check -- make ci                # run make ci; last 50 lines + explicit "exited N"
hop check -- make test
hop check -n 20 -- make ci          # keep only the last 20 lines of output
hop check --allow-capture -- make ci   # from a tool call with captured stdout
```

`hop check` buffers combined stdout+stderr, prints the trailing lines, then
prints `hop check: `<cmd>` exited N` and returns N. A non-zero exit is a failed
check. It refuses non-terminal stdout before starting the command, so a pipe
cannot make an unrun validation look successful. Use `-n` to bound output
instead. Runs locally in the current directory; does not need the server.

**If you are an agent calling this from a tool, add `--allow-capture`.** A tool
call has no TTY, so the bare form refuses and nothing runs. `--allow-capture` is
your promise that your stdout is *captured* (your harness hands you the exit
code) rather than *piped* into another command that would replace it. With it,
you get the command's real status.

**Warning:** Do not work around the refusal by detaching the gate. `nohup … &`
or a trailing `&` returns the *launcher's* status, not the job's, so the gate
result becomes unverifiable. Do not hand-roll a pty with `pty.spawn`, either:
it returns a **wait-status, not an exit code** (256 means exit 1, and it is
truthy), so reading it directly has the same problem. Use `--allow-capture`.

## Internal lode commands (inside a lode only)

These commands only work when `HOPPER_LID` is set (i.e., inside a running lode):

```bash
hop processed <<'EOF'                 # durably submit stage output
<stage output>
EOF

hop gate <<'EOF'                      # pause lode at a review gate
<review document>
EOF

hop code <stage>                      # run prompts/<stage>.md via the lode's coder
```

`hop processed` submits exact bytes and returns after the server durably accepts
the action. Acceptance stages those bytes before teardown begins; the CLI does
not write the canonical output file. The server then closes the owned pane and
proves the recorded runner containment is empty independently before advancing.
In ship, the server later re-proves landing against the canonical session
worktree: it must be clean, and one stable HEAD must be contained in freshly
fetched upstream `main`, falling back to upstream `master` only when `main` is
absent. Without `origin`, that same stable, clean HEAD must be contained in local
`main`, or local `master` only when `main` is absent. Hopper verifies only: it
never merges, rebases, commits, or pushes. If submission is refused, follow the
printed recovery. If its disposition is unknown, inspect `hop lode status`
before taking another action; do not submit the output again blindly.

## Responding to a gate

- Use these after a lode prints a gate banner and waits for your reply. Exit 0 from
  `hop gate feedback` means Claude accepted a new user turn; any reported failure
  leaves the lode gated and prints a safe next action.
- If failure says the delivery outcome is unknown, run `hop lode peek <lode-id>`
  before deciding whether to retry; never resend blindly. `hop feedback <lode-id>`
  is an alias with the same contract and input forms.

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
hop lode nudge <lode-id> "focus the failing test"
hop lode nudge <lode-id> --text "..."
hop lode nudge <lode-id> -- -leading-dash-text
hop lode answer <lode-id> 1         # answer numbered prompts
```

If `hop lode status <lode-id>` reports that an already accepted output cannot
be published, restore only those missing server-owned staged bytes with the
printed capability token:

```bash
hop lode repair-output <lode-id> - --token <token> < exact-output.md
```

This is accepted-output recovery, not normal submission or a general output
editor. The stdin bytes must match the accepted SHA-256 and byte length exactly;
otherwise the server refuses without changing canonical output.

When JavaScript exec runs a command, inspect `r.exit_code` directly. When forwarding the
shell result to the model, emit both fields with
`text(JSON.stringify({exit_code: r.exit_code, output: r.output}))`;
never emit only `r.output` when success matters.

### Stuck lodes

When `hop lode status` shows a lode in `stuck` state, inspect it through hop:

    hop lode peek <lode-id>

Common causes: permission prompt waiting for input, process hung, or waiting for
human approval.

**When the SCOPE is the problem, use kill-and-resubmit, not restart:**

    # 1. fix the scope file
    hop lode kill <lode-id>          # add --force if commits live only in its worktree
    # 2. resubmit the corrected scope

**Warning:** `hop lode restart --force` will not clean up retained work. Its
`--force` consents only to discarding an active or already-started stage. The
server still closes the owned pane and proves containment is empty before it
spawns a replacement. Restart re-runs the lode you already have, which is not
appropriate when the premise was wrong. `hop lode kill` retains **both** the
worktree and the branch, so "killed" is not "cleaned up": push the branch,
verify the SHA from a **second** clone, kill, run
`git worktree remove --force`, then confirm with `ls ~/.hopper/worktrees/`.

**Workspace trust is Hopper-managed.** Before opening Claude, Hopper records
trust for the exact workspace it will open, including each lode worktree. Treat
a workspace-trust dialog in a Hopper pane as a launch problem to inspect. Use
`hop lode peek <lode-id>` and report the lode, stage, path, and effective
`CLAUDE_CONFIG_DIR`; do not reflexively answer it or add a manual trust entry.

An unrecognised pane state does **not** mean Claude hasn't started: a live,
fully-titled Claude Code session can also carry a title Hopper does not know,
and it may be mid-turn. Never send keys to an unrecognised pane on the
assumption that nothing is running; confirm with `hop lode peek <lode-id>`
what is actually on screen first.

**Long output-silent test runs are protected by `hop check` heartbeats.** While
its child runs, `hop check` emits a socket progress heartbeat every 30 seconds,
so a healthy output-silent run is no longer killed with `No output or progress
for 351s`. Output is still buffered and only the tail is printed after the
command exits; the heartbeat supplies liveness without changing that contract.

Hopper's liveness model uses pane-diff activity, in-flight coder command
heartbeats, and descendant-process CPU activity. Pane and heartbeat silence are
the real foreground signals; descendant CPU can keep a lode `running` while
background work is active. Heartbeat or CPU activity can carry a quiet stage,
but neither bypasses the 60-minute pane-silence cap.

If a stage remains quiet or stuck, Hopper parks it at a gate and keeps the agent
alive for operator inspection. Use `hop lode peek <id>` to inspect it, then
choose an explicit pause, restart, or kill action. These actions retain the
worktree; none treats terminal silence as permission to clean it up.

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
output tail instead of remaining active at the setup status. Coder bootstrap
is bounded separately.

`hop code` prints a `CODEX TURN FAILED` banner when the backend fails a turn.
In deployments that share one Codex seat across all hosts, usage-limit failures
are fleet-wide. The in-lode agent should implement the stage directly under the
same review bar rather than retrying `hop code`.

If the action is safe (e.g. a routine permission prompt, a test confirmation),
use the recovery primitives:

    hop lode nudge <lode-id>
    hop lode answer <lode-id> 1

Both send only when Hopper sees a recognised idle Claude prompt, and report
success only after observing acceptance. If either reports an unrecognised
pane state, it reports that Hopper did not send anything. Inspect with
`hop lode peek <lode-id>` before deciding whether to retry; an unrecognised pane
may be a live session mid-turn, not a stalled one.

If the pane shows something you're not comfortable resolving (destructive action,
ambiguous approval, sensitive operation), leave it for the operator to resolve.
