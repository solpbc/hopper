"""Refine runner - wraps Claude execution for processing stage sessions."""

from pathlib import Path

from hopper import prompt
from hopper.client import set_codex_thread_id
from hopper.codex import bootstrap_codex
from hopper.git import create_worktree
from hopper.runner import BaseRunner
from hopper.sessions import SHORT_ID_LEN, get_session_dir


class RefineRunner(BaseRunner):
    """Runs Claude for a processing-stage session with git worktree."""

    _done_label = "Refine done"
    _first_run_state = "ready"
    _done_status = "Refine complete"
    _next_stage = "ship"
    _always_dismiss = True

    def __init__(self, session_id: str, socket_path: Path):
        super().__init__(session_id, socket_path)
        self.worktree_path: Path | None = None
        self.shovel_content: str | None = None
        self.stage: str = ""

    def _load_session_data(self, session_data: dict) -> None:
        self.stage = session_data.get("stage", "")

    def _setup(self) -> int | None:
        # Validate stage
        if self.stage != "processing":
            print(f"Session {self.session_id[:SHORT_ID_LEN]} is not in processing stage.")
            return 1

        # Validate project directory
        if not self.project_dir:
            print("No project directory found for session.")
            return 1
        if not Path(self.project_dir).is_dir():
            print(f"Project directory not found: {self.project_dir}")
            return 1

        # Ensure worktree exists
        self.worktree_path = get_session_dir(self.session_id) / "worktree"
        if not self.worktree_path.is_dir():
            branch_name = f"hopper-{self.session_id[:SHORT_ID_LEN]}"
            if not create_worktree(self.project_dir, self.worktree_path, branch_name):
                print("Failed to create git worktree.")
                return 1

        # Load shovel doc for first run
        if self.is_first_run:
            shovel_path = get_session_dir(self.session_id) / "shovel.md"
            if not shovel_path.exists():
                print(f"Shovel document not found: {shovel_path}")
                return 1
            self.shovel_content = shovel_path.read_text()

            # Bootstrap Codex session for tasks
            err = self._bootstrap_codex()
            if err is not None:
                return err

        return None

    def _bootstrap_codex(self) -> int | None:
        """Bootstrap a Codex session using task.md prompt.

        Creates a new Codex session in the worktree and stores the thread_id
        on the server for subsequent hop task calls to resume.

        Returns:
            Exit code on failure, None on success.
        """
        sid = self.session_id[:SHORT_ID_LEN]
        print(f"Bootstrapping Codex session for {sid}...")

        # Build context for task prompt
        context: dict[str, str] = {}
        if self.project_name:
            context["project"] = self.project_name
        if self.project_dir:
            context["dir"] = self.project_dir

        try:
            task_prompt = prompt.load("task", context=context if context else None)
        except FileNotFoundError:
            print("Task prompt not found: prompts/task.md")
            return 1

        exit_code, thread_id = bootstrap_codex(task_prompt, str(self.worktree_path))

        if exit_code == 127:
            print("codex command not found. Install codex to use task features.")
            return 1
        if exit_code != 0:
            print(f"Codex bootstrap failed (exit {exit_code}).")
            return 1
        if not thread_id:
            print("Failed to capture Codex session ID from bootstrap.")
            return 1

        # Store thread_id on the server
        set_codex_thread_id(self.socket_path, self.session_id, thread_id)
        print(f"Codex session {thread_id[:8]} ready.")
        return None

    def _build_command(self) -> tuple[list[str], str | None]:
        cwd = str(self.worktree_path)

        skip = "--dangerously-skip-permissions"

        if self.is_first_run and self.shovel_content is not None:
            context: dict[str, str] = {"shovel": self.shovel_content}
            if self.project_name:
                context["project"] = self.project_name
            if self.project_dir:
                context["dir"] = self.project_dir
            initial_prompt = prompt.load("refine", context=context)
            cmd = ["claude", skip, "--session-id", self.session_id, initial_prompt]
        else:
            cmd = ["claude", skip, "--resume", self.session_id]

        return cmd, cwd


def run_refine(session_id: str, socket_path: Path) -> int:
    """Entry point for refine command."""
    runner = RefineRunner(session_id, socket_path)
    return runner.run()
