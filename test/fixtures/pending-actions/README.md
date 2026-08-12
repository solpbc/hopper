# Wedged pending-action fixtures — captured 2026-08-11/12

*Vendored into the hopper test tree from the extro repo (`cto/tools/hopper-containment-eval/fixtures/`),
which holds the operator-side copy and the analysis that produced them. This copy is the one CI loads.*

⏱ **Captured because they are perishable.** Verbatim copies of live `pending-completion.json`
records from lodes wedged in `Teardown blocked` on the night of 2026-08-11. The recovery handed to
VPE (`req_d72rqbpw`) destroys them — `hop lode restart` clears the pending action.

## What these are actually for

⚠ **The value is the schema boundary, not discrimination between wedge classes.**
`test/test_containment_records.py` copies these production bytes into the pending-action path,
loads them through `actions.load_pending_action`, and drives their recorded containment states.
That boundary catches changes that would brick records already on disk, including the draft fix
that removed two keys and changed one pinned value.

✅ All five validate under `actions.validate_pending_action` unmodified at hopper `5d8049f`, and
both mutation controls genuinely refuse — `poll_interval_ms=250` → *"containment.poll_interval_ms
must be 50"*, dropping `started_monotonic_ns` → *"containment has missing keys"*. That is what makes
them a migration oracle: **if a change stops them validating, it bricks production.**

⛔ **Load them through `actions.load_pending_action` / `validate_pending_action`.** A test that
builds the dict in-process bypasses the validator entirely and goes green while production bricks.

## What the bytes actually discriminate

⚠ **Corrected 2026-08-12.** An earlier version of this table labelled fixtures by the *live host
state at capture* — "the scope drained", "absent cgroup" — which the records do not carry. Measured
across all five, there are **four** distinct record shapes, not five (three among the completion records, plus the restart record):

| fixture | action | containment | `last_cgroup_observation` | `scope_kill` | `last_error` |
|---|---|---|---|---|---|
| `wedged-populated-scope-5tuofgwh.json` | completion | `blocked` | `populated` | `not_started` | deadline expired |
| `wedged-drained-scope-jklqlbm7.json` | completion | `blocked` | `populated` | `not_started` | deadline expired |
| `wedged-killfired-verify-expired-jjihelyd.json` | completion | `blocked` | `populated` | **`blocked`** | deadline expired |
| `wedged-killfailed-absent-cgroup-x627n2uj.json` | completion | `blocked` | `populated` | **`blocked`** | **exact cgroup kill failed** |
| `wedged-restart-ownership-blghq7to.json` | restart | **`not_started`** | `None` | `not_started` | `None` |

📌 **The first two are byte-identical on the containment surface.** Their filenames record what the
host looked like when they were taken — `jklqlbm7`'s scope had drained, `5tuofgwh`'s had not — and
that fact lives on the host, not in the JSON. A test that wants those two to diverge **must supply
the observation stream**; the record cannot supply it.

What each shape is worth:

- **`scope_kill: not_started`** — the escalation never ran. The force-kill was missed at the
  boundary, so the scope is usually still populated.
- **`scope_kill: blocked` + deadline expired** — the kill **fired and worked**; only post-kill
  verification ran out of budget. Distinguishing this from the row below is how you tell the
  shared-deadline defect from the kill-return-value defect.
- **`scope_kill: blocked` + exact cgroup kill failed** — the class both independent re-derivations
  said survives every other fix: the retry re-signals from the stale `populated` reading, the
  cgroup is gone so the signal reports failure, and it never reaches the proof that would have
  passed trivially.
- **the `restart` record** — blocked at `ownership_capture`, containment never started. A different
  path, and the one the original analysis under-covered: four action types beyond `completion`
  share this machinery.

## Provenance

Captured from `fedora.local` and `suze.local` before VPE's recovery cleared them. `blghq7to`,
`x627n2uj` and `jklqlbm7` belonged to lanes that had already landed their work directly, so no
committed work depended on them.
