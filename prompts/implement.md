# Implement

Implement the task described below based on the plan we've reviewed.

1. **Work through it carefully** - Clean, maintainable code. KISS and DRY.

2. **Test when complete** - Run the narrowest tests and gates relevant to the changed behavior, checking the repository's available focus commands first. Run each check through `hop check --allow-capture` so a failure cannot be hidden: it prints only the output tail but exits with the command's real status. The flag is required because your tool call has no TTY, and it is your promise that your stdout is captured rather than piped into a command that would replace the exit code. Do not run the repository's full CI gate unless the directions explicitly require it under the refine policy. If required, run it after focused checks on the settled tree. Do not retry an unchanged failure merely to seek green, never pipe a check straight through a pager, and never detach a gate with `nohup` or a trailing `&` — that returns the launcher's status, not the gate's.

3. **Stay focused** - Only implement what was planned. No extra features or embellishments.

## Output

Summarize your work:

- Files changed
- Tests run and results
- Any issues or followups encountered

Implementation complete - ready for review. Thanks!

---

## Directions

$request
