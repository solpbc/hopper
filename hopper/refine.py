"""Refine runner - wraps Claude execution for processing stage lodes."""

from pathlib import Path

from hopper import prompt
from hopper.client import set_codex_thread_id
from hopper.codex import bootstrap_codex
from hopper.git import create_worktree
from hopper.lodes import get_lode_dir
from hopper.pyenv import get_venv_env, has_pyproject, setup_worktree_venv
from hopper.runner import BaseRunner


class RefineRunner(BaseRunner):
    """Runs Claude for a processing-stage lode with git worktree."""

    _done_label = "Refine done"
    _first_run_state = "ready"
    _done_status = "Refine complete"
    _next_stage = "ship"
    _always_dismiss = True

    def __init__(self, lode_id: str, socket_path: Path):
        super().__init__(lode_id, socket_path)
        self.worktree_path: Path | None = None
        self.shovel_content: str | None = None
        self.stage: str = ""
        self.use_venv: bool = False

    def _load_lode_data(self, lode_data: dict) -> None:
        self.stage = lode_data.get("stage", "")

    def _setup(self) -> int | None:
        # Validate stage
        if self.stage != "processing":
            print(f"Lode {self.lode_id} is not in processing stage.")
            return 1

        # Validate project directory
        if not self.project_dir:
            print("No project directory found for lode.")
            return 1
        if not Path(self.project_dir).is_dir():
            print(f"Project directory not found: {self.project_dir}")
            return 1

        # Ensure worktree exists
        self.worktree_path = get_lode_dir(self.lode_id) / "worktree"
        if not self.worktree_path.is_dir():
            branch_name = f"hopper-{self.lode_id}"
            if not create_worktree(self.project_dir, self.worktree_path, branch_name):
                print("Failed to create git worktree.")
                return 1

        # Set up venv if project has pyproject.toml
        if has_pyproject(self.worktree_path):
            venv_path = self.worktree_path / ".venv"
            if not venv_path.is_dir():
                print(f"Setting up virtual environment for {self.lode_id}...")
            if not setup_worktree_venv(self.worktree_path):
                print("Failed to set up virtual environment.")
                return 1
            self.use_venv = True

        # Load shovel doc for first run
        if self.is_first_run:
            shovel_path = get_lode_dir(self.lode_id) / "shovel.md"
            if not shovel_path.exists():
                print(f"Shovel document not found: {shovel_path}")
                return 1
            self.shovel_content = shovel_path.read_text()

            # Bootstrap Codex session for stages
            err = self._bootstrap_codex()
            if err is not None:
                return err

        return None

    def _bootstrap_codex(self) -> int | None:
        """Bootstrap a Codex session using code.md prompt.

        Creates a new Codex session in the worktree and stores the thread_id
        on the server for subsequent hop code calls to resume.

        Returns:
            Exit code on failure, None on success.
        """
        print(f"Bootstrapping Codex session for {self.lode_id}...")

        # Build context for code prompt
        context: dict[str, str] = {}
        if self.project_name:
            context["project"] = self.project_name
        if self.project_dir:
            context["dir"] = self.project_dir

        try:
            code_prompt = prompt.load("code", context=context if context else None)
        except FileNotFoundError:
            print("Prompt not found: prompts/code.md")
            return 1

        # Pass venv environment if applicable
        env = self._get_subprocess_env() if self.use_venv else None
        exit_code, thread_id = bootstrap_codex(code_prompt, str(self.worktree_path), env=env)

        if exit_code == 127:
            print("codex command not found. Install codex to use code features.")
            return 1
        if exit_code != 0:
            print(f"Codex bootstrap failed (exit {exit_code}).")
            return 1
        if not thread_id:
            print("Failed to capture Codex session ID from bootstrap.")
            return 1

        # Store thread_id on the server
        set_codex_thread_id(self.socket_path, self.lode_id, thread_id)
        print(f"Codex session {thread_id[:8]} ready.")
        return None

    def _get_subprocess_env(self) -> dict:
        """Build environment with venv activated if applicable."""
        base_env = super()._get_subprocess_env()
        if self.use_venv and self.worktree_path:
            return get_venv_env(self.worktree_path, base_env)
        return base_env

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
            # Note: --session-id is Claude's flag, not ours
            cmd = ["claude", skip, "--session-id", self.lode_id, initial_prompt]
        else:
            # Note: --resume is Claude's flag, not ours
            cmd = ["claude", skip, "--resume", self.lode_id]

        return cmd, cwd


def run_refine(lode_id: str, socket_path: Path) -> int:
    """Entry point for refine command."""
    runner = RefineRunner(lode_id, socket_path)
    return runner.run()
