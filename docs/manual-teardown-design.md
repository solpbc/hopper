# Unified manual teardown design

This is the implementation plan for the unified completion, pause, restart, kill,
and archive lifecycle. It is intentionally a clean protocol and schema break. The
only legacy artifact retained is the on-disk slot name.

## 1. Vocabulary, ownership, and file changes

Use **action** for the durable transaction and reserve **completion** for the
`hop processed` action and its output/ship-only work.

- Rename `hopper/completion.py` to `hopper/actions.py` and
  `test/test_completion.py` to `test/test_actions.py`; update imports in
  `hopper/server.py`, `hopper/cli.py`, `hopper/tmux.py`, `test/test_server.py`,
  `test/test_cli.py`, and `test/test_tmux.py`. The module currently owns both the
  generic durability primitives and completion-only output helpers
  (`hopper/completion.py:80-155`, `:840-922`, `:925-1071`), so leaving the module
  named `completion` would preserve two vocabularies in the central path.
- Rename the generic APIs and server fields: `pending_completion_path` to
  `pending_action_path`, `validate_pending_completion` to
  `validate_pending_action`, `write/load/clear_pending_completion` to their
  `pending_action` forms, `completion_status` to `action_status`,
  `_pending_completion_exists` to `_pending_action_file_exists`, `_load_pending`
  to `_load_action_slot`, `completion_threads` to `action_threads`,
  `completion_acceptances` to `action_acceptances`, and all generic scheduling,
  persistence, result, retry, continuation, projection, and startup-reconcile
  methods from `completion` to `action`. These are the primitives currently
  concentrated at `hopper/server.py:930-1051`, `:1502-1533`, `:1918-2206`, and
  `:2521-2817`.
- Keep completion-specific names for completion-only behavior:
  `stage_output`, `publish_output`, `repair_staged_output`,
  `pending_output_recovery`, ship landing/quarantine helpers, `hop processed`,
  and completion output fields. Their implementations actually manipulate
  completion bytes or ship facts (`hopper/completion.py:925-1049`,
  `hopper/server.py:2060-2068`, `:2089-2122`, `:2170-2182`).
- Keep the physical filename `pending-completion.json`, but expose it as
  `PENDING_ACTION_FILENAME = "pending-completion.json"` with a comment that the
  value is fleet-cutover ABI. Rename the path API and every operator-facing use;
  do not retain aliases for the old Python identifiers.
- Set pending-action `SCHEMA_VERSION = 2` and split the auxiliary versions into
  `RUN_OWNERSHIP_SCHEMA_VERSION = 1` and
  `SPAWN_RECEIPT_SCHEMA_VERSION = 1`. The current constant is incorrectly shared
  by the pending record, ownership record, and spawn receipt
  (`hopper/completion.py:20`, `:576-577`, `:662-663`, `:872-873`); bumping it
  globally would strand otherwise-valid ownership and spawn evidence.
- Generalize the server engine names `_run_completion_step`,
  `_handle_completion_step_result`, `_continue_completion`,
  `_block_completion`, `_clear_completed_action`, and `_resume_completion` to
  `action` names. Retain small completion-only branches under explicit
  completion helper names. This makes one engine with action-type dispatch,
  instead of four manual engines beside a completion engine.
- Add `pending_action` and `action_results` to newly created lodes and the shared
  fixture (`hopper/lodes.py:374-400`, `test/conftest.py:47-86`).
  `pending_action` is a public status projection. `action_results` is an
  oldest-to-newest list containing at most eight immutable completed receipts;
  appending a ninth evicts index zero before the lode is saved. Existing lodes
  without either field read as `None` and `[]`; this is ordinary optional lode
  data, not a v1 pending-record compatibility path.

## 2. Version 2 record and legacy-v1 disposition

### V2 record

The v2 slot has these required top-level fields:

- Identity and binding: `schema_version`, `action_id`, `lode_id`,
  `expected_generation` (32 lowercase hex or null for a never-run, proven-empty
  lode), `action_type`, `target_disposition`, and `force_consent`.
- Acceptance snapshot: `stage`, `accepted_at_ms`, and `boot_id` (nullable only
  when there was no generation or ownership to tear down).
- Progress: `phase`, `next_action`, `markers`, `recovery`, and nullable `result`.
- Evidence: nullable `output`, nullable `ownership`, `containment`, `durability`,
  nullable `spawn`, and nullable `ship`.

`force_consent` must pass `type(value) is bool`; integers are not booleans for
identity purposes. `expected_generation` replaces the generic record's
`run_generation` name, while ownership and spawn receipts retain their own
`run_generation`/`target_generation` names because those fields describe facts,
not a request precondition.

`action_type` is one of `completion`, `pause`, `restart`, `kill`, or `archive`.
Validation dispatches `next_action` as follows:

| Action | Valid target disposition | Valid next action |
|---|---|---|
| completion at mill | `advance_refine` | advance to `refine` |
| completion at refine | `advance_ship` | advance to `ship` |
| completion at ship | `shipped_archived` | ship/archive with no target stage |
| pause | `paused` | pause with no target stage |
| restart | `replacement_spawned` | restart the accepted stage |
| kill | `killed_archived` | killed archive with no target stage |
| archive | `archived` | archive with no target stage |

This replaces the stage-only derivation at `hopper/completion.py:587-594`.
`output` is required only for `completion` and must be null for every manual
action; `ship` is required only for ship completion. Output repair and orphan
collection must first require `action_type == completion` and non-null output,
fixing the unconditional reads at `hopper/completion.py:904-922`, `:977-995`,
and `:1052-1071`.

`durability` records whether the action requires the worktree guard, the
pre-accept result, and the post-empty result. Each observation records
`safe`, `unknown`, `unpushed`, `consent_override`, or `not_required`, plus the
count/basis/error and check time. Only kill with `force_consent == true` may
record `consent_override`; archive never may.

`result` is null until terminal publication. A terminal result contains the
entire bound tuple, terminal disposition, completion time, containment proof
label, retained worktree/branch/session facts, and successor lode/generation/
pane when applicable. Before the pending file is cleared, copy that result into
the bounded `lode["action_results"]` list and persist the owning active or archived
lode. This existing lode record is the durable post-clear receipt; it is what
allows a response-lost retry to return its old disposition after a replacement
generation is running. The current code deletes its only record at
`hopper/server.py:2521-2560`, so the receipt must be published first.

Keep exactly the eight most recently completed receipts, ordered oldest to
newest by publication. A retry for an evicted action cannot prove its prior
outcome: it falls through to current-generation validation and therefore
refuses `stale_expected_generation` (or `action_result_unavailable` when the
generation is still current), naming `hop lode status <id>`. It never reports
success and never re-executes an action whose receipt is unavailable.

### Markers and phases

Retain the generic teardown markers `ownership_capture`, `pane_close`,
`containment`, `scope_kill`, and `supervisor_kill`; retain completion-only
markers `output_publish`, `ship_landing`, `quarantine_rename`,
`worktree_repair`, `cleanup_authorization`, `backlog`, `worktree_remove`, and
`branch_delete`. Generalize `stage_mutation` to `lode_mutation`, generalize
`archive` and `spawn` by validating them against `action_type`, retain
`pending_clear`, and add `durability_recheck`. Unused markers remain exactly in
`not_started`; action-type validation rejects an irrelevant marker in progress.

Retain generic phases `accepted`, `capturing_ownership`, `closing_pane`,
`observing_containment`, `force_killing`, `containment_blocked`, `spawning`, and
`complete`. Rename `publishing_next_action` to `publishing_terminal`; add
`checking_durability` and `durability_blocked`. Retain the completion-only
output, ship, quarantine, and cleanup phases. The current phase-to-marker result
guard at `hopper/server.py:1976-2012` remains the pattern: action ID,
expected generation, phase, marker, and attempt ID must all match before a
worker result is applied.

### V1 behavior and recovery

Normal loading recognizes only that a JSON object declares schema 1 and raises
a distinct `LegacyPendingActionError`; it does not validate, continue, or
convert the v1 state machine. Any v1 or malformed slot remains a fence because
`_pending_action_file_exists` is the fail-closed predicate. Startup projects a
clear operator status: schema-v1 pending action must be drained before this host
is upgraded; malformed pending action must be repaired or drained before
upgrade. It does not run OOM, disconnect, liveness, or spawn reconciliation for
that source generation. There is no migration, quarantine, reconstruction, or
recovery verb in product code. The fleet ship summary instructs the calling
session to drain every pending action before upgrading a host.

## 3. Single accept-mutation boundary

All five action requests use one wire type, `lode_action`, and one event-loop
transaction opener. Completion may prepare/stage output and ship facts off-loop,
and manual kill/archive may probe durability off-loop, but only this opener may
publish accepted intent.

The signature is:
`Server._open_lode_action(*, action_type: str, message: dict, prepared: dict) -> dict`.
`action_type` is supplied by the server's request dispatcher and must equal the
message discriminator; `prepared` contains only server-produced output,
ownership, ship, and durability facts. The return object always has `outcome`
(`accepted`, `idempotent`, or `refused`), `reason`, and `action_id`; it has
`record` for accepted/in-progress actions, `disposition` for a completed retry,
and `detail` plus `recovery_command` for refusals or blocked actions. Callers do
not mutate a lode based on an exception or a partial result.

The opener performs these steps in this exact order:

1. Validate the message envelope and scalar types without looking up or
   mutating a lode: canonical lode ID, 32-hex action ID, explicit nullable
   expected generation, supported action type, supported target disposition,
   and real boolean force consent. Reject partial hidden-protocol pairs and
   invalid action/target/force combinations.
2. Find the active or archived lode. Search its bounded immutable
   `action_results` list for
   the action ID before comparing against current generation. If the receipt's
   validated binding matches, return its disposition; if the same ID has any
   changed bound field, refuse `action_identity_mismatch`. This ordering is what
   lets an old retry address a result after restart has changed
   `lode["run_generation"]`.
3. Inspect the canonical slot. A legacy/corrupt slot refuses fail-closed. For a
   valid v2 pending action, compare identity and binding before any new-action
   safety check: an exact retry returns/restarts that record; the same ID with a
   changed binding refuses; every different ID refuses `action_conflict` and
   returns the owner identity/type/generation/retry command. No conflict path
   changes `next_action`, phase, markers, containment, or lode state.
4. Only for a genuinely new action, require
   `message.expected_generation == lode.run_generation`, including explicit
   null equality for a never-run lode. Validate stage and action-specific payload
   snapshots. Completion additionally validates its stage/output fingerprint;
   a duplicate completion retry does not need to resend output because step 3
   resolves its already accepted identity first.
5. Resolve outside-supervisor ordering before action safety: reject a recorded
   terminal `failure_kind`, a previously serialized OOM/unverified run result,
   or a pending guarded disconnect/result. Because the event loop serializes
   mutations (`hopper/server.py:4496-4514`), a result handled first wins here.
6. Validate ownership and containment safety without side effects. Active or
   parked actions require the durable ownership record for exactly the expected
   generation and the required proof interface. A null-generation or inactive
   archive shortcut is allowed only when active, pane, PID, OOM scope, and live
   owned-process evidence are all absent; it constructs already-done
   ownership/pane/containment markers rather than skipping proof implicitly.
7. Apply raw verb rules: restart validates stage and enforces both existing
   active-runner and started-stage force requirements; non-force kill and
   active/parked archive require a proven-safe durability preflight; pause and
   archive require false force; kill/restart record force exactly. This is the
   first point where expensive server-prepared safety facts are consumed, but
   still no mutation has occurred.
8. Recheck lode presence, stage, generation, terminal failure, action-result
   race, and canonical-slot absence immediately before persistence. This is the
   second in-loop check after off-loop preparation, equivalent to the current
   completion check at `hopper/server.py:1428-1461`.
9. Build and fully validate the v2 record, then fsync it through the atomic
   replace primitive (`hopper/completion.py:119-155`). That replace is the sole
   acceptance linearization point. A persistence failure returns refusal with
   the lode, pane, process, containment, state, and action projection unchanged.
10. Only after fsync, cancel the old generation's disconnect guard, install the
    `pending_action`/teardown projection, register any response waiter, send the
    accepted acknowledgement used by `hop processed`, and schedule the first
    side-effecting marker. For manual commands, acceptance is not terminal
    success: retain the connection and answer with completed or blocked
    disposition. A disconnected waiter has no effect on the durable action.

Manual durability probes may race an external commit after preflight; that is
why kill and active/parked archive have the mandatory post-empty check. No race
can make them publish a safe archive without that second proof.

## 4. Conflict and idempotence contract

Binding equality uses a validated Python tuple in this exact order:
`(lode_id, expected_generation, action_type, target_disposition, force_consent)`.
A tuple is simpler and stricter than serializing a second dictionary, and parsed
JSON object ordering is irrelevant by construction. `_canonical_json_bytes`
remains the durable-file serializer (`hopper/completion.py:146-155`), not the
identity comparator. Exact boolean validation prevents `true` from comparing as
integer `1`. Completion also compares its immutable stage/output
digest/length as payload integrity after binding equality; those are not added
to the fixed bound tuple.

For a pending owner, every action-type pair has the same deterministic rule:

| Incoming \ owner | completion | pause | restart | kill | archive |
|---|---|---|---|---|---|
| completion | Same ID/binding is idempotent; otherwise conflict | Same ID/binding is idempotent; otherwise conflict | Same ID/binding is idempotent; otherwise conflict | Same ID/binding is idempotent; otherwise conflict | Same ID/binding is idempotent; otherwise conflict |
| pause | Same ID/binding is idempotent; otherwise conflict | Same ID/binding is idempotent; otherwise conflict | Same ID/binding is idempotent; otherwise conflict | Same ID/binding is idempotent; otherwise conflict | Same ID/binding is idempotent; otherwise conflict |
| restart | Same ID/binding is idempotent; otherwise conflict | Same ID/binding is idempotent; otherwise conflict | Same ID/binding is idempotent; otherwise conflict | Same ID/binding is idempotent; otherwise conflict | Same ID/binding is idempotent; otherwise conflict |
| kill | Same ID/binding is idempotent; otherwise conflict | Same ID/binding is idempotent; otherwise conflict | Same ID/binding is idempotent; otherwise conflict | Same ID/binding is idempotent; otherwise conflict | Same ID/binding is idempotent; otherwise conflict |
| archive | Same ID/binding is idempotent; otherwise conflict | Same ID/binding is idempotent; otherwise conflict | Same ID/binding is idempotent; otherwise conflict | Same ID/binding is idempotent; otherwise conflict | Same ID/binding is idempotent; otherwise conflict |

The “same ID/binding” cells can occur only when the incoming action type equals
the owner type; because action type is part of the binding, a cross-type reuse
is `action_identity_mismatch`, not recovery. `hop lode restart` recovers a
blocked completion by submitting the pending completion identity and binding,
not by attempting a new restart identity.

| Slot/result state | Incoming request | Outcome | Permitted mutation |
|---|---|---|---|
| no pending action, current generation | new ID and valid binding | first request is accepted and owns the generation | write intent, then drive its state machine |
| pending, before terminal publication | same ID and canonical-equivalent binding | idempotent in-progress/blocked result; blocked phase is retried | only the recorded phase/marker may advance |
| pending, after terminal publication but before clear | same ID and binding | recorded disposition | pending clear may finish; target generation is untouched |
| pending | same ID, any one bound field changed | `action_identity_mismatch` | none |
| pending | different ID, same or different fields | `action_conflict` naming owner | none, including no `next_action` rewrite |
| cleared, receipt exists | same ID and binding | recorded disposition | none |
| cleared, receipt exists | same ID with changed field | `action_identity_mismatch` | none |
| replacement generation running | delayed old ID and binding | old recorded disposition found before current-generation check | none on replacement |
| no pending action, prior receipt exists, current generation | fresh ID explicitly bound to current generation | new action accepted | normal new transaction |
| no pending action | fresh/different ID bound to old generation | `stale_expected_generation` | none |
| any | same semantic fields after real JSON serialization with different key order | same tuple, therefore idempotent | as above |
| v1 or malformed slot | any action or spawn | fail-closed legacy/invalid refusal naming drain-before-upgrade | none |
| no matching retained receipt | retry ID bound to an old generation | `stale_expected_generation` with `hop lode status <id>` | none; never re-execute |

## 5. Shared teardown and per-verb state machines

All active-generation paths share exact ownership capture, PTY close,
containment observation, optional scoped/supervisor kill, and empty proof. They
reuse the worker/result attempt guard at `hopper/server.py:1962-2057`; no verb
may directly null pane/PID/OOM identity as the old pause and kill paths do at
`hopper/server.py:3883-3910` and `:3978-4001`.

| Action | Ordered phases | Ordered markers | Terminal publication |
|---|---|---|---|
| completion | accepted; publishing output; capture ownership; close pane; observe containment; force kill if needed; publish terminal; completion-specific ship/quarantine/cleanup; spawn when advancing; complete | output publish; ownership capture; pane close; containment; optional scope/supervisor kill; ship/cleanup markers as applicable; lode mutation; archive/backlog as applicable; spawn as applicable; pending clear | existing stage advance or shipped archive, then optional successor adoption |
| pause | accepted; capture ownership; close pane; observe containment; force kill if needed; publish terminal; complete | ownership capture; pane close; containment; optional scope/supervisor kill; lode mutation; pending clear | state `paused`, inactive, pane/PID/OOM handle cleared only after proof; stage, worktree, branch, and Claude stage session retained |
| restart | accepted; capture ownership; close pane; observe containment; force kill if needed; publish terminal; spawn; complete | ownership capture; pane close; containment; optional scope/supervisor kill; lode mutation; spawn; pending clear | after empty, reset only the accepted stage session/progress, then create and adopt exactly one replacement generation |
| kill | accepted; capture ownership; close pane; force kill/observe containment; check durability; publish terminal; complete | ownership capture; pane close; scope/supervisor kill as observed; containment; durability recheck; archive; pending clear | archive once with killed status and `killed_archived` result; worktree and branch retained |
| active/parked archive | accepted; capture ownership; close pane; observe containment; force kill if needed; check durability; publish terminal; complete | ownership capture; pane close; containment; optional scope/supervisor kill; durability recheck; archive; pending clear | archive once with `archived` result; worktree and branch retained |
| inactive, already-empty archive | accepted; publish terminal; complete | ownership/pane/containment markers constructed done with explicit no-owner proof; archive; pending clear | immediate archive after durable acceptance, with no PTY/process side effect |

Kill starts containment in `kill_pending` and may skip the graceful deadline;
`force_consent` does not control that scheduling. Restart `--force` only consents
to discarding an active/started stage. It does not authorize worktree cleanup,
bypass ownership capture, shorten empty proof, or admit a spawn before proof.
Kill `--force` only overrides the unpushed/unknown durability refusal; cleanup
remains out of scope and worktree/branch remain retained.

The restart raw boundary preserves both current refusal rules. Without force,
an active registered runner refuses with the current “has a registered runner”
semantics, and a started non-error stage refuses with the current
`claude[stage].started=True` semantics (`hopper/cli.py:3225-3242`). Move those
decisions to the opener. The server returns structured reasons plus the
authoritative message; CLI help and presentation retain the existing meaning
at `hopper/cli.py:2692-2699`. The CLI may print the pre-action “terminating”
notice and final success prose, but it no longer decides safety.

Move the kill durability guard from CLI-only `_unpushed_kill_refusal`
(`hopper/cli.py:1851-1887`, `:3433-3438`) to a server helper backed by
`git.unpushed_commits`. Before intent, unknown or positive counts refuse without
PTY close unless force is true. After containment is proven empty and
immediately before archive publication, run `durability_recheck` again for every
non-force kill and active/parked archive:

- zero marks the marker done and permits archive publication;
- unknown or positive marks it blocked, leaves phase `durability_blocked`,
  preserves the proven-empty containment and all artifacts, and exposes the
  exact same-action retry;
- force kill records `consent_override` and proceeds; archive has no override;
- changing force on the accepted ID is an identity mismatch, and a new forced
  kill cannot displace the pending non-force action. The operator must push/fix
  durability and retry that same action.

Manual archive preflight refusal tells the operator to inspect/push and press
Delete again. It may additionally point to `hop lode kill <id> --force` while
stating that kill is a different destructive disposition. Post-empty archive
blockage only offers push/inspect plus the same archive retry.

## 6. Spawn fencing and successor admission

Split the two meanings currently combined in `_generation_is_fenced`
(`hopper/server.py:1042-1051`):

- `_lode_has_pending_action(lode_id)` loads the slot and returns true for every
  nonterminal v2 record and every invalid/v1 file, regardless of the lode's
  current generation. `_gated_spawn` uses this and therefore cannot escape when
  restart has already installed a successor generation.
- `_generation_has_teardown_intent(lode_id, generation)` is true only for the
  pending record's expected/source generation. Runner exits and OOM results use
  this to classify teardown-caused loss as intentional. A successor-generation
  failure is not swallowed as expected teardown.

Delete `PENDING_ACTION_FENCED_MUTATIONS` (`hopper/server.py:93-95`). New manual
actions conflict in `_open_lode_action`, all ordinary spawn-capable operations
fence inside `_gated_spawn`, and old-generation runner mutations use the
generation-intent predicate. This closes the omissions for `lode_resume_refine`,
unarchive, and stage reset documented by their current direct gate calls at
`hopper/server.py:4021`, `:4212`, and `:4241`.

Keep exactly two textual `spawn_claude(` call sites:

1. `_gated_spawn` remains the only ordinary spawn boundary for create, backlog
   promotion, resume, generic spawn/auto-advance, resume-refine, and unarchive.
2. Rename/generalize `_spawn_completion_pane` to `_spawn_action_successor`; it
   serves only a pending completion or restart record.

The successor path requires a valid pending record, matching action/attempt,
`containment == proven`, completed terminal/lode mutation, a durable spawn
marker in `intent`, and a recorded target generation. It reuses the existing
action-scoped receipt and supervisor/worker adoption proof
(`hopper/server.py:2413-2519`). An existing matching receipt is adopted; a
conflicting receipt blocks. Only registration messages for that exact recorded
successor generation bypass the old-generation runner fence. The action is not
successful until both supervisor and worker ownership are adopted, so replay
cannot spawn twice. Update the source-count invariant at
`test/test_server.py:3150-3154` to pin `_gated_spawn` and
`_spawn_action_successor` as the two callers.

Unarchive-with-spawn must perform the read-only pending-action gate before
moving the archived object into the active list. The current code mutates first
and gates second (`hopper/server.py:4011-4025`); reverse that order so refusal is
side-effect free. Compound resume-refine and stage reset likewise keep their
state/session mutations in the admitted spawn's pre-spawn callback, never
before the gate.

## 7. Refusal projection and TUI channel

Move the refusal constants to `hopper/lodes.py` and define one authoritative
`REFUSAL_STATUS_PREFIXES` tuple containing `spawn refused: `, `spawn failed: `,
and `action refused: `. Import it in both server and TUI. This removes the
server-only tuple at `hopper/server.py:509` and the literal duplicate at
`hopper/tui.py:122`.

Use `_set_action_refusal` only for a syntactically valid, identified manual
request that failed pre-accept safety/durability. It updates status, saves the
correct active or archived list, and broadcasts. The TUI already polls the
server's shared list references (`hopper/tui.py:1474-1485`) and renders status
text (`hopper/tui.py:116-140`), so archive refusal becomes visible without a
socket or new notice queue.

Refusal precedence is strict:

- A valid accepted action always owns `state=teardown`, status, and
  `pending_action`. Conflicts return a response but never call a refusal-status
  setter.
- A refused action has no accepted record because all safety checks precede the
  fsynced linearization point. Therefore setting `action refused: ...` cannot
  clobber an in-flight teardown projection. The setter also checks both the
  pending file and `pending_action` before writing, making the invariant
  executable rather than conventional.
- Invalid raw messages and stale generations receive structured responses but
  do not overwrite useful status. V1/invalid-slot startup projection is itself
  the fence owner and is never overwritten by a later refusal.

Clear spawn refusal/failure only when pane liveness or a successful admitted
spawn supersedes it, preserving the current intent of `_clear_spawn_refusal`
(`hopper/server.py:631-637`, `:2884-2915`, `:3021-3025`). Clear a manual refusal
only when the same or a later valid action is durably accepted and its teardown
projection replaces the status, or when an explicit successful operator action
publishes a newer terminal status. Startup liveness and progress summaries do
not clear manual refusals. TUI progress selection uses the shared prefix tuple
so neither refusal class is hidden by stale `last_progress_summary`.

## 8. Startup and outside-supervisor ordering

Startup remains ordered around the existing calls at
`hopper/server.py:3104-3106`, but the predicates become action-aware:

1. `_reconcile_action_records` scans active and archived lodes first. It loads
   every slot, installs pending projections, records v1/invalid fail-closed
   fences, cancels disconnect handling only for accepted source generations,
   reconciles terminal receipts/pending clear, and queues valid unfinished
   records. It does not start worker threads before the event loop exists.
2. `_consume_failed_oom_units` runs next. It skips only generations for which a
   durable source-generation teardown intent already exists. Thus intent-first
   exit/OOM is intentional. It still consumes a pending action's recorded
   successor generation; successor OOM/unverified results block replacement
   spawn instead of being mislabeled expected teardown. With no action record,
   OOM/unverified is recorded first and a later opener refuses.
3. `_reconcile_startup_lodes` runs after OOM. It skips every lode with a pending
   action or invalid/v1 slot because the action reconciler owns pane/process
   interpretation. It may scrub a stale `pending_action` projection when there
   is no slot and a matching durable result receipt proves clear completed.
4. After the writer and event-loop threads start, enqueue one
   `_action_reconcile` event per valid unfinished action, replacing
   `_startup_completion_actions` and `_completion_reconcile`
   (`hopper/server.py:3151-3152`, `:3738`). Reconciliation selects the first
   unfinished legal marker, adopts receipts before spawning, and never retries
   a blocked safety/durability phase under a different identity.

This ordering prevents ordinary disconnect cleanup from clearing identity or
spawning between record load and action recovery.

## 9. CLI, client, remote protocol, and cutover behavior

### New protocol

Replace the mutating wire shapes `lode_complete`, `lode_pause`,
`lode_reset_claude_stage` with spawn, `lode_kill`, and `lode_archive` with the
single `lode_action` message. It carries the five binding fields plus the
action-specific completion payload. Replace their disparate responses with
`lode_action_ack`, containing action ID/type, accepted flag, outcome, reason,
terminal disposition when known, containment/preservation facts when blocked,
and exact recovery command. Remove the fire-and-forget kill client
(`hopper/client.py:598-604`); every CLI manual action waits for a terminal or
blocked disposition.

Add hidden `--action-id` and `--expected-generation` arguments with
`help=argparse.SUPPRESS` to pause, restart, and kill. Treat them as an all-or-none
pair. The internal literal `none` represents an explicit null generation if a
protocol-forwarded never-run action ever needs it; omission still means “fresh
local invocation,” not null.

For a fresh local invocation, resolve once, then either reuse the same action
type's `pending_action` identity/binding or generate one UUID and capture the
snapshot's generation. Never regenerate within the invocation. If restart sees
a pending completion, it submits that completion identity/binding for recovery,
preserving the current `hop lode restart` recovery contract at
`hopper/cli.py:3206-3223`. A pending different verb is allowed to reach the
server only to receive the deterministic owner conflict; it is never silently
retargeted.

On unknown disposition, return nonzero and print the action ID, expected
generation, `hop lode status <id>`, and the exact same-verb retry. The normal
retry command reuses `pending_action`; hidden flags also make the identity
explicit for transport/debug recovery. Success prose is printed only for a
terminal disposition, never mere acceptance.

Remote forwarding appends both hidden arguments before calling
`_run_remote_cli`. The remote parser must preserve provided values even if its
fresh `_resolve_lode` snapshot has moved. If `HOP_NO_ROUTE=1` is present for a
manual verb and the pair is absent, refuse protocol upgrade rather than minting
identity remotely. No `remote.py` transport changes are needed: argv already
crosses `run_remote` and is shell-quoted by `_remote_command`
(`hopper/remote.py:71-99`, `:122-152`), while `_run_remote_cli` mirrors the
remote exit/output (`hopper/cli.py:517-547`).

### Fleet cutover summary

- Drain every pending action on a host before upgrading it; a missed v1 or
  malformed slot remains visibly fail-closed and has no product migration verb.
- Changed wire messages: the five old mutation types above become
  `lode_action`; manual responses become `lode_action_ack`; completion output
  repair keeps its message but renames generic identity field
  `run_generation` to `expected_generation`; successor registration/receipt and
  ordinary runner messages retain their generation fields.
- The new server keeps refusal-only branches for old action message types. They
  never mutate and return `protocol_upgrade_required` in the response shape the
  old waiting caller can understand where possible. Old completion, pause, and
  restart clients therefore report refusal. Old kill remains fire-and-forget
  and can print its old success line despite the new server doing nothing; the
  refusal remains mutation-free, so the old client's false presentation has no
  synchronous correction. This is why rollout remains a coordinated protocol
  cutover.
- A new CLI talking directly to an old server sends unknown `lode_action`, times
  out/returns UNKNOWN nonzero, and performs no mutation. A new CLI routed to an
  old remote CLI is rejected by that CLI's unknown hidden options before it
  reaches the old server. An old routing CLI reaching a new remote CLI lacks the
  required hidden pair under `HOP_NO_ROUTE=1` and is refused before remote
  re-resolution.
- Old-generation action retries missing identity or expected generation,
  mismatched force consent, stale-generation retries, and raw safety bypasses
  are all refused by the new opener before containment or state changes.

The ship summary must explicitly call out the old kill false-success caveat and
require the §8 fleet inventory/cutover before mixed-version control use.

## 10. Runner-owned completion removal

In `hopper/runner.py`, delete `DISMISS_STABILIZATION_TIMEOUT_SEC`,
`DISMISS_DEADLINE_MS`, and `DISMISS_DEADLINE_MIN`; retain
`PANE_CAPTURE_FAILURE_LIMIT` because `_capture_activity_pane` uses it at
`hopper/runner.py:866-878`. Remove `_done_label`, `_done_status`, `_next_stage`,
all `_done`/deadline/dismiss fields, the dismissal thread in `_run_claude`, the
`completed` branch in `_on_server_message`, `_wait_and_dismiss_claude`, the
completion branch at the start of `_check_activity`, `_emit_stage`, and the
runner-owned ready/stage transition after process exit
(`hopper/runner.py:220-223`, `:262-270`, `:376-380`, `:468-475`, `:535-543`,
`:579-591`, `:601-691`, `:738-757`). Remove the now-unused subclass metadata in
`hopper/process.py` and update its tests.

Keep gate, question/selector, permission, review-gate, ordinary idle/stuck,
activity capture, and `_park_idle` behavior. `_park_idle` remains live from the
ordinary activity paths at `hopper/runner.py:828-851`; only its completion
wording at `:923` changes. No path writes or reacts to state `completed` after
this removal. Also close the raw dynamic write hole by validating
`lode_set_state` against supported states at the server boundary instead of
persisting arbitrary client strings through `update_lode_state`
(`hopper/client.py:792-814`, `hopper/server.py:4039-4059`,
`hopper/lodes.py:515-537`).

Update runtime help/refusal/recovery text in `hopper/cli.py`, status projection
in `hopper/actions.py`, comments/icons in `hopper/lodes.py` and `hopper/wait.py`,
the stage prompts (`prompts/mill.md:133-134`, `prompts/refine.md:134-135`,
`prompts/ship.md:190-191`), `README.md`, and `skills/hop/SKILL.md:299-317` and
`:388-395`. They must say that `hop processed` durably accepts output and the
server closes/proves containment before advancing; no text may claim Hopper
waits between turns, sends terminal keys, waits for Claude exit, needs a second
completion, or parks on a completion deadline. Apply fresh `owner-copy` output
to prompts/operator wording and fresh `public-repo` output to README/public help
before finalizing those surfaces.

## 11. Deterministic fault-injection plan

Use a parameterized action/restart harness in `test/test_server.py`, real temp
files, fake ownership/containment/spawn facts, and a fresh `Server` object for
reconciliation. Do not add sleeps or a production fault API.

| Crash point | Deterministic injection mechanism | Required recovery assertion |
|---|---|---|
| after intent persistence | wrap `actions.write_pending_action`; perform the real write, then raise a test `InjectedCrash` when phase is `accepted` | startup sees the fence and begins only the recorded first marker |
| after PTY close | feed a synchronous fake close worker result, then make the next `_persist_action`/schedule boundary raise after the pane-close marker is fsynced | close is not repeated destructively; containment resumes from recorded ownership |
| after grace/kill | fake `teardown.observe_containment` to return `kill_pending`/post-kill facts and raise after scope/supervisor-kill intent/result persistence | retry verifies the same owned scope/processes and never targets siblings |
| after empty proof | return deterministic `proven` containment and raise after the containment-done record write | startup proceeds only to durability/terminal work; it does not close or kill again |
| after terminal state/archive | wrap `save_lodes` or `save_archived_lodes` to perform the real atomic save and then raise before the terminal marker/result write | reconciliation recognizes action ID/archive receipt and publishes one matching result, with one archive copy |
| after replacement spawn | fake `_spawn_action_successor` so it writes the real spawn receipt and successor generation/pane, then raise before adoption completion | startup adopts the receipt and never calls the fake spawn a second time |
| after response | fake `_send_response` to capture the terminal acknowledgement and then raise before pending-clear intent | retry returns the persisted result; startup only clears pending state |
| after pending-clear | wrap `actions.clear_pending_action` to unlink/fsync for real and then raise before stale projection cleanup | startup uses `action_results`, removes stale `pending_action`, and returns the same result without side effects |

Use `threading.Event` barriers only where the acceptance/worker thread must be
held while the test serializes an OOM/result event first or action intent first.
No wall-clock polling is needed.

## 12. Acceptance-criteria test map

| AC | Tests and files |
|---|---|
| AC1 | `test/test_server.py`: call `srv._handle_mutation({...}, conn)` with hand-built pause/restart/kill/archive messages missing identity, stale generation, invalid force, and bypassed client guards. Take `copy.deepcopy` of active/archived lodes before each call; assert exact before/after equality, no pending file, and no close/containment fake calls. Add completion/`hop processed` missing-identity twins. |
| AC2 | `test/test_server.py`: response-lost terminal retries for all four manual actions after restart replacement is live; fresh-current-generation acceptance only after no pending action; same-verb recovery reuses identity. The canonical-order proof must create two differently ordered JSON byte strings, `json.loads` each across a real serialization round trip, accept one and retry the other, and assert one action/result. Changed lode ID, generation, action type, disposition, and each real boolean force value are separate mismatch cases. `test/test_cli.py` and remote tests cover local/remote identity retention and UNKNOWN nonzero output. |
| AC3 | `test/test_server.py`: parameterize the full 5-by-5 conflict matrix and assert first record/`next_action` bytes remain unchanged; duplicate accepted retry; no acceleration; one successor. Exercise create, backlog promotion, resume, generic spawn, resume-refine, stage restart, and unarchive through the shared gate. Preserve the exact-two-`spawn_claude` source invariant. |
| AC4 | `test/test_server.py`: pause/restart exact close and empty-before-terminal/spawn; ownership/inspection/kill failures remain pending with facts/artifacts; restart force and non-force raw rules; force still waits for empty; restart command recovers a failed completion identity. `test/test_cli.py` covers presentation and same-action retry. |
| AC5 | `test/test_server.py`: raw non-force kill unknown/unpushed refusal before close, force acceptance with retention, empty-before-archive, injected new commit at post-empty check blocks, and proven-zero twin completes. Assert no worktree/branch cleanup calls. Use the AC1 raw/deepcopy pattern for bypass. |
| AC6 | `test/test_server.py`: active/parked archive unknown/unpushed refusal, no force field accepted, safe containment/archive retention, inactive proven-empty immediate action, and post-empty new-commit blockage. The guarded-client fixture and a separately hand-built unguarded raw request both call `_handle_mutation` and use `copy.deepcopy` before/after; these are the explicit raw-boundary bypass proofs. `test/test_tui.py` asserts refusal status becomes visible. |
| AC7 | `test/test_server.py`: parameterize all actions over sibling pane, unrelated process group, cgroup, server process, and unrelated systemd facts using existing teardown fakes; assert identity remains until proof. Barrier-test OOM/unverified before intent versus after intent for each action. Add successor-generation failure as non-intentional. |
| AC8 | `test/test_runner.py`: retain/add deterministic silent-build, permission, review-gate, unanswered-selector, idle-park, and capture-outage tests and assert no close/kill/send-key effect. `test/test_server.py` adds mill/refine/ship completion teardown twins and pause-then-resume with retained worktree/session and exactly one new generation. `test/test_process.py` stops expecting runner-emitted ready/stage updates. |
| AC9 | `test/test_server.py`: parameterize pause/restart/kill/archive over every injection point in §11, instantiate a fresh server, reconcile, and assert one result, zero duplicate spawn/archive/destructive calls, no ordinary-disconnect classification, and retained artifacts. |
| AC10 | `test/test_runner.py`: source/behavior assertions that completion broadcast does not create a dismissal thread, send keys, park, or emit a stage; ordinary observation tests remain. `test/test_server.py` proves accepted completion advances without runner state. A repository source scan rejects the removed symbols and completion-key prose. |
| AC11 | `test/test_cli.py` covers runtime help and recovery rendering; add a focused docs/source stale-phrase test for README, prompts, and shipped skill. Record the fresh owner-copy/public-repo review evidence in the implementation report. |
| AC12 | All new tests use fake clocks, events, ownership, tmux, process, systemd, and git probes. After focused files pass, run the full `make ci` gate through `hop check --allow-capture`; this cross-cutting protocol/shared-fixture change justifies the full refine gate. Baseline is 2131 passed, 0 failed, 0 skipped. |
| AC13 | `test/test_actions.py` validates blocked status projections include identity, target disposition, containment truth, retention, and exact retry. `test/test_server.py` restarts a server and finalizes the same blocked identity. `test/test_cli.py` covers pause/restart/kill refusals; `test/test_tui.py` covers archive refusal and import of the one shared prefix tuple. |

Delete these completion-dismiss tests from `test/test_runner.py` because their
product behavior is removed:

- `test_wait_and_dismiss_sends_ctrl_c`
- `test_resumed_stage_sends_no_keys_until_completed_broadcast`
- `test_wait_and_dismiss_no_longer_exits_on_gate`
- `test_wait_and_dismiss_retries_when_process_survives`
- `test_dismiss_never_sends_ctrl_d`
- `test_wait_and_dismiss_acts_after_capture_failure_limit`
- `test_wait_and_dismiss_acts_when_stabilization_bound_expires`
- `test_wait_and_dismiss_aborts_when_monitor_stops`
- `test_wait_and_dismiss_aborts_without_pane`
- `test_wait_and_dismiss_pauses_while_parked_then_resumes`
- `test_wait_and_dismiss_restarts_stabilization_after_rearm`
- `test_parked_completion_ignores_pane_changes_without_rearming`
- `test_completion_deadline_latch_preserves_done_and_allows_advance`
- `test_completion_deadline_rearms_and_allows_advance`
- `test_healthy_completion_advances_without_park_stuck_or_gated`

Replace them with runner tests that completion/teardown updates never send keys,
park, or emit ready/stage; a normal worker exit never owns stage advancement;
and the existing pre-completion gate/idle/activity cases remain intact.
`PANE_CAPTURE_FAILURE_LIMIT` tests for `_capture_activity_pane` remain.

## 13. Implementation sequence

1. **Schema and naming foundation:** move `completion.py` to `actions.py`, split
   schema constants, implement v2 validation/construction/binding/result
   receipts, make output optional by action type, add lode projection/result
   fields, and update unit fixtures/tests. Implement v1/malformed fail-closed
   loading before changing server callers; no product migration path is added.
2. **Acceptance and generic engine:** add `lode_action`, the ordered opener,
   action response waiters, generic persistence/projection/status/retry, shared
   phases/markers, and completion dispatch. Route `hop processed` through it and
   keep the existing completion/output/ship tests green under v2 semantics.
3. **Manual actions:** replace direct pause/restart/kill/archive branches with
   per-verb plans over the shared engine. Move restart and durability safety to
   the server, add both durability checks, terminal receipts, and exact recovery
   status. Remove the old direct process/pane/state mutation code.
4. **Spawn and startup:** split lode-wide spawn fence from source-generation exit
   intent, generalize the successor spawn/receipt path, delete the mutation set,
   make unarchive preflight non-mutating, then implement action-first startup and
   successor OOM handling.
5. **Client surfaces:** add generic client submission, terminal/blocked response
   handling, hidden CLI protocol args, identity reuse, remote forwarding, UNKNOWN
   recovery, TUI archive identity, and the shared refusal prefix/clear rules.
   Add refusal-only old-wire handlers.
6. **Runner removal and prose:** remove the completion terminal-dismiss path and
   dead subclass/state-write behavior while retaining ordinary observation.
   Update CLI help/status, prompts, README, skill, comments, and wait/TUI prose
   using the required voice-check outputs.
7. **Tests in dependency order:** action schema/binding; raw acceptance and
   conflict matrix; state machines/durability; spawn/startup/OOM; CLI/remote/TUI;
   runner deletion/replacements; fault matrix; stale-prose checks. Update the
   shared `make_lode` fixture early so all consumers see the new fields.
8. **Validation during implementation only:** focused action/server tests first,
   then client/CLI/TUI/runner/process/wait tests, then one full `make ci` because
   the wire contract and shared fixture are cross-cutting. No validation belongs
   to this design stage.

## 14. Risks and fixed resolutions

- **Cross-file terminal persistence:** publish `action_results` and terminal
  lode/archive state before response and pending clear; startup reconciles every
  intermediate ordering. Never rely on the response as durability.
- **External Git mutation between checks:** accept that preflight is a snapshot,
  but require the second post-empty check immediately before archive. A late
  commit can block publication, never create false safe success.
- **Successor OOM hidden by a lode-wide fence:** use lode-wide fencing only for
  spawn admission and exact source-generation intent for exit/OOM
  classification.
- **Refusal hiding action progress:** refusal setters cannot run when a slot or
  pending projection owns the lode; manual refusal has its own clear rule and
  the TUI imports the shared prefixes.
- **Mixed-version fleet:** new wire types make new-to-old mutation fail safe;
  explicit legacy refusals make old-to-new fail safe at the server, except the
  old kill CLI can still print false fire-and-forget success. The fleet cutover
  must eliminate mixed control before manual use.
- **V1 fence permanence:** normal code never reads v1 semantically and never
  clears it. Fleet rollout must drain pending actions before upgrading each host;
  any missed v1 or malformed slot remains visibly fail-closed for the calling
  session to resolve operationally.
- **Receipt eviction:** retain the eight newest terminal receipts in documented
  oldest-to-newest order. A retry older than that cannot prove disposition and
  safely refuses with current status/recovery; it never replays destructive work.
- **No unresolved product-design questions remain.** The required owner-copy and
  public-repo voice-check outputs and the fleet cutover are execution evidence,
  not alternate designs.
