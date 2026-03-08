# Code Review

Review the area described below thoroughly. Trace through all logic paths until you have the complete picture. Find:

- **Dead/unused code** - functions, imports, variables, files with no callers
- **Dangling references** - broken imports, missing deps, orphaned calls after refactors
- **Stale/outdated content** - docs, comments, naming that doesn't match current code
- **Redundancy** - DRY violations, duplicate logic that should be consolidated
- **Over-complexity** - anything that could be simplified while maintaining functionality
- **Data flow gaps** - trace compound operations through all trigger paths (manual, automatic, CLI) and verify each path propagates complete data to shared functions. Watch for default parameters that silently degrade.
- **Self-consistency** - verify that guard clauses and validation checks in new code don't reject state created by the same call chain. Trace new functions back to their callers — if function A creates a directory and then calls function B, function B must not raise an error because that directory exists.
- **Safety defaults** - verify that cleanup, deletion, and garbage collection operations default to safe behavior. Destructive operations should preserve user data (e.g., skip dirty worktrees, refuse to delete uncommitted work) and require explicit opt-in (`--force`) for irreversible actions. Check that the implementation matches the scope's specified behavior for destructive operations.
- **Error UX** - verify that error messages match the scope's described user experience. When the tool's audience needs guidance to self-resolve (especially agent-facing tools), error messages should list the problem, explain resolution options, and only mention force/override flags as a last resort — not as the primary message.
- **Backwards Compatibility** - look for legacy/compatibility only things that can be cleaned up

For each finding, note: what, where (file:line), and why it's an issue.

## Output

Present a categorized summary of issues found:

- Group by category, not by file
- Prioritize by severity (critical → minor)
- Surface any questions needing clarification

Do not implement fixes yet - findings only for review and approval. Thanks!

---

## Directions

$request
