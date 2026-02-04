"""Ship runner - wraps Claude execution for ship stage sessions."""

from pathlib import Path

from hopper import prompt
from hopper.git import current_branch, is_dirty
from hopper.runner import BaseRunner
from hopper.sessions import SHORT_ID_LEN, get_session_dir


class ShipRunner(BaseRunner):
    """Runs Claude for a ship-stage session to merge work back to main."""

    _done_label = "Ship done"
    _first_run_state = "ready"
    _done_status = "Ship complete"
    _next_stage = ""
    _always_dismiss = True

    def __init__(self, session_id: str, socket_path: Path):
        super().__init__(session_id, socket_path)
        self.worktree_path: Path | None = None
        self.branch_name: str = ""
        self.stage: str = ""

    def _load_session_data(self, session_data: dict) -> None:
        self.stage = session_data.get("stage", "")

    def _setup(self) -> int | None:
        sid = self.session_id[:SHORT_ID_LEN]

        # Validate stage
        if self.stage != "ship":
            print(f"Session {sid} is not in ship stage.")
            return 1

        # Validate project directory
        if not self.project_dir:
            print("No project directory found for session.")
            return 1
        if not Path(self.project_dir).is_dir():
            print(f"Project directory not found: {self.project_dir}")
            return 1

        # Validate worktree exists
        self.worktree_path = get_session_dir(self.session_id) / "worktree"
        if not self.worktree_path.is_dir():
            print(f"Worktree not found: {self.worktree_path}")
            return 1

        self.branch_name = f"hopper-{sid}"

        # Pre-flight: project repo must be clean
        if is_dirty(self.project_dir):
            print(f"Project repo has uncommitted changes: {self.project_dir}")
            print("Commit or stash changes before shipping.")
            return 1

        # Pre-flight: project repo must be on main or master
        branch = current_branch(self.project_dir)
        if branch not in ("main", "master"):
            print(f"Project repo is on branch '{branch}', expected 'main' or 'master'.")
            print("Switch to the main branch before shipping.")
            return 1

        return None

    def _build_command(self) -> tuple[list[str], str | None]:
        cwd = self.project_dir

        skip = "--dangerously-skip-permissions"

        if self.is_first_run:
            context: dict[str, str] = {
                "branch": self.branch_name,
                "worktree": str(self.worktree_path),
            }
            if self.project_name:
                context["project"] = self.project_name
            if self.project_dir:
                context["dir"] = self.project_dir
            initial_prompt = prompt.load("ship", context=context)
            cmd = ["claude", skip, "--session-id", self.session_id, initial_prompt]
        else:
            cmd = ["claude", skip, "--resume", self.session_id]

        return cmd, cwd


def run_ship(session_id: str, socket_path: Path) -> int:
    """Entry point for ship command."""
    runner = ShipRunner(session_id, socket_path)
    return runner.run()
