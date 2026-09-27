# Action Result Fixtures

These fixtures are production terminal action receipts, JSON-equal to stored records but pretty-printed with 2-space indentation. Production stores these as single-line JSONL in the lode state and pending-action records.

They exist to ensure changes to receipt validation cannot invalidate or brick receipts already stored on disk. Tests should always load these fixtures directly from disk through the real validator rather than rebuilding dictionaries in-process.

## Forward Boundary and Rollback Constraints

A server version prior to this change rejects any action receipt with a `null` retained value in two places:

1. **Lode records**: `find_action_result` and `append_action_result` validate every entry in `action_results`. An older server validates existing entries first during append and raises an exception, leaving no mechanism to displace or ignore the receipt. Once a lode carries a receipt with `null` fields, an older server refuses all subsequent actions on that lode with `action_result_invalid`, including stage completion. The lode remains wedged until the host is rolled forward.
2. **In-flight pending-action files**: `validate_pending_action` validates `record["result"]`. A pending record persisted with a `null` receipt will be rejected during startup reconciliation by an older server, marking the lode as carrying a malformed pending action.
3. **Rollback procedure**: In-flight actions must be drained before rolling back. Otherwise, an in-flight action on a lode that already carries a receipt with a `null` value will hang during clearance on the older server, surfacing only as a log line. Any action already past pending-clear will raise during startup reconciliation on the older server, preventing the daemon from starting.
