# Implement

Implement the task described below based on the plan we've reviewed.

1. **Work through it carefully** - Clean, maintainable code. KISS and DRY.

2. **Test when complete** - Only run the tests relevant to your changes, check Makefile for all options. Run each check through `hop check` (e.g. `hop check -- make test`, `hop check -- make ci`) so a failure can't be hidden: it prints only the output tail but exits with the command's real status. Never pipe a check straight through a pager yourself — `make ci 2>&1 | tail -30` reports `tail`'s exit 0, not make's red.

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
