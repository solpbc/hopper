# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Process runner - unified stage runner for mill, refine, and ship."""

from pathlib import Path

from hopper import prompt
from hopper.client import set_codex_thread_id, set_lode_status
from hopper.codex import bootstrap_codex
from hopper.git import create_worktree, current_branch, is_dirty
from hopper.lodes import get_lode_dir
from hopper.pyenv import get_venv_env, has_pyproject, setup_worktree_venv
from hopper.runner import BaseRunner

# Stage configuration: keyed by stage name
STAGES = {
    "mill": {
        "prompt": "mill",
        "done_status": "Mill complete",
        "next_stage": "refine",
        "always_dismiss": False,
        "input_from": None,
    },
    "refine": {
        "prompt": "refine",
        "done_status": "Refine complete",
        "next_stage": "ship",
        "always_dismiss": True,
        "input_from": "mill",
    },
    "ship": {
        "prompt": "ship",
        "done_status": "Ship complete",
        "next_stage": "",
        "always_dismiss": True,
        "input_from": "refine",
    },
}


class ProcessRunner(BaseRunner):
    """Unified runner for all lode stages."""

    def __init__(self, lode_id: str, socket_path: Path, stage: str):
        super().__init__(lode_id, socket_path)
        if stage not in STAGES:
            raise ValueError(f"Unknown stage: {stage}")
        cfg = STAGES[stage]
        self._claude_stage = stage
        self._done_label = f"{stage.capitalize()} done"
        self._done_status = cfg["done_status"]
        self._next_stage = cfg["next_stage"]
        self._always_dismiss = cfg["always_dismiss"]
        self._prompt_name: str = cfg["prompt"]
        self._input_from: str | None = cfg["input_from"]
        # Set by _setup
        self._context: dict[str, str] = {}
        self._cwd: str | None = None
        self.worktree_path: Path | None = None
        self.use_venv: bool = False
        self.scope: str = ""
        self.stage: str = ""

    def _load_lode_data(self, lode_data: dict) -> None:
        self.stage = lode_data.get("stage", "")
        self.scope = lode_data.get("scope", "")

    def _setup(self) -> int | None:
        # All stages validate their stage
        if self.stage != self._claude_stage:
            print(f"Lode {self.lode_id} is not in {self._claude_stage} stage.")
            return 1

        # Dispatch to per-stage setup
        setup_method = {
            "mill": self._setup_mill,
            "refine": self._setup_refine,
            "ship": self._setup_ship,
        }[self._claude_stage]
        return setup_method()

    def _setup_mill(self) -> int | None:
        if self.project_dir and not Path(self.project_dir).is_dir():
            print(f"Project directory not found: {self.project_dir}")
            return 1

        self._cwd = self.project_dir if self.project_dir else None
        if self.is_first_run:
            if self.project_name:
                self._context["project"] = self.project_name
            if self.project_dir:
                self._context["dir"] = self.project_dir
            if self.scope:
                self._context["scope"] = self.scope
                # Save raw scope as mill input
                self._save_stage_input(self.scope)
        return None

    def _setup_refine(self) -> int | None:
        if not self.project_dir:
            print("No project directory found for lode.")
            return 1
        if not Path(self.project_dir).is_dir():
            print(f"Project directory not found: {self.project_dir}")
            return 1

        # Ensure worktree exists
        self.worktree_path = get_lode_dir(self.lode_id) / "worktree"
        if not self.worktree_path.is_dir():
            set_lode_status(self.socket_path, self.lode_id, "Creating worktree...")
            branch_name = f"hopper-{self.lode_id}"
            if not create_worktree(self.project_dir, self.worktree_path, branch_name):
                print("Failed to create git worktree.")
                return 1

        # Set up venv if project has pyproject.toml
        if has_pyproject(self.worktree_path):
            venv_path = self.worktree_path / ".venv"
            if not venv_path.is_dir():
                set_lode_status(self.socket_path, self.lode_id, "Setting up venv...")
                print(f"Setting up virtual environment for {self.lode_id}...")
            if not setup_worktree_venv(self.worktree_path):
                print("Failed to set up virtual environment.")
                return 1
            self.use_venv = True

        self._cwd = str(self.worktree_path)

        if self.is_first_run:
            # Load input from previous stage
            err = self._load_input()
            if err is not None:
                return err

            if self.project_name:
                self._context["project"] = self.project_name
            if self.project_dir:
                self._context["dir"] = self.project_dir

            # Bootstrap Codex session
            err = self._bootstrap_codex()
            if err is not None:
                return err

        return None

    def _setup_ship(self) -> int | None:
        if not self.project_dir:
            print("No project directory found for lode.")
            return 1
        if not Path(self.project_dir).is_dir():
            print(f"Project directory not found: {self.project_dir}")
            return 1

        # Validate worktree exists
        self.worktree_path = get_lode_dir(self.lode_id) / "worktree"
        if not self.worktree_path.is_dir():
            print(f"Worktree not found: {self.worktree_path}")
            return 1

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

        self._cwd = self.project_dir

        if self.is_first_run:
            # Load input from previous stage
            err = self._load_input()
            if err is not None:
                return err

            self._context["branch"] = f"hopper-{self.lode_id}"
            self._context["worktree"] = str(self.worktree_path)
            if self.project_name:
                self._context["project"] = self.project_name
            if self.project_dir:
                self._context["dir"] = self.project_dir

        return None

    def _load_input(self) -> int | None:
        """Load the previous stage's output as $input context."""
        if not self._input_from:
            return None
        input_path = get_lode_dir(self.lode_id) / f"{self._input_from}_out.md"
        if not input_path.exists():
            print(f"Input not found: {input_path}")
            return 1
        self._context["input"] = input_path.read_text()
        return None

    def _save_stage_input(self, content: str) -> None:
        """Save stage input to <stage>_in.md via atomic write."""
        import os

        lode_dir = get_lode_dir(self.lode_id)
        lode_dir.mkdir(parents=True, exist_ok=True)
        path = lode_dir / f"{self._claude_stage}_in.md"
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(content)
        os.replace(tmp, path)

    def _get_subprocess_env(self) -> dict:
        """Build environment with venv activated if applicable."""
        base_env = super()._get_subprocess_env()
        if self.use_venv and self.worktree_path:
            return get_venv_env(self.worktree_path, base_env)
        return base_env

    def _build_command(self) -> tuple[list[str], str | None]:
        skip = "--dangerously-skip-permissions"

        if self.is_first_run:
            initial_prompt = prompt.load(
                self._prompt_name, context=self._context if self._context else None
            )
            cmd = ["claude", skip, "--session-id", self.claude_session_id, initial_prompt]
        else:
            cmd = ["claude", skip, "--resume", self.claude_session_id]

        return cmd, self._cwd

    def _bootstrap_codex(self) -> int | None:
        """Bootstrap a Codex session for the refine stage."""
        print(f"Bootstrapping Codex session for {self.lode_id}...")

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

        env = self._get_subprocess_env() if self.use_venv else None
        set_lode_status(self.socket_path, self.lode_id, "Bootstrapping Codex...")
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

        set_codex_thread_id(self.socket_path, self.lode_id, thread_id)
        print(f"Codex session {thread_id[:8]} ready.")
        return None


def run_process(lode_id: str, socket_path: Path) -> int:
    """Entry point for process command. Reads stage from server."""
    from hopper.client import connect

    response = connect(socket_path, lode_id=lode_id)
    if not response:
        print(f"Failed to connect to server for lode {lode_id}")
        return 1

    lode_data = response.get("lode")
    if not lode_data:
        print(f"Lode {lode_id} not found")
        return 1

    stage = lode_data.get("stage", "")
    if stage not in STAGES:
        print(f"Unknown stage: {stage}")
        return 1

    runner = ProcessRunner(lode_id, socket_path, stage)
    return runner.run()
