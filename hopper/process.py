# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Process runner - unified stage runner for mill, refine, and ship."""

import logging
import os
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from hopper import config, prompt
from hopper.client import set_codex_thread_id, set_lode_branch, set_lode_state, set_lode_status
from hopper.codex import bootstrap_codex
from hopper.git import (
    commit_all,
    create_worktree,
    get_diff_numstat,
    head_sha,
    is_dirty,
    quarantine_dirty_repo,
)
from hopper.lodes import get_lode_dir, get_worktree_dir, slugify
from hopper.runner import BaseRunner, _descendant_pids, _sum_descendant_cpu_ms

logger = logging.getLogger(__name__)

SETUP_COMMAND_IDLE_TIMEOUT_SEC = 20 * 60
SETUP_COMMAND_ABSOLUTE_TIMEOUT_SEC = 60 * 60
SETUP_MONITOR_INTERVAL_SEC = 5.0
SETUP_OUTPUT_TAIL_BYTES = 64 * 1024
SETUP_OUTPUT_TAIL_LINES = 20
QUARANTINE_STATUS = "Quarantined dirty project repo to branch {branch}; continuing"


def _has_makefile(worktree_path: Path) -> bool:
    """Check if worktree has a Makefile."""
    return (worktree_path / "Makefile").exists()


def _run_make_install(
    worktree_path: Path, timeout_sec: float = SETUP_COMMAND_IDLE_TIMEOUT_SEC
) -> tuple[bool, str | None]:
    """Run 'make install' in the worktree to set up project tooling.

    Returns (ok, detail). detail is a tail of command output on failure.
    """
    ok, detail = _run_setup_command(["make", "install"], worktree_path, timeout_sec=timeout_sec)
    if not ok:
        logger.error(f"make install failed: {detail or 'unknown error'}")
    return ok, detail


def _run_setup_command(
    command: list[str],
    cwd: Path,
    *,
    timeout_sec: float,
    absolute_timeout_sec: float = SETUP_COMMAND_ABSOLUTE_TIMEOUT_SEC,
    env: dict | None = None,
) -> tuple[bool, str | None]:
    """Run a setup command with idle and absolute bounds plus a bounded output tail."""
    try:
        with tempfile.TemporaryFile() as output:
            proc = subprocess.Popen(
                command,
                cwd=str(cwd),
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            previous_sigterm = _install_setup_sigterm_handler(proc)
            try:
                started_at = time.monotonic()
                last_progress_at = started_at
                output_size = output.tell()
                cpu_ms = _sum_descendant_cpu_ms(proc.pid)
                io_chars = _sum_process_tree_io_chars(proc.pid)

                while True:
                    return_code = proc.poll()
                    if return_code is not None:
                        break

                    now = time.monotonic()
                    idle_for = now - last_progress_at
                    total_for = now - started_at
                    if total_for >= absolute_timeout_sec:
                        _terminate_process_group(proc)
                        detail = (
                            f"Setup exceeded the {int(absolute_timeout_sec)}s total cap; "
                            f"last progress was {int(idle_for)}s ago."
                        )
                        return False, _append_output_tail(detail, output)
                    if idle_for >= timeout_sec:
                        _terminate_process_group(proc)
                        detail = (
                            f"No setup progress for {int(timeout_sec)}s "
                            f"(ran {int(total_for)}s total)."
                        )
                        return False, _append_output_tail(detail, output)

                    wait_for = min(
                        SETUP_MONITOR_INTERVAL_SEC,
                        timeout_sec - idle_for,
                        absolute_timeout_sec - total_for,
                    )
                    try:
                        return_code = proc.wait(timeout=max(0.0, wait_for))
                        break
                    except subprocess.TimeoutExpired:
                        pass

                    next_output_size = output.tell()
                    next_cpu_ms = _sum_descendant_cpu_ms(proc.pid)
                    next_io_chars = _sum_process_tree_io_chars(proc.pid)
                    if (
                        next_output_size != output_size
                        or _changed_metric(cpu_ms, next_cpu_ms)
                        or _changed_metric(io_chars, next_io_chars)
                    ):
                        last_progress_at = time.monotonic()
                    output_size = next_output_size
                    cpu_ms = next_cpu_ms
                    io_chars = next_io_chars
            finally:
                if previous_sigterm is not None:
                    signal.signal(signal.SIGTERM, previous_sigterm)

            if return_code != 0:
                tail = _read_output_tail(output)
                detail = f"Exited with code {return_code}."
                if tail:
                    detail = f"{detail}\n{tail}"
                return False, detail

            return True, None
    except FileNotFoundError as e:
        return False, f"Command not found: {e.filename or command[0]}"
    except subprocess.SubprocessError as e:
        return False, str(e)


def _changed_metric(previous: int | None, current: int | None) -> bool:
    """Return whether an available cumulative activity metric changed."""
    return previous is not None and current is not None and previous != current


def _sum_process_tree_io_chars(root_pid: int) -> int | None:
    """Return Linux character I/O for a process tree, or None when unavailable."""
    total = 0
    observed = False
    for pid in [root_pid, *_descendant_pids(root_pid)]:
        try:
            lines = (Path("/proc") / str(pid) / "io").read_text().splitlines()
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            continue
        values: dict[str, int] = {}
        for line in lines:
            key, separator, raw_value = line.partition(":")
            if not separator:
                continue
            try:
                values[key] = int(raw_value.strip())
            except ValueError:
                continue
        if "rchar" in values or "wchar" in values:
            total += values.get("rchar", 0) + values.get("wchar", 0)
            observed = True
    return total if observed else None


def _append_output_tail(detail: str, output) -> str:
    """Append bounded command output to a setup failure detail."""
    tail = _read_output_tail(output)
    return f"{detail}\n{tail}" if tail else detail


def _install_setup_sigterm_handler(proc: subprocess.Popen):
    """Ensure terminating the runner also terminates its setup process group."""
    if threading.current_thread() is not threading.main_thread():
        return None

    previous = signal.getsignal(signal.SIGTERM)

    def terminate_setup(signum, frame) -> None:
        _terminate_process_group(proc)
        if callable(previous):
            previous(signum, frame)
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate_setup)
    return previous


def _terminate_process_group(proc: subprocess.Popen) -> None:
    """Terminate a subprocess group, then hard-kill if it ignores SIGTERM."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        logger.debug("Failed to terminate process group; terminating proc", exc_info=True)
        proc.terminate()

    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except Exception:
            logger.debug("Failed to kill process group; killing proc", exc_info=True)
            proc.kill()


def _read_output_tail(output) -> str | None:
    """Read a bounded tail from a temporary output file."""
    try:
        size = output.tell()
        output.seek(max(0, size - SETUP_OUTPUT_TAIL_BYTES))
        text = output.read().decode("utf-8", errors="replace").strip()
    except Exception:
        logger.debug("Failed to read setup output tail", exc_info=True)
        return None

    if not text:
        return None

    lines = text.splitlines()
    tail = "\n".join(lines[-SETUP_OUTPUT_TAIL_LINES:])
    if size > SETUP_OUTPUT_TAIL_BYTES:
        return f"...\n{tail}"
    return tail


def _get_worktree_env(worktree_path: Path, base_env: dict | None = None) -> dict:
    """Get environment dict with worktree tooling activated.

    Prepends .venv/bin and/or node_modules/.bin to PATH as available.
    Sets VIRTUAL_ENV when .venv is present.
    """
    env = dict(base_env) if base_env else os.environ.copy()

    prepend: list[str] = []

    venv_bin = worktree_path / ".venv" / "bin"
    if venv_bin.is_dir():
        prepend.append(str(venv_bin))
        env["VIRTUAL_ENV"] = str(worktree_path / ".venv")

    node_bin = worktree_path / "node_modules" / ".bin"
    if node_bin.is_dir():
        prepend.append(str(node_bin))

    if prepend:
        current_path = env.get("PATH", "")
        prefix = ":".join(prepend)
        env["PATH"] = f"{prefix}:{current_path}" if current_path else prefix

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
        self.use_env: bool = False
        self.scope: str = ""
        self.stage: str = ""
        self.lode_title: str = ""
        self.lode_branch: str = ""

    def _load_lode_data(self, lode_data: dict) -> None:
        self.stage = lode_data.get("stage", "")
        self.scope = lode_data.get("scope", "")
        self.lode_title = lode_data.get("title", "")
        self.lode_branch = lode_data.get("branch", "")

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
        if self.project_dir:
            if not Path(self.project_dir).is_dir():
                self._setup_error = f"Project directory not found: {self.project_dir}"
                print(self._setup_error)
                logger.error(f"setup error lode={self.lode_id}: {self._setup_error}")
                return 1
            if is_dirty(self.project_dir):
                rc = self._quarantine_or_error("Commit or stash changes before milling.")
                if rc is not None:
                    return rc

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
        self.worktree_path = get_worktree_dir(self.lode_id)
        if not self.worktree_path.is_dir():
            set_lode_status(self.socket_path, self.lode_id, "Creating worktree...")
            if self.lode_branch:
                branch_name = self.lode_branch
            else:
                slug = slugify(self.lode_title)
                branch_name = f"hopper-{self.lode_id}-{slug}" if slug else f"hopper-{self.lode_id}"
            self.worktree_path.parent.mkdir(parents=True, exist_ok=True)
            if not create_worktree(self.project_dir, self.worktree_path, branch_name):
                self._setup_error = "Failed to create git worktree."
                print(self._setup_error)
                logger.error(f"setup error lode={self.lode_id}: {self._setup_error}")
                return 1
            if not self.lode_branch:
                set_lode_branch(self.socket_path, self.lode_id, branch_name)
            self.lode_branch = branch_name
            logger.debug(f"worktree created lode={self.lode_id} path={self.worktree_path}")

        # Set up environment via make install if project has a Makefile
        if _has_makefile(self.worktree_path):
            has_venv = (self.worktree_path / ".venv").is_dir()
            has_node = (self.worktree_path / "node_modules").is_dir()
            needs_install = not has_venv and not has_node
            if needs_install:
                set_lode_status(self.socket_path, self.lode_id, "Running make install...")
                logger.debug(f"make install start lode={self.lode_id}")
                print(f"Running make install for {self.lode_id}...")
            if needs_install:
                install_ok, install_detail = _run_make_install(self.worktree_path)
            else:
                install_ok, install_detail = True, None
            if not install_ok:
                self._setup_error = "Failed to run make install."
                if install_detail:
                    self._setup_error = f"{self._setup_error}\n{install_detail}"
                print(self._setup_error)
                logger.error(f"setup error lode={self.lode_id}: {self._setup_error}")
                return 1
            if needs_install:
                logger.debug(f"make install complete lode={self.lode_id}")
            self.use_env = True

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

    def _quarantine_or_error(self, stash_hint: str) -> int | None:
        """Quarantine a dirty project repo, or fall back to the setup error.

        Returns None if setup should proceed (repo is now clean), or 1 if the
        caller should abort setup with the existing dirty-repo error.
        """
        branch = quarantine_dirty_repo(self.project_dir, self.lode_id)
        if branch is None:
            self._setup_error = f"Project repo has uncommitted changes: {self.project_dir}"
            print(self._setup_error)
            print(stash_hint)
            print(f"hint: after fixing, restart with: hop restart {self.lode_id}")
            logger.error(f"setup error lode={self.lode_id}: {self._setup_error}")
            return 1
        set_lode_status(self.socket_path, self.lode_id, QUARANTINE_STATUS.format(branch=branch))
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
        self.worktree_path = get_worktree_dir(self.lode_id)
        if not self.worktree_path.is_dir():
            self._setup_error = f"Worktree not found: {self.worktree_path}"
            print(self._setup_error)
            logger.error(f"setup error lode={self.lode_id}: {self._setup_error}")
            return 1

        # Activate worktree env if project has tooling (already installed by refine)
        if _has_makefile(self.worktree_path):
            self.use_env = True

        # Pre-flight: project repo must be clean
        if is_dirty(self.project_dir):
            rc = self._quarantine_or_error("Commit or stash changes before shipping.")
            if rc is not None:
                return rc

        self._cwd = str(self.worktree_path)

        if self.is_first_run:
            # Load input from previous stage
            err = self._load_input()
            if err is not None:
                return err
            logger.debug(f"input loaded lode={self.lode_id}")

            self._context["branch"] = (
                self.lode_branch if self.lode_branch else f"hopper-{self.lode_id}"
            )
            self._context["worktree"] = str(self.worktree_path)
            if self.project_name:
                self._context["project"] = self.project_name
            if self.project_dir:
                self._context["dir"] = self.project_dir

        logger.debug(f"ship setup complete lode={self.lode_id}")
        # Capture diff numstat for stats analysis
        if self.is_first_run:
            try:
                diff_output = get_diff_numstat(str(self.worktree_path))
                if diff_output:
                    lode_dir = get_lode_dir(self.lode_id)
                    diff_path = lode_dir / "diff.txt"
                    tmp_path = diff_path.with_suffix(".txt.tmp")
                    tmp_path.write_text(diff_output)
                    os.replace(tmp_path, diff_path)
                    logger.debug(f"diff numstat saved lode={self.lode_id}")
            except Exception:
                logger.warning(f"failed to capture diff numstat lode={self.lode_id}", exc_info=True)
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
        """Build environment with worktree tooling activated if applicable."""
        base_env = super()._get_subprocess_env()
        if self.use_env and self.worktree_path:
            return _get_worktree_env(self.worktree_path, base_env)
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

    def _snapshot_stuck_worktree(self) -> dict:
        """Commit dirty worktree contents after a stuck timeout."""
        try:
            if not self.worktree_path or not self.worktree_path.is_dir():
                return {"outcome": "no_worktree"}
            wt = str(self.worktree_path)
            if not is_dirty(wt):
                return {"outcome": "clean"}
            committed, error = commit_all(
                wt, f"hopper: auto-snapshot after stuck timeout ({self.lode_id})"
            )
            if not committed:
                return {"outcome": "failed", "git_error": error or "unknown git error"}
            sha = head_sha(wt)
            if sha is None:
                return {
                    "outcome": "failed",
                    "git_error": "snapshot commit succeeded but HEAD SHA could not be resolved",
                }
            return {"outcome": "committed", "sha": sha}
        except Exception as exc:
            logger.warning(f"failed to snapshot stuck worktree lode={self.lode_id}", exc_info=True)
            return {"outcome": "failed", "git_error": str(exc)}

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

        env = self._get_subprocess_env() if self.use_env else None
        set_lode_status(self.socket_path, self.lode_id, "Bootstrapping Codex...")
        exit_code, thread_id, failed_msg = bootstrap_codex(
            code_prompt, str(self.worktree_path), env=env
        )

        if exit_code == 127:
            self._setup_error = "codex command not found. Install codex to use code features."
            print(self._setup_error)
            logger.error(f"setup error lode={self.lode_id}: {self._setup_error}")
            return 1
        if exit_code == 124:
            self._setup_error = "Codex bootstrap timed out."
            print(self._setup_error)
            logger.error(f"setup error lode={self.lode_id}: {self._setup_error}")
            return 1
        if exit_code != 0:
            if failed_msg:
                self._setup_error = f"Codex bootstrap failed: {failed_msg}"
            else:
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
            emitted = set_lode_state(socket_path, lode_id, "error", f"Unknown stage: {stage}")
            return 0 if emitted else 1

        runner = ProcessRunner(lode_id, socket_path, stage)
        try:
            return runner.run()
        except Exception as exc:
            print(f"Error [{lode_id}]: {exc}")
            logger.exception(f"unexpected error lode={lode_id}")
            emitted = False
            try:
                emitted = set_lode_state(socket_path, lode_id, "error", str(exc))
            except Exception:
                logger.exception(f"failed to emit error state lode={lode_id}")
            return 0 if emitted else 1
    finally:
        hopper_logger.removeHandler(handler)
        handler.close()
