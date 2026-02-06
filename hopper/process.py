# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Process runner - unified stage runner for mill, refine, and ship."""

import logging
import os
import subprocess
from pathlib import Path

from hopper import config, prompt
from hopper.client import set_codex_thread_id, set_lode_state, set_lode_status
from hopper.codex import bootstrap_codex
from hopper.git import create_worktree, current_branch, is_dirty
from hopper.lodes import get_lode_dir
from hopper.runner import BaseRunner

logger = logging.getLogger(__name__)


def _has_makefile(worktree_path: Path) -> bool:
    """Check if worktree has a Makefile."""
    return (worktree_path / "Makefile").exists()


def _run_make_install(worktree_path: Path) -> bool:
    """Run 'make install' in the worktree to set up venv via uv.

    Returns True on success, False on failure.
    """
    try:
        result = subprocess.run(
            ["make", "install"],
            cwd=str(worktree_path),
        )
        if result.returncode != 0:
            logger.error(f"make install failed exit_code={result.returncode}")
            return False
        return True
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        logger.error(f"make install failed error={e}")
        return False


def _get_venv_env(worktree_path: Path, base_env: dict | None = None) -> dict:
    """Get environment dict with venv activated.

    Prepends .venv/bin to PATH and sets VIRTUAL_ENV.
    """
    env = dict(base_env) if base_env else os.environ.copy()

    venv_path = worktree_path / ".venv"
    venv_bin = venv_path / "bin"

    current_path = env.get("PATH", "")
    env["PATH"] = f"{venv_bin}:{current_path}" if current_path else str(venv_bin)
    env["VIRTUAL_ENV"] = str(venv_path)

    return env


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
        "next_stage": "shipped",
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
        logger.debug(f"setup dispatching lode={self.lode_id} stage={self.stage}")
        # All stages validate their stage
        if self.stage != self._claude_stage:
            self._setup_error = f"Lode {self.lode_id} is not in {self._claude_stage} stage."
            print(self._setup_error)
            logger.error(f"setup error lode={self.lode_id}: {self._setup_error}")
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
            self._setup_error = f"Project directory not found: {self.project_dir}"
            print(self._setup_error)
            logger.error(f"setup error lode={self.lode_id}: {self._setup_error}")
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
        logger.debug(f"mill setup complete lode={self.lode_id}")
        return None

    def _setup_refine(self) -> int | None:
        if not self.project_dir:
            self._setup_error = "No project directory found for lode."
            print(self._setup_error)
            logger.error(f"setup error lode={self.lode_id}: {self._setup_error}")
            return 1
        if not Path(self.project_dir).is_dir():
            self._setup_error = f"Project directory not found: {self.project_dir}"
            print(self._setup_error)
            logger.error(f"setup error lode={self.lode_id}: {self._setup_error}")
            return 1

        # Ensure worktree exists
        self.worktree_path = get_lode_dir(self.lode_id) / "worktree"
        if not self.worktree_path.is_dir():
            set_lode_status(self.socket_path, self.lode_id, "Creating worktree...")
            branch_name = f"hopper-{self.lode_id}"
            if not create_worktree(self.project_dir, self.worktree_path, branch_name):
                self._setup_error = "Failed to create git worktree."
                print(self._setup_error)
                logger.error(f"setup error lode={self.lode_id}: {self._setup_error}")
                return 1
            logger.debug(f"worktree created lode={self.lode_id} path={self.worktree_path}")

        # Set up venv via make install if project has a Makefile
        if _has_makefile(self.worktree_path):
            venv_path = self.worktree_path / ".venv"
            venv_missing = not venv_path.is_dir()
            if venv_missing:
                set_lode_status(self.socket_path, self.lode_id, "Running make install...")
                logger.debug(f"make install start lode={self.lode_id}")
                print(f"Running make install for {self.lode_id}...")
            if venv_missing and not _run_make_install(self.worktree_path):
                self._setup_error = "Failed to run make install."
                print(self._setup_error)
                logger.error(f"setup error lode={self.lode_id}: {self._setup_error}")
                return 1
            if venv_missing:
                logger.debug(f"make install complete lode={self.lode_id}")
            self.use_venv = True

        self._cwd = str(self.worktree_path)

        if self.is_first_run:
            # Load input from previous stage
            err = self._load_input()
            if err is not None:
                return err
            logger.debug(f"input loaded lode={self.lode_id}")

            if self.project_name:
                self._context["project"] = self.project_name
            if self.project_dir:
                self._context["dir"] = self.project_dir

            # Bootstrap Codex session
            err = self._bootstrap_codex()
            if err is not None:
                return err

        logger.debug(f"refine setup complete lode={self.lode_id}")
        return None

    def _setup_ship(self) -> int | None:
        if not self.project_dir:
            self._setup_error = "No project directory found for lode."
            print(self._setup_error)
            logger.error(f"setup error lode={self.lode_id}: {self._setup_error}")
            return 1
        if not Path(self.project_dir).is_dir():
            self._setup_error = f"Project directory not found: {self.project_dir}"
            print(self._setup_error)
            logger.error(f"setup error lode={self.lode_id}: {self._setup_error}")
            return 1

        # Validate worktree exists
        self.worktree_path = get_lode_dir(self.lode_id) / "worktree"
        if not self.worktree_path.is_dir():
            self._setup_error = f"Worktree not found: {self.worktree_path}"
            print(self._setup_error)
            logger.error(f"setup error lode={self.lode_id}: {self._setup_error}")
            return 1

        # Pre-flight: project repo must be clean
        if is_dirty(self.project_dir):
            self._setup_error = f"Project repo has uncommitted changes: {self.project_dir}"
            print(self._setup_error)
            print("Commit or stash changes before shipping.")
            logger.error(f"setup error lode={self.lode_id}: {self._setup_error}")
            return 1

        # Pre-flight: project repo must be on main or master
        branch = current_branch(self.project_dir)
        if branch not in ("main", "master"):
            self._setup_error = (
                f"Project repo is on branch '{branch}', expected 'main' or 'master'."
            )
            print(self._setup_error)
            print("Switch to the main branch before shipping.")
            logger.error(f"setup error lode={self.lode_id}: {self._setup_error}")
            return 1

        self._cwd = self.project_dir

        if self.is_first_run:
            # Load input from previous stage
            err = self._load_input()
            if err is not None:
                return err
            logger.debug(f"input loaded lode={self.lode_id}")

            self._context["branch"] = f"hopper-{self.lode_id}"
            self._context["worktree"] = str(self.worktree_path)
            if self.project_name:
                self._context["project"] = self.project_name
            if self.project_dir:
                self._context["dir"] = self.project_dir

        logger.debug(f"ship setup complete lode={self.lode_id}")
        return None

    def _load_input(self) -> int | None:
        """Load the previous stage's output as $input context."""
        if not self._input_from:
            return None
        input_path = get_lode_dir(self.lode_id) / f"{self._input_from}_out.md"
        if not input_path.exists():
            self._setup_error = f"Input not found: {input_path}"
            print(self._setup_error)
            logger.error(f"setup error lode={self.lode_id}: {self._setup_error}")
            return 1
        self._context["input"] = input_path.read_text()
        return None

    def _save_stage_input(self, content: str) -> None:
        """Save stage input to <stage>_in.md via atomic write."""
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
            return _get_venv_env(self.worktree_path, base_env)
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
        logger.debug(f"codex bootstrap start lode={self.lode_id}")
        print(f"Bootstrapping Codex session for {self.lode_id}...")

        context: dict[str, str] = {}
        if self.project_name:
            context["project"] = self.project_name
        if self.project_dir:
            context["dir"] = self.project_dir

        try:
            code_prompt = prompt.load("code", context=context if context else None)
        except FileNotFoundError:
            self._setup_error = "Prompt not found: prompts/code.md"
            print(self._setup_error)
            logger.error(f"setup error lode={self.lode_id}: {self._setup_error}")
            return 1

        env = self._get_subprocess_env() if self.use_venv else None
        set_lode_status(self.socket_path, self.lode_id, "Bootstrapping Codex...")
        exit_code, thread_id = bootstrap_codex(code_prompt, str(self.worktree_path), env=env)

        if exit_code == 127:
            self._setup_error = "codex command not found. Install codex to use code features."
            print(self._setup_error)
            logger.error(f"setup error lode={self.lode_id}: {self._setup_error}")
            return 1
        if exit_code != 0:
            self._setup_error = f"Codex bootstrap failed (exit {exit_code})."
            print(self._setup_error)
            logger.error(f"setup error lode={self.lode_id}: {self._setup_error}")
            return 1
        if not thread_id:
            self._setup_error = "Failed to capture Codex session ID from bootstrap."
            print(self._setup_error)
            logger.error(f"setup error lode={self.lode_id}: {self._setup_error}")
            return 1

        set_codex_thread_id(self.socket_path, self.lode_id, thread_id)
        print(f"Codex session {thread_id[:8]} ready.")
        logger.debug(f"codex bootstrap complete lode={self.lode_id} thread={thread_id[:8]}")
        return None


def run_process(lode_id: str, socket_path: Path) -> int:
    """Entry point for process command. Reads stage from server."""
    from hopper.client import connect

    # Configure file logging (mirrors server.py activity.log pattern)
    log_path = config.hopper_dir() / "processing.log"
    handler = logging.FileHandler(log_path)
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    hopper_logger = logging.getLogger("hopper")
    hopper_logger.setLevel(logging.DEBUG)
    hopper_logger.addHandler(handler)

    try:
        response = connect(socket_path, lode_id=lode_id)
        if not response:
            logger.error(f"connect failed lode={lode_id}")
            print(f"Failed to connect to server for lode {lode_id}")
            return 1

        lode_data = response.get("lode")
        if not lode_data:
            logger.error(f"lode not found lode={lode_id}")
            print(f"Lode {lode_id} not found")
            return 1

        stage = lode_data.get("stage", "")
        logger.info(f"process start lode={lode_id} stage={stage}")
        if stage not in STAGES:
            logger.error(f"unknown stage lode={lode_id} stage={stage}")
            print(f"Unknown stage: {stage}")
            set_lode_state(socket_path, lode_id, "error", f"Unknown stage: {stage}")
            return 1

        runner = ProcessRunner(lode_id, socket_path, stage)
        try:
            return runner.run()
        except Exception as exc:
            print(f"Error [{lode_id}]: {exc}")
            logger.exception(f"unexpected error lode={lode_id}")
            try:
                set_lode_state(socket_path, lode_id, "error", str(exc))
            except Exception:
                pass
            return 1
    finally:
        hopper_logger.removeHandler(handler)
        handler.close()
