# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for the unified process runner module."""

import copy
import io
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from hopper import oom
from hopper.claude import spawn_claude
from hopper.git import (
    create_worktree as git_create_worktree,
)
from hopper.git import (
    is_dirty,
)
from hopper.git import (
    quarantine_dirty_repo as git_quarantine_dirty_repo,
)
from hopper.lodes import get_lode_dir, get_worktree_dir, load_lodes, save_lodes
from hopper.process import (
    QUARANTINE_STATUS,
    STAGES,
    ProcessRunner,
    _get_worktree_env,
    _install_setup_sigterm_handler,
    _make_install_target,
    _run_make_install,
    _run_setup_command,
    run_process,
    run_process_supervisor,
)
from hopper.server import Server

CLAUDE_SESSIONS = {
    "mill": {"session_id": "11111111-1111-1111-1111-111111111111", "started": False},
    "refine": {"session_id": "22222222-2222-2222-2222-222222222222", "started": False},
    "ship": {"session_id": "33333333-3333-3333-3333-333333333333", "started": False},
}

REAL_ARM_WORKER = oom.arm_worker


def _claude_sessions(**stage_overrides):
    """Return claude sessions dict with per-stage overrides."""
    sessions = copy.deepcopy(CLAUDE_SESSIONS)
    for stage, overrides in stage_overrides.items():
        sessions[stage].update(overrides)
    return sessions


def _mock_response(stage="mill", state="new", active=False, project="", claude=None, **extra):
    lode = {
        "state": state,
        "active": active,
        "project": project,
        "stage": stage,
        "scope": extra.get("scope", ""),
        "claude": claude or _claude_sessions(),
    }
    lode.update(extra)
    return {"type": "connected", "tmux": None, "lode": lode, "lode_found": True}


def _mock_conn(emitted=None):
    mock = MagicMock()
    callback_ref = None

    def emit(msg_type, **kw):
        if emitted is not None:
            emitted.append((msg_type, kw))
        if msg_type == "lode_set_branch" and callback_ref:
            callback_ref(
                {
                    "type": "lode_updated",
                    "lode": {
                        "id": kw["lode_id"],
                        "branch": kw["branch"],
                    },
                }
            )
        return True

    mock.emit.side_effect = emit

    def start(callback=None, on_connect=None):
        nonlocal callback_ref
        callback_ref = callback
        if on_connect:
            on_connect()
        if callback:
            callback({"type": "lode_registered", "lode_id": "test-id"})

    mock.start.side_effect = start
    return mock


@pytest.fixture(autouse=True)
def isolate_git_config(monkeypatch):
    """Keep real-git tests independent of user and system configuration."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)


@pytest.fixture(autouse=True)
def mock_worker_oom_boundary(monkeypatch):
    """Never touch the host's procfs/cgroup state from process tests."""
    monkeypatch.setattr(
        oom,
        "arm_worker",
        lambda **_kwargs: oom.OomCapability.NON_LINUX,
    )
    monkeypatch.setattr("hopper.process._sum_descendant_cpu_ms", lambda _pid: None)
    monkeypatch.setattr("hopper.process._sum_process_tree_io_chars", lambda _pid: None)


def _run_git(repo_dir, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    )


def _init_git_repo(tmp_path, *, name="repo", branch="main", bare=False):
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")

    repo_dir = tmp_path / name
    if bare:
        _run_git(tmp_path, "init", "--bare", "-b", branch, str(repo_dir))
        return repo_dir

    repo_dir.mkdir()
    _run_git(repo_dir, "init", "-b", branch)
    _run_git(repo_dir, "config", "user.email", "test@example.com")
    _run_git(repo_dir, "config", "user.name", "Test User")
    (repo_dir / "README.md").write_text("init\n")
    _run_git(repo_dir, "add", ".")
    _run_git(repo_dir, "commit", "-m", "init")
    return repo_dir


def _stale_clone(tmp_path, branch="main"):
    """Create a registered clone one commit behind its origin."""
    remote = _init_git_repo(tmp_path, name=f"{branch}-origin.git", branch=branch, bare=True)
    publisher = tmp_path / f"{branch}-publisher"
    _run_git(tmp_path, "clone", str(remote), str(publisher))
    _run_git(publisher, "config", "user.email", "test@example.com")
    _run_git(publisher, "config", "user.name", "Test User")
    (publisher / "README.md").write_text("initial\n")
    _run_git(publisher, "add", ".")
    _run_git(publisher, "commit", "-m", "initial")
    _run_git(publisher, "push", "-u", "origin", branch)

    registered = tmp_path / f"{branch}-registered"
    _run_git(tmp_path, "clone", str(remote), str(registered))
    _run_git(registered, "config", "user.email", "test@example.com")
    _run_git(registered, "config", "user.name", "Test User")
    local_sha = _run_git(registered, "rev-parse", "HEAD").stdout.strip()

    (publisher / "upstream.txt").write_text("upstream\n")
    _run_git(publisher, "add", ".")
    _run_git(publisher, "commit", "-m", "advance upstream")
    _run_git(publisher, "push", "origin", branch)
    upstream_sha = _run_git(publisher, "rev-parse", "HEAD").stdout.strip()
    return registered, publisher, local_sha, upstream_sha


def test_ship_next_stage_is_shipped():
    """Ship stage should transition to shipped on completion."""
    assert STAGES["ship"]["next_stage"] == "shipped"


class TestRunMakeInstall:
    def test_prefers_declared_hopper_install_target(self, tmp_path):
        """A project can keep runtime provisioning out of lode bootstrap."""
        (tmp_path / "Makefile").write_text(
            "install:\n\t@echo full > selected\nhopper-install:\n\t@echo lean > selected\n"
        )

        target = _make_install_target(tmp_path)
        ok, detail = _run_make_install(tmp_path, target=target)

        assert target == "hopper-install"
        assert ok is True
        assert detail is None
        assert (tmp_path / "selected").read_text().strip() == "lean"

    def test_falls_back_when_hopper_install_target_is_absent(self, tmp_path):
        """Existing Make projects retain the full install target contract."""
        (tmp_path / "Makefile").write_text(
            "# hopper-install: documentation only\ninstall:\n\t@echo full > selected\n"
        )

        target = _make_install_target(tmp_path)
        ok, detail = _run_make_install(tmp_path, target=target)

        assert target == "install"
        assert ok is True
        assert detail is None
        assert (tmp_path / "selected").read_text().strip() == "full"

    def test_detects_compact_hopper_install_rule(self, tmp_path):
        """Valid target syntax does not require whitespace after the colon."""
        (tmp_path / "Makefile").write_text(
            "bootstrap:\n\t@true\nhopper-install:bootstrap\n\t@true\n"
        )

        assert _make_install_target(tmp_path) == "hopper-install"

    def test_declared_hopper_install_failure_does_not_fall_back(self, tmp_path):
        """A broken lean target fails loudly instead of provisioning a runtime."""
        (tmp_path / "Makefile").write_text(
            "install:\n\t@echo full > selected\n"
            "hopper-install:\n\t@echo lean failed >&2\n\t@exit 9\n"
        )

        target = _make_install_target(tmp_path)
        ok, detail = _run_make_install(tmp_path, target=target)

        assert target == "hopper-install"
        assert ok is False
        assert detail is not None
        assert "lean failed" in detail
        assert not (tmp_path / "selected").exists()

    def test_returns_output_tail_on_failure(self, tmp_path):
        """make install failures include the command output tail."""
        (tmp_path / "Makefile").write_text(
            "install:\n\t@echo installing\n\t@echo failed >&2\n\t@exit 7\n"
        )

        ok, detail = _run_make_install(tmp_path)

        assert ok is False
        assert detail is not None
        assert "Exited with code 2." in detail
        assert "installing" in detail
        assert "failed" in detail
        assert "Error 7" in detail

    def test_times_out_and_kills_process_group(self, tmp_path):
        """Output-silent make install is bounded by inactivity."""
        (tmp_path / "Makefile").write_text("install:\n\t@sleep 30\n")

        ok, detail = _run_make_install(tmp_path, timeout_sec=0.1)

        assert ok is False
        assert detail is not None
        assert "No setup progress for 0s" in detail

    def test_process_io_extends_idle_bound(self, tmp_path):
        """A quiet downloader stays alive while its process-tree I/O advances."""
        command = [sys.executable, "-c", "import time; time.sleep(0.25)"]

        with (
            patch("hopper.process._sum_descendant_cpu_ms", return_value=None),
            patch(
                "hopper.process._sum_process_tree_io_chars",
                side_effect=[100, 200, 300, 400],
            ),
        ):
            ok, detail = _run_setup_command(command, tmp_path, timeout_sec=0.1)

        assert ok is True
        assert detail is None

    def test_absolute_cap_stops_continuously_active_setup(self, tmp_path):
        """Progress cannot keep a setup command alive past the absolute cap."""
        command = [sys.executable, "-c", "import time; time.sleep(30)"]

        with (
            patch("hopper.process._sum_descendant_cpu_ms", return_value=None),
            patch(
                "hopper.process._sum_process_tree_io_chars",
                side_effect=[100, 200, 300, 400],
            ),
        ):
            ok, detail = _run_setup_command(
                command,
                tmp_path,
                timeout_sec=0.2,
                absolute_timeout_sec=0.15,
            )

        assert ok is False
        assert detail is not None
        assert "total cap" in detail

    def test_sigterm_handler_kills_setup_process_group(self):
        """Killing a lode during setup also terminates make and its descendants."""
        proc = MagicMock()
        with (
            patch("hopper.process.signal.getsignal", return_value=signal.SIG_DFL),
            patch("hopper.process.signal.signal") as mock_signal,
            patch("hopper.process._terminate_process_group") as mock_terminate,
        ):
            previous = _install_setup_sigterm_handler(proc)
            handler = mock_signal.call_args.args[1]
            with pytest.raises(SystemExit, match="143"):
                handler(signal.SIGTERM, None)

        assert previous == signal.SIG_DFL
        mock_terminate.assert_called_once_with(proc)


class TestStuckWorktreeSnapshot:
    def test_snapshot_dirty_worktree_commits_expected_message(self, tmp_path):
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "refine")
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        runner.worktree_path = worktree

        with (
            patch("hopper.process.is_dirty", return_value=True) as mock_dirty,
            patch("hopper.process.commit_all", return_value=(True, None)) as mock_commit,
            patch("hopper.process.head_sha", return_value="a" * 40) as mock_head,
        ):
            outcome = runner._snapshot_stuck_worktree()

        assert outcome == {"outcome": "committed", "sha": "a" * 40}
        mock_dirty.assert_called_once_with(str(worktree))
        mock_commit.assert_called_once_with(
            str(worktree), "hopper: auto-snapshot after stuck timeout (test-id)"
        )
        mock_head.assert_called_once_with(str(worktree))

    def test_snapshot_clean_worktree_skips_commit(self, tmp_path):
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "refine")
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        runner.worktree_path = worktree

        with (
            patch("hopper.process.is_dirty", return_value=False) as mock_dirty,
            patch("hopper.process.commit_all") as mock_commit,
        ):
            outcome = runner._snapshot_stuck_worktree()

        assert outcome == {"outcome": "clean"}
        mock_dirty.assert_called_once_with(str(worktree))
        mock_commit.assert_not_called()

    def test_snapshot_without_worktree_skips_commit(self):
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "mill")

        with (
            patch("hopper.process.is_dirty") as mock_dirty,
            patch("hopper.process.commit_all") as mock_commit,
        ):
            outcome = runner._snapshot_stuck_worktree()

        assert outcome == {"outcome": "no_worktree"}
        mock_dirty.assert_not_called()
        mock_commit.assert_not_called()

    def test_snapshot_commit_failure_returns_git_error(self, tmp_path):
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "refine")
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        runner.worktree_path = worktree

        with (
            patch("hopper.process.is_dirty", return_value=True),
            patch(
                "hopper.process.commit_all",
                return_value=(False, "git add -A failed: index locked"),
            ),
        ):
            outcome = runner._snapshot_stuck_worktree()

        assert outcome == {
            "outcome": "failed",
            "git_error": "git add -A failed: index locked",
        }

    def test_snapshot_sha_resolution_failure_is_failed(self, tmp_path):
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "refine")
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        runner.worktree_path = worktree

        with (
            patch("hopper.process.is_dirty", return_value=True),
            patch("hopper.process.commit_all", return_value=(True, None)),
            patch("hopper.process.head_sha", return_value=None),
        ):
            outcome = runner._snapshot_stuck_worktree()

        assert outcome == {
            "outcome": "failed",
            "git_error": "snapshot commit succeeded but HEAD SHA could not be resolved",
        }

    def test_snapshot_unexpected_exception_returns_failed(self, tmp_path):
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "refine")
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        runner.worktree_path = worktree

        with patch("hopper.process.is_dirty", side_effect=RuntimeError("status failed")):
            outcome = runner._snapshot_stuck_worktree()

        assert outcome == {"outcome": "failed", "git_error": "status failed"}

    def test_index_lock_snapshot_is_failed_never_clean(self, tmp_path):
        repo_dir = _init_git_repo(tmp_path)
        (repo_dir / "README.md").write_text("changed\n")
        (repo_dir / ".git" / "index.lock").write_text("")
        runner = ProcessRunner("test-id", Path("/tmp/missing.sock"), "refine")
        runner.worktree_path = repo_dir

        outcome = runner._snapshot_stuck_worktree()

        assert outcome["outcome"] == "failed"
        assert outcome["git_error"].startswith("git add -A failed:")

    def test_fail_stuck_writes_recovery_without_server(self, temp_config):
        runner = ProcessRunner("test-id", Path("/tmp/missing.sock"), "mill")
        runner.lode_branch = "hopper-test-id"

        with (
            patch("hopper.runner.current_time_ms", return_value=1234),
            patch.object(runner, "_terminate_claude_process"),
        ):
            runner._fail_stuck("stuck reason")

        record = json.loads((get_lode_dir("test-id") / "recovery.json").read_text())
        assert record == {
            "failed_at": 1234,
            "stage": "mill",
            "reason": "stuck reason",
            "branch": "hopper-test-id",
            "worktree_path": None,
            "snapshot": {"outcome": "no_worktree"},
        }

    def test_idle_stage_parks_and_leaves_the_worktree_untouched(self, tmp_path, monkeypatch):
        """An idle stage parks: the agent lives, and hopper does not touch the worktree.

        The old behavior killed the agent and auto-committed the dirty worktree to
        rescue work from the kill. With no kill there is nothing to rescue -- the agent
        is still alive and still owns its files. Committing a live agent's worktree
        could capture half-written files, so hopper leaves it strictly alone.
        """
        repo_dir = _init_git_repo(tmp_path)
        worktree = tmp_path / "worktree"
        _run_git(repo_dir, "worktree", "add", str(worktree), "-b", "hopper-test-id")
        (worktree / "README.md").write_text("changed\n")
        (worktree / "new.txt").write_text("new\n")

        monkeypatch.setattr("hopper.runner.IDLE_THRESHOLD_MS", 100)
        monkeypatch.setattr("hopper.runner.STUCK_FAIL_THRESHOLD_MS", 100)
        monkeypatch.setattr("hopper.runner.current_time_ms", lambda: 1_000)

        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "refine")
        runner.worktree_path = worktree
        runner.lode_branch = "hopper-test-id"
        runner._pane_id = "%1"
        runner._last_snapshot = "Hello World"
        runner._last_pane_activity_ms = 0
        runner._stuck_since = 0
        runner._claude_proc = MagicMock(pid=1234)
        runner._claude_proc.poll.return_value = None

        with (
            patch("hopper.runner.capture_pane", return_value="Hello World"),
            patch(
                "hopper.runner.connect",
                return_value={"lode": {"last_progress_at": None, "last_progress_summary": None}},
            ),
            patch("hopper.runner._sum_descendant_cpu_ms", return_value=0),
            patch("hopper.runner._descendant_pids", return_value=[]),
        ):
            runner._check_activity()

        # THE POINT: the agent is alive and the worktree is untouched.
        runner._claude_proc.terminate.assert_not_called()
        runner._claude_proc.kill.assert_not_called()
        assert _run_git(worktree, "status", "--porcelain").stdout.strip() != ""
        assert "auto-snapshot" not in _run_git(worktree, "log", "-1", "--pretty=%B").stdout

        # It parked as gated, not errored, and recorded why.
        assert runner._gated.is_set()
        assert runner._stuck_error is None

        record = json.loads((get_lode_dir("test-id") / "recovery.json").read_text())
        assert record == {
            "parked_at": 1000,
            "state": "gated",
            "stage": "refine",
            "reason": "no pane output, heartbeat, or CPU activity for 1s",
            "branch": "hopper-test-id",
            "worktree_path": str(worktree),
            "terminated": False,
        }


# ---------------------------------------------------------------------------
# Mill stage tests
# ---------------------------------------------------------------------------


class TestMillStage:
    def test_emits_running_state(self):
        """Mill runner emits running state when Claude starts."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "mill")
        emitted = []

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(
                    stage="mill", state="running", claude=_claude_sessions(mill={"started": True})
                ),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn(emitted)),
            patch("subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 0
        assert any(e[0] == "lode_set_state" and e[1]["state"] == "running" for e in emitted)

    def test_bails_if_already_active(self):
        """Runner exits 1 if lode is already active."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "mill")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="mill", active=True),
            ),
        ):
            assert runner.run() == 1

        assert runner.connection is None

    def test_validates_stage(self):
        """Mill stage mismatch emits error and exits 0."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "mill")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="refine"),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()) as MockConn,
            patch("hopper.runner.get_current_pane_id", return_value="%0"),
        ):
            assert runner.run() == 0

        MockConn.return_value.emit.assert_any_call(
            "lode_set_state",
            lode_id="test-id",
            state="error",
            status="Lode test-id is not in mill stage.",
        )
        MockConn.return_value.stop.assert_called_once()

    def test_emits_error_on_nonzero_exit(self, capsys):
        """Non-zero Claude exit emits error and exits 0."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "mill")
        emitted = []

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(
                    stage="mill", state="running", claude=_claude_sessions(mill={"started": True})
                ),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn(emitted)),
            patch(
                "subprocess.Popen",
                return_value=MagicMock(returncode=1, stderr=io.BytesIO(b"")),
            ),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            assert runner.run() == 0

        assert "Exited with code 1" in capsys.readouterr().out
        assert any(e[0] == "lode_set_state" and e[1]["state"] == "error" for e in emitted)

    def test_captures_stderr_on_error(self, capsys):
        """Runner captures stderr as error message."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "mill")
        emitted = []

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(
                    stage="mill", state="running", claude=_claude_sessions(mill={"started": True})
                ),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn(emitted)),
            patch(
                "subprocess.Popen",
                return_value=MagicMock(
                    returncode=1, stderr=io.BytesIO(b"Error: something broke\n")
                ),
            ),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            runner.run()

        assert "something broke" in capsys.readouterr().out
        error_emissions = [
            e for e in emitted if e[0] == "lode_set_state" and e[1]["state"] == "error"
        ]
        assert "something broke" in error_emissions[0][1]["status"]

    def test_resume_uses_resume_flag(self):
        """Existing session uses --resume."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "mill")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(
                    stage="mill", state="running", claude=_claude_sessions(mill={"started": True})
                ),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()),
            patch(
                "subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)
            ) as mock_popen,
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            runner.run()

        cmd = mock_popen.call_args[0][0]
        assert cmd == [
            "claude",
            "--dangerously-skip-permissions",
            "--resume",
            CLAUDE_SESSIONS["mill"]["session_id"],
        ]

    def test_new_session_uses_session_id_and_prompt(self):
        """New session is durably marked started as soon as its process exists."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "mill")
        emitted = []

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="mill", state="new"),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn(emitted)),
            patch(
                "subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)
            ) as mock_popen,
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            runner.run()

        cmd = mock_popen.call_args[0][0]
        assert cmd[0] == "claude"
        assert cmd[2:4] == ["--session-id", CLAUDE_SESSIONS["mill"]["session_id"]]
        assert len(cmd) == 5  # claude, skip, --session-id, id, prompt
        started_index = next(
            index for index, event in enumerate(emitted) if event[0] == "lode_set_claude_started"
        )
        running_index = next(
            index
            for index, event in enumerate(emitted)
            if event[0] == "lode_set_state" and event[1]["state"] == "running"
        )
        assert started_index < running_index
        assert emitted[started_index][1] == {
            "lode_id": "test-id",
            "claude_stage": "mill",
        }

    def test_mill_and_refine_share_fetched_snapshot_without_moving_registered_checkout(
        self, tmp_path
    ):
        """Mill and refine use one fetched snapshot while leaving the checkout unchanged."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "mill")
        project_dir, publisher, local_sha, upstream_sha = _stale_clone(tmp_path)
        original_branch = _run_git(project_dir, "branch", "--show-current").stdout.strip()
        (project_dir / "README.md").write_text("dirty registered checkout\n")
        mock_project = MagicMock(path=str(project_dir))
        events = []

        def quarantine(repo_dir, lode_id):
            events.append("quarantine")
            return git_quarantine_dirty_repo(repo_dir, lode_id)

        def create(repo_dir, worktree_path, branch_name):
            events.append("create")
            return git_create_worktree(repo_dir, worktree_path, branch_name)

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(
                    stage="mill",
                    state="new",
                    project="my-project",
                ),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.quarantine_dirty_repo", side_effect=quarantine),
            patch("hopper.process.create_worktree", side_effect=create),
            patch("hopper.process.prompt.load", return_value="mill prompt"),
            patch.object(runner, "_run_claude", return_value=(0, None)) as mock_claude,
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            runner.run()

        worktree = get_worktree_dir("test-id")
        assert events == ["quarantine", "create"]
        assert runner.worktree_path == worktree
        assert runner.lode_branch == "hopper-test-id"
        assert runner._cwd == str(worktree)
        mock_claude.assert_called_once_with()
        assert (worktree / "upstream.txt").read_text() == "upstream\n"
        assert _run_git(worktree, "rev-parse", "HEAD").stdout.strip() == upstream_sha
        assert not (project_dir / "upstream.txt").exists()
        assert _run_git(project_dir, "branch", "--show-current").stdout.strip() == original_branch
        assert _run_git(project_dir, "rev-parse", "HEAD").stdout.strip() == local_sha
        assert _run_git(project_dir, "status", "--porcelain").stdout.strip() == ""

        (publisher / "later.txt").write_text("later\n")
        _run_git(publisher, "add", ".")
        _run_git(publisher, "commit", "-m", "advance after mill")
        _run_git(publisher, "push", "origin", "main")
        mill_sha = _run_git(worktree, "rev-parse", "HEAD").stdout.strip()

        refine = ProcessRunner("test-id", Path("/tmp/test.sock"), "refine")
        refine.project_dir = str(project_dir)
        refine.project_name = "my-project"
        refine.lode_branch = runner.lode_branch
        refine.is_first_run = False
        with patch("hopper.process.create_worktree") as mock_create:
            assert refine._setup_refine() is None

        mock_create.assert_not_called()
        assert refine.worktree_path == worktree
        assert refine._cwd == str(worktree)
        assert _run_git(worktree, "rev-parse", "HEAD").stdout.strip() == mill_sha
        assert not (worktree / "later.txt").exists()

    def test_resumed_mill_reuses_recorded_legacy_worktree_without_creation(self, tmp_path):
        """A resumed mill reuses its recorded legacy branch and existing snapshot."""
        repo_dir = _init_git_repo(tmp_path)
        worktree = get_worktree_dir("test-id")
        worktree.parent.mkdir(parents=True, exist_ok=True)
        branch = "hopper-test-id-legacy-title"
        _run_git(repo_dir, "worktree", "add", "-b", branch, str(worktree))
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "mill")
        runner.project_dir = str(repo_dir)
        runner.is_first_run = False
        runner.lode_branch = branch

        with (
            patch("hopper.process.current_branch") as mock_current_branch,
            patch("hopper.process.create_worktree") as mock_create,
            patch.object(runner, "_persist_lode_branch") as mock_persist,
        ):
            assert runner._setup_mill() is None

        assert runner.worktree_path == worktree
        assert runner.lode_branch == branch
        assert runner._cwd == str(worktree)
        mock_current_branch.assert_not_called()
        mock_create.assert_not_called()
        mock_persist.assert_not_called()

    @pytest.mark.parametrize("branch", ["", "hopper-test-id-legacy-title"])
    def test_resumed_mill_without_worktree_fails_before_launch(self, tmp_path, branch):
        """A legacy mill cannot replace its missing original snapshot."""
        repo_dir = _init_git_repo(tmp_path)
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "mill")
        mock_project = MagicMock(path=str(repo_dir))
        emitted = []

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(
                    stage="mill",
                    state="running",
                    project="my-project",
                    branch=branch,
                    claude=_claude_sessions(mill={"started": True}),
                ),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn(emitted)),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.create_worktree") as mock_create,
            patch.object(runner, "_run_claude") as mock_claude,
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            assert runner.run() == 0

        error = (
            "Cannot resume mill: the original mill snapshot cannot be reconstructed "
            "because its worktree is missing. Restart with: hop lode restart test-id"
        )
        assert ("lode_set_state", {"lode_id": "test-id", "state": "error", "status": error}) in (
            emitted
        )
        mock_create.assert_not_called()
        mock_claude.assert_not_called()

    def test_existing_worktree_blank_branch_persists_before_mill(self, tmp_path, make_lode):
        """Blank metadata is durable before mill accepts an existing worktree."""
        repo_dir = _init_git_repo(tmp_path)
        worktree = get_worktree_dir("test-id")
        worktree.parent.mkdir(parents=True, exist_ok=True)
        branch = "hopper-test-id"
        _run_git(repo_dir, "worktree", "add", "-b", branch, str(worktree))
        generation = "a" * 32
        save_lodes(
            [
                make_lode(
                    id="test-id",
                    project="my-project",
                    branch="",
                    run_generation=generation,
                )
            ]
        )
        server = Server(tmp_path / "hopper.sock")
        server.lodes = load_lodes()
        runner = ProcessRunner(
            "test-id",
            Path("/tmp/test.sock"),
            "mill",
            run_generation=generation,
        )
        runner.project_dir = str(repo_dir)
        runner.is_first_run = False
        connection = MagicMock()

        def emit(msg_type, **payload):
            server._handle_mutation(
                {
                    "type": msg_type,
                    "run_generation": generation,
                    **payload,
                },
                None,
            )
            return True

        connection.emit.side_effect = emit
        runner.connection = connection

        with (
            patch.object(server, "broadcast", side_effect=runner._on_server_message),
            patch("hopper.process.create_worktree") as mock_create,
        ):
            assert runner._setup_mill() is None

        persisted = load_lodes()
        assert persisted[0]["branch"] == branch
        assert json.loads((tmp_path / "active.jsonl").read_text())["branch"] == branch
        assert runner.worktree_path == worktree
        assert runner.lode_branch == branch
        assert runner._cwd == str(worktree)
        mock_create.assert_not_called()

    def test_existing_detached_worktree_with_blank_branch_fails(self, tmp_path):
        """Detached worktrees cannot supply missing branch metadata."""
        repo_dir = _init_git_repo(tmp_path)
        worktree = get_worktree_dir("test-id")
        worktree.parent.mkdir(parents=True, exist_ok=True)
        _run_git(repo_dir, "worktree", "add", "--detach", str(worktree))
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "mill")
        runner.project_dir = str(repo_dir)
        runner.is_first_run = False

        with patch.object(runner, "_persist_lode_branch") as mock_persist:
            assert runner._setup_mill() == 1

        assert runner.lode_branch == ""
        assert runner._setup_error == (
            f"Existing worktree has no current branch (detached HEAD): {worktree}"
        )
        mock_persist.assert_not_called()

    def test_unacknowledged_branch_persistence_fails_before_launch(self, tmp_path):
        """Mill does not launch until its branch update is observed after persistence."""
        repo_dir = _init_git_repo(tmp_path)
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "mill")
        mock_project = MagicMock(path=str(repo_dir))
        emitted = []
        connection = _mock_conn(emitted)

        def emit_without_branch_broadcast(msg_type, **payload):
            emitted.append((msg_type, payload))
            return True

        connection.emit.side_effect = emit_without_branch_broadcast
        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="mill", state="new", project="my-project"),
            ),
            patch("hopper.runner.HopperConnection", return_value=connection),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.runner.BRANCH_PERSIST_TIMEOUT_SEC", 0),
            patch.object(runner, "_run_claude") as mock_claude,
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            assert runner.run() == 0

        assert runner.lode_branch == ""
        assert any(
            msg_type == "lode_set_state"
            and payload["state"] == "error"
            and payload["status"] == "Failed to persist lode branch: hopper-test-id"
            for msg_type, payload in emitted
        )
        mock_claude.assert_not_called()

    @pytest.mark.parametrize(
        "detail",
        [
            "git fetch origin failed: network unavailable",
            (
                "upstream default branch resolution failed after git fetch origin: "
                "no candidate exists (origin/main, origin/master)"
            ),
            "git worktree add failed: post-checkout hook declined",
        ],
    )
    def test_worktree_creation_failure_detail_prevents_launch(self, tmp_path, detail):
        """Every worktree creation failure reaches the setup error unchanged."""
        repo_dir = _init_git_repo(tmp_path)
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "mill")
        mock_project = MagicMock(path=str(repo_dir))
        emitted = []

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="mill", state="new", project="my-project"),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn(emitted)),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.create_worktree", return_value=(False, detail)),
            patch.object(runner, "_run_claude") as mock_claude,
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            assert runner.run() == 0

        assert any(
            msg_type == "lode_set_state"
            and payload["state"] == "error"
            and payload["status"] == f"Failed to create git worktree: {detail}"
            for msg_type, payload in emitted
        )
        mock_claude.assert_not_called()

    @pytest.mark.parametrize("repository_kind", ["master-origin", "local-head"])
    def test_first_mill_supports_existing_base_variants(self, tmp_path, repository_kind):
        """First mill keeps create_worktree's master and no-origin base behavior."""
        if repository_kind == "master-origin":
            repo_dir, _publisher, _local_sha, expected_sha = _stale_clone(tmp_path, branch="master")
        else:
            repo_dir = _init_git_repo(tmp_path, branch="topic")
            expected_sha = _run_git(repo_dir, "rev-parse", "HEAD").stdout.strip()
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "mill")
        runner.project_dir = str(repo_dir)
        runner.is_first_run = True

        with (
            patch.object(runner, "_persist_lode_branch", return_value=True),
            patch("hopper.process.set_lode_status"),
        ):
            assert runner._setup_mill() is None

        assert runner.lode_branch == "hopper-test-id"
        assert _run_git(runner.worktree_path, "rev-parse", "HEAD").stdout.strip() == expected_sha

    def test_no_project_uses_none_cwd(self):
        """Runner passes cwd=None when no project set."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "mill")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(
                    stage="mill", state="running", claude=_claude_sessions(mill={"started": True})
                ),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()),
            patch("hopper.process.get_worktree_dir") as mock_worktree_dir,
            patch("hopper.process.current_branch") as mock_current_branch,
            patch("hopper.process.create_worktree") as mock_create,
            patch.object(runner, "_persist_lode_branch") as mock_persist,
            patch(
                "subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)
            ) as mock_popen,
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            runner.run()

        assert mock_popen.call_args[1]["cwd"] is None
        mock_worktree_dir.assert_not_called()
        mock_current_branch.assert_not_called()
        mock_create.assert_not_called()
        mock_persist.assert_not_called()

    def test_fails_if_project_dir_missing(self, tmp_path):
        """Missing project dir emits error and exits 0."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "mill")
        mock_project = MagicMock(path=str(tmp_path / "nope"))

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="mill", project="my-project"),
            ),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()) as MockConn,
            patch("hopper.runner.get_current_pane_id", return_value="%0"),
        ):
            assert runner.run() == 0

        MockConn.return_value.emit.assert_any_call(
            "lode_set_state",
            lode_id="test-id",
            state="error",
            status=f"Project directory not found: {mock_project.path}",
        )
        MockConn.return_value.stop.assert_called_once()

    def test_fails_if_repo_dirty(self, tmp_path, capsys):
        """Dirty repo emits error and exits 0."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "mill")
        project_dir = tmp_path / "my-project"
        project_dir.mkdir()
        mock_project = MagicMock(path=str(project_dir))

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="mill", project="my-project"),
            ),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.is_dirty", return_value=True),
            patch("hopper.process.quarantine_dirty_repo", return_value=None),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()) as MockConn,
            patch("hopper.runner.get_current_pane_id", return_value="%0"),
        ):
            assert runner.run() == 0

        out = capsys.readouterr().out
        assert "uncommitted changes" in out
        assert "hint: after fixing, restart with: hop restart test-id" in out
        MockConn.return_value.emit.assert_any_call(
            "lode_set_state",
            lode_id="test-id",
            state="error",
            status=f"Project repo has uncommitted changes: {project_dir}",
        )
        MockConn.return_value.stop.assert_called_once()

    def test_quarantines_dirty_repo(self, tmp_path):
        """Dirty project repo is quarantined before milling."""
        repo_dir = _init_git_repo(tmp_path)
        worktree = get_worktree_dir("test-id")
        worktree.parent.mkdir(parents=True, exist_ok=True)
        _run_git(
            repo_dir,
            "worktree",
            "add",
            "-b",
            "hopper-test-id",
            str(worktree),
        )
        (repo_dir / "README.md").write_text("changed\n")
        (repo_dir / "new.txt").write_text("new\n")
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "mill")
        runner.project_dir = str(repo_dir)
        runner.is_first_run = False
        runner.lode_branch = "hopper-test-id"

        with patch("hopper.process.set_lode_status") as mock_status:
            assert runner._setup_mill() is None

        assert runner._setup_error is None
        assert runner.worktree_path == worktree
        assert runner._cwd == str(worktree)
        assert is_dirty(str(repo_dir)) is False
        branch = _run_git(
            repo_dir,
            "for-each-ref",
            "--format=%(refname:short)",
            "refs/heads/hopper-quarantine-*",
        ).stdout.strip()
        assert branch.startswith("hopper-quarantine-")
        mock_status.assert_called_once_with(
            runner.socket_path,
            "test-id",
            QUARANTINE_STATUS.format(branch=branch),
        )
        assert mock_status.call_args[0][2].startswith(
            "Quarantined dirty project repo to branch hopper-quarantine-"
        )

    def test_loads_scope_in_context(self):
        """Runner passes scope to prompt template."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "mill")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="mill", state="new", scope="build the widget"),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()),
            patch("subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)),
            patch("hopper.runner.get_current_pane_id", return_value=None),
            patch("hopper.process.prompt.load", return_value="prompt") as mock_load,
        ):
            runner.run()

        context = mock_load.call_args[1]["context"]
        assert context["scope"] == "build the widget"

    def test_handles_missing_claude(self, capsys):
        """Missing claude emits error and exits 0."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "mill")
        emitted = []

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(
                    stage="mill", state="running", claude=_claude_sessions(mill={"started": True})
                ),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn(emitted)),
            patch("subprocess.Popen", side_effect=FileNotFoundError),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            assert runner.run() == 0

        assert "command not found" in capsys.readouterr().out.lower()

    def test_prints_on_unexpected_exception(self, capsys):
        """Unexpected exception emits error and exits 0."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "mill")
        emitted = []

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(
                    stage="mill", state="running", claude=_claude_sessions(mill={"started": True})
                ),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn(emitted)),
            patch.object(runner, "_run_claude", side_effect=RuntimeError("disk full")),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            assert runner.run() == 0

        assert "disk full" in capsys.readouterr().out
        assert any(
            e[0] == "lode_set_state" and e[1]["state"] == "error" and e[1]["status"] == "disk full"
            for e in emitted
        )

    def test_clean_exit_after_done_emits_ready_and_next_stage(self):
        """Mill emits state=ready then stage=refine after completion."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "mill")
        emitted = []

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="mill", state="new"),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn(emitted)),
            patch("subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            runner._done.set()
            runner.run()

        state_idx = next(
            i
            for i, e in enumerate(emitted)
            if e[0] == "lode_set_state" and e[1]["state"] == "ready"
        )
        stage_idx = next(
            i
            for i, e in enumerate(emitted)
            if e[0] == "lode_set_stage" and e[1]["stage"] == "refine"
        )
        assert state_idx < stage_idx
        assert "Mill complete" in emitted[state_idx][1]["status"]

    def test_clean_exit_without_done_no_transition(self):
        """No ready/stage transition if done was never signalled."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "mill")
        emitted = []

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(
                    stage="mill", state="running", claude=_claude_sessions(mill={"started": True})
                ),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn(emitted)),
            patch("subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            runner.run()

        assert not any(e[0] == "lode_set_stage" for e in emitted)
        assert not any(e[0] == "lode_set_state" and e[1]["state"] == "ready" for e in emitted)

    def test_connection_stopped_on_exit(self):
        """Runner stops connection on exit."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "mill")
        mock_conn = _mock_conn()

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(
                    stage="mill", state="running", claude=_claude_sessions(mill={"started": True})
                ),
            ),
            patch("hopper.runner.HopperConnection", return_value=mock_conn),
            patch("subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            runner.run()

        mock_conn.start.assert_called_once()
        mock_conn.stop.assert_called_once()


# ---------------------------------------------------------------------------
# Refine stage tests
# ---------------------------------------------------------------------------


class TestRefineStage:
    def _setup_refine(self, tmp_path, lode_id="test-id"):
        """Set up common refine test fixtures. Returns (session_dir, project_dir, mock_project)."""
        project_dir = tmp_path / "my-project"
        project_dir.mkdir()
        session_dir = tmp_path / "lodes" / lode_id
        session_dir.mkdir(parents=True)
        mock_project = MagicMock(path=str(project_dir))
        return session_dir, project_dir, mock_project

    def _setup_git_refine(self, tmp_path, *, branch, broken_origin=False):
        """Set up refine with a local bare origin and a real registered checkout."""
        remote = _init_git_repo(
            tmp_path,
            name="origin.git",
            branch=branch,
            bare=True,
        )
        publisher = tmp_path / "publisher"
        _run_git(tmp_path, "clone", str(remote), str(publisher))
        _run_git(publisher, "config", "user.email", "test@example.com")
        _run_git(publisher, "config", "user.name", "Test User")
        (publisher / "README.md").write_text("initial\n")
        _run_git(publisher, "add", ".")
        _run_git(publisher, "commit", "-m", "initial")
        _run_git(publisher, "push", "-u", "origin", branch)

        project_dir = tmp_path / "my-project"
        _run_git(tmp_path, "clone", str(remote), str(project_dir))
        if broken_origin:
            _run_git(
                project_dir,
                "remote",
                "set-url",
                "origin",
                str(tmp_path / "missing.git"),
            )
        session_dir = tmp_path / "lodes" / "test-id"
        session_dir.mkdir(parents=True)
        return session_dir, project_dir, MagicMock(path=str(project_dir))

    def test_first_run_bootstraps_codex_then_runs_claude(self, tmp_path):
        """First run bootstraps Codex then runs Claude with refine prompt."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "refine")
        session_dir, project_dir, mock_project = self._setup_refine(tmp_path)
        (session_dir / "mill_out.md").write_text("Build the widget")

        codex_calls = []

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="refine", state="ready", project="my-project"),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.process.create_worktree", return_value=(True, None)),
            patch("hopper.process.prompt.load", return_value="loaded prompt"),
            patch(
                "hopper.process.bootstrap_codex", return_value=(0, "codex-thread-abc", None)
            ) as mock_boot,
            patch(
                "hopper.process.set_codex_thread_id",
                side_effect=lambda s, sid, tid: codex_calls.append((sid, tid)),
            ),
            patch(
                "subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)
            ) as mock_popen,
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 0
        mock_boot.assert_called_once()
        assert codex_calls == [("test-id", "codex-thread-abc")]
        cmd = mock_popen.call_args[0][0]
        assert "--session-id" in cmd

    def test_first_run_emits_setup_status(self, tmp_path):
        """First-run refine emits setup status updates in order."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "refine")
        session_dir, project_dir, mock_project = self._setup_refine(tmp_path)
        (session_dir / "mill_out.md").write_text("Build the widget")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="refine", state="ready", project="my-project"),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.process.create_worktree", return_value=(True, None)),
            patch("hopper.process._has_makefile", return_value=True),
            patch("hopper.process._make_install_target", return_value="install"),
            patch("hopper.process._run_make_install", return_value=(True, None)),
            patch("hopper.process.prompt.load", return_value="loaded prompt"),
            patch("hopper.process.bootstrap_codex", return_value=(0, "codex-thread-abc", None)),
            patch("hopper.process.set_codex_thread_id", return_value=True),
            patch("hopper.process.set_lode_status") as mock_status,
            patch("subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 0
        assert mock_status.call_args_list == [
            call(runner.socket_path, runner.lode_id, "Creating worktree..."),
            call(runner.socket_path, runner.lode_id, "Running make install..."),
            call(runner.socket_path, runner.lode_id, "Bootstrapping Codex..."),
        ]

    def test_first_run_uses_declared_hopper_install_target(self, tmp_path):
        """Refine surfaces and executes the project's lean bootstrap target."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "refine")
        session_dir, project_dir, mock_project = self._setup_refine(tmp_path)
        (session_dir / "mill_out.md").write_text("Build the widget")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="refine", state="ready", project="my-project"),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.process.create_worktree", return_value=(True, None)),
            patch("hopper.process._has_makefile", return_value=True),
            patch("hopper.process._make_install_target", return_value="hopper-install"),
            patch("hopper.process._run_make_install", return_value=(True, None)) as mock_install,
            patch("hopper.process.prompt.load", return_value="loaded prompt"),
            patch("hopper.process.bootstrap_codex", return_value=(0, "codex-thread-abc", None)),
            patch("hopper.process.set_codex_thread_id", return_value=True),
            patch("hopper.process.set_lode_status") as mock_status,
            patch("subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 0
        mock_install.assert_called_once_with(runner.worktree_path, target="hopper-install")
        assert (
            call(runner.socket_path, runner.lode_id, "Running make hopper-install...")
            in mock_status.call_args_list
        )

    def test_make_install_failure_includes_detail(self, tmp_path):
        """Refine setup emits captured setup detail when make install fails."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "refine")
        session_dir, project_dir, mock_project = self._setup_refine(tmp_path)

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="refine", state="ready", project="my-project"),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()) as MockConn,
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.process.create_worktree", return_value=(True, None)),
            patch("hopper.process._has_makefile", return_value=True),
            patch(
                "hopper.process._run_make_install",
                return_value=(
                    False,
                    "No setup progress for 1200s (ran 1200s total).\npytest output tail",
                ),
            ),
            patch("hopper.runner.get_current_pane_id", return_value="%0"),
        ):
            assert runner.run() == 0

        MockConn.return_value.emit.assert_any_call(
            "lode_set_state",
            lode_id="test-id",
            state="error",
            status=(
                "Failed to run make install.\n"
                "No setup progress for 1200s (ran 1200s total).\n"
                "pytest output tail"
            ),
        )

    def test_no_makefile_skips_make_install(self, tmp_path):
        """First-run refine without Makefile skips make install and env setup."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "refine")
        session_dir, project_dir, mock_project = self._setup_refine(tmp_path)
        (session_dir / "mill_out.md").write_text("Build the widget")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="refine", state="ready", project="my-project"),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.process.create_worktree", return_value=(True, None)),
            patch("hopper.process._has_makefile", return_value=False),
            patch("hopper.process._run_make_install") as mock_make_install,
            patch("hopper.process.prompt.load", return_value="loaded prompt"),
            patch("hopper.process.bootstrap_codex", return_value=(0, "codex-thread-abc", None)),
            patch("hopper.process.set_codex_thread_id", return_value=True),
            patch("hopper.process.set_lode_status"),
            patch("subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 0
        mock_make_install.assert_not_called()
        assert runner.use_env is False

    def test_resume_skips_bootstrap(self, tmp_path):
        """Resume uses --resume and skips Codex bootstrap."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "refine")
        session_dir, project_dir, mock_project = self._setup_refine(tmp_path)
        worktree = session_dir / "worktree"
        worktree.mkdir()

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(
                    stage="refine",
                    state="running",
                    project="my-project",
                    branch="hopper-test-id",
                    claude=_claude_sessions(refine={"started": True}),
                ),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.process.create_worktree") as mock_wt,
            patch("hopper.process.bootstrap_codex", return_value=(0, "unused", None)) as mock_boot,
            patch(
                "subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)
            ) as mock_popen,
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 0
        mock_wt.assert_not_called()
        mock_boot.assert_not_called()
        cmd = mock_popen.call_args[0][0]
        assert "--resume" in cmd
        assert mock_popen.call_args[1]["cwd"] == str(worktree)

    def test_resume_skips_setup_status_with_node_modules(self, tmp_path):
        """Resume with existing worktree and node_modules emits no setup status updates."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "refine")
        session_dir, project_dir, mock_project = self._setup_refine(tmp_path)
        worktree = session_dir / "worktree"
        worktree.mkdir()
        (worktree / "node_modules").mkdir()

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(
                    stage="refine",
                    state="running",
                    project="my-project",
                    branch="hopper-test-id",
                    claude=_claude_sessions(refine={"started": True}),
                ),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.process._has_makefile", return_value=True),
            patch("hopper.process._run_make_install", return_value=(True, None)) as mock_install,
            patch("hopper.process.set_lode_status") as mock_status,
            patch("subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 0
        mock_status.assert_not_called()
        mock_install.assert_not_called()

    def test_resume_skips_setup_status(self, tmp_path):
        """Resume with existing worktree and venv emits no setup status updates."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "refine")
        session_dir, project_dir, mock_project = self._setup_refine(tmp_path)
        worktree = session_dir / "worktree"
        worktree.mkdir()
        (worktree / ".venv").mkdir()

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(
                    stage="refine",
                    state="running",
                    project="my-project",
                    branch="hopper-test-id",
                    claude=_claude_sessions(refine={"started": True}),
                ),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.process._has_makefile", return_value=True),
            patch("hopper.process._run_make_install", return_value=(True, None)),
            patch("hopper.process.set_lode_status") as mock_status,
            patch("subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 0
        mock_status.assert_not_called()

    def test_validates_stage(self):
        """Refine stage mismatch emits error and exits 0."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "refine")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="mill"),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()) as MockConn,
            patch("hopper.runner.get_current_pane_id", return_value="%0"),
        ):
            assert runner.run() == 0

        MockConn.return_value.emit.assert_any_call(
            "lode_set_state",
            lode_id="test-id",
            state="error",
            status="Lode test-id is not in refine stage.",
        )
        MockConn.return_value.stop.assert_called_once()

    def test_fails_if_no_project(self):
        """Missing project emits error and exits 0."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "refine")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="refine", project=""),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()) as MockConn,
            patch("hopper.runner.get_current_pane_id", return_value="%0"),
        ):
            assert runner.run() == 0

        MockConn.return_value.emit.assert_any_call(
            "lode_set_state",
            lode_id="test-id",
            state="error",
            status="No project directory found for lode.",
        )
        MockConn.return_value.stop.assert_called_once()

    def test_fails_if_project_dir_missing(self, tmp_path):
        """Missing project dir emits error and exits 0."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "refine")
        mock_project = MagicMock(path=str(tmp_path / "nope"))

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="refine", project="my-project"),
            ),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()) as MockConn,
            patch("hopper.runner.get_current_pane_id", return_value="%0"),
        ):
            assert runner.run() == 0

        MockConn.return_value.emit.assert_any_call(
            "lode_set_state",
            lode_id="test-id",
            state="error",
            status=f"Project directory not found: {mock_project.path}",
        )
        MockConn.return_value.stop.assert_called_once()

    def test_fails_if_worktree_creation_fails(self, tmp_path):
        """Worktree creation failure emits error and exits 0."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "refine")
        session_dir, project_dir, mock_project = self._setup_refine(tmp_path)

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="refine", project="my-project"),
            ),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch(
                "hopper.process.create_worktree",
                return_value=(False, "git fetch origin failed: fatal: unavailable"),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()) as MockConn,
            patch("hopper.runner.get_current_pane_id", return_value="%0"),
        ):
            assert runner.run() == 0

        MockConn.return_value.emit.assert_any_call(
            "lode_set_state",
            lode_id="test-id",
            state="error",
            status=("Failed to create git worktree: git fetch origin failed: fatal: unavailable"),
        )
        MockConn.return_value.stop.assert_called_once()

    def test_fetch_failure_sets_specific_error_without_git_side_effects(self, tmp_path):
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "refine")
        session_dir, project_dir, mock_project = self._setup_git_refine(
            tmp_path, branch="main", broken_origin=True
        )

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="refine", project="my-project"),
            ),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()) as MockConn,
            patch("hopper.runner.get_current_pane_id", return_value="%0"),
        ):
            assert runner.run() == 0

        error_updates = [
            item.kwargs
            for item in MockConn.return_value.emit.call_args_list
            if item.args == ("lode_set_state",) and item.kwargs.get("state") == "error"
        ]
        assert len(error_updates) == 1
        assert error_updates[0]["status"].startswith(
            "Failed to create git worktree: git fetch origin failed:"
        )
        assert not get_worktree_dir("test-id").exists()
        assert _run_git(project_dir, "branch", "--list", "hopper-test-id").stdout.strip() == ""

    def test_resolution_failure_sets_specific_error_without_git_side_effects(self, tmp_path):
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "refine")
        session_dir, project_dir, mock_project = self._setup_git_refine(tmp_path, branch="develop")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="refine", project="my-project"),
            ),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()) as MockConn,
            patch("hopper.runner.get_current_pane_id", return_value="%0"),
        ):
            assert runner.run() == 0

        MockConn.return_value.emit.assert_any_call(
            "lode_set_state",
            lode_id="test-id",
            state="error",
            status=(
                "Failed to create git worktree: upstream default branch resolution "
                "failed after git fetch origin: no candidate exists "
                "(origin/main, origin/master)"
            ),
        )
        assert not get_worktree_dir("test-id").exists()
        assert _run_git(project_dir, "branch", "--list", "hopper-test-id").stdout.strip() == ""

    def test_fails_if_input_missing_on_first_run(self, tmp_path):
        """Missing mill input emits error and exits 0."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "refine")
        session_dir, project_dir, mock_project = self._setup_refine(tmp_path)

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="refine", project="my-project"),
            ),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.process.create_worktree", return_value=(True, None)),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()) as MockConn,
            patch("hopper.runner.get_current_pane_id", return_value="%0"),
        ):
            assert runner.run() == 0

        MockConn.return_value.emit.assert_any_call(
            "lode_set_state",
            lode_id="test-id",
            state="error",
            status=f"Input not found: {session_dir / 'mill_out.md'}",
        )
        MockConn.return_value.stop.assert_called_once()

    def test_bootstrap_failure_bails(self, tmp_path, capsys):
        """Codex bootstrap failure emits error and exits 0."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "refine")
        session_dir, project_dir, mock_project = self._setup_refine(tmp_path)
        (session_dir / "mill_out.md").write_text("Build it")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="refine", project="my-project"),
            ),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.process.create_worktree", return_value=(True, None)),
            patch("hopper.process.prompt.load", return_value="prompt"),
            patch("hopper.process.bootstrap_codex", return_value=(1, None, None)),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()) as MockConn,
            patch("hopper.runner.get_current_pane_id", return_value="%0"),
        ):
            assert runner.run() == 0

        assert "bootstrap failed" in capsys.readouterr().out
        MockConn.return_value.emit.assert_any_call(
            "lode_set_state",
            lode_id="test-id",
            state="error",
            status="Codex bootstrap failed (exit 1).",
        )
        MockConn.return_value.stop.assert_called_once()

    def test_bootstrap_failure_with_turn_failed_message_bails(self, tmp_path):
        """Codex bootstrap failure with a turn.failed message emits that message."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "refine")
        session_dir, project_dir, mock_project = self._setup_refine(tmp_path)
        (session_dir / "mill_out.md").write_text("Build it")
        message = "You've hit your usage limit. try again at Jul 11th, 2026 9:36 AM."

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="refine", project="my-project"),
            ),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.process.create_worktree", return_value=(True, None)),
            patch("hopper.process.prompt.load", return_value="prompt"),
            patch("hopper.process.bootstrap_codex", return_value=(1, None, message)),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()) as MockConn,
            patch("hopper.runner.get_current_pane_id", return_value="%0"),
        ):
            assert runner.run() == 0

        MockConn.return_value.emit.assert_any_call(
            "lode_set_state",
            lode_id="test-id",
            state="error",
            status=f"Codex bootstrap failed: {message}",
        )
        MockConn.return_value.stop.assert_called_once()

    def test_bootstrap_timeout_bails(self, tmp_path):
        """Codex bootstrap timeout emits a setup error and releases the lode."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "refine")
        session_dir, project_dir, mock_project = self._setup_refine(tmp_path)
        (session_dir / "mill_out.md").write_text("Build it")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="refine", project="my-project"),
            ),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.process.create_worktree", return_value=(True, None)),
            patch("hopper.process.prompt.load", return_value="prompt"),
            patch("hopper.process.bootstrap_codex", return_value=(124, None, None)),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()) as MockConn,
            patch("hopper.runner.get_current_pane_id", return_value="%0"),
        ):
            assert runner.run() == 0

        MockConn.return_value.emit.assert_any_call(
            "lode_set_state",
            lode_id="test-id",
            state="error",
            status="Codex bootstrap timed out.",
        )

    def test_clean_exit_after_done_emits_ready_and_ship(self, tmp_path):
        """Refine emits state=ready then stage=ship after completion."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "refine")
        emitted = []
        session_dir, project_dir, mock_project = self._setup_refine(tmp_path)
        (session_dir / "mill_out.md").write_text("Build it")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="refine", state="ready", project="my-project"),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn(emitted)),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.process.create_worktree", return_value=(True, None)),
            patch("hopper.process.prompt.load", return_value="prompt"),
            patch("hopper.process.bootstrap_codex", return_value=(0, "thread-123", None)),
            patch("hopper.process.set_codex_thread_id", return_value=True),
            patch("subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            runner._done.set()
            runner.run()

        state_idx = next(
            i
            for i, e in enumerate(emitted)
            if e[0] == "lode_set_state" and e[1]["state"] == "ready"
        )
        stage_idx = next(
            i for i, e in enumerate(emitted) if e[0] == "lode_set_stage" and e[1]["stage"] == "ship"
        )
        assert state_idx < stage_idx
        assert "Refine complete" in emitted[state_idx][1]["status"]


# ---------------------------------------------------------------------------
# _get_worktree_env tests
# ---------------------------------------------------------------------------


class TestGetWorktreeEnv:
    def test_venv_only(self, tmp_path):
        """Prepends .venv/bin to PATH and sets VIRTUAL_ENV."""
        (tmp_path / ".venv" / "bin").mkdir(parents=True)
        env = _get_worktree_env(tmp_path, {"PATH": "/usr/bin"})
        assert env["PATH"].startswith(str(tmp_path / ".venv" / "bin"))
        assert env["VIRTUAL_ENV"] == str(tmp_path / ".venv")
        assert "node_modules" not in env["PATH"]

    def test_node_modules_only(self, tmp_path):
        """Prepends node_modules/.bin to PATH, no VIRTUAL_ENV."""
        (tmp_path / "node_modules" / ".bin").mkdir(parents=True)
        env = _get_worktree_env(tmp_path, {"PATH": "/usr/bin"})
        assert str(tmp_path / "node_modules" / ".bin") in env["PATH"]
        assert "VIRTUAL_ENV" not in env

    def test_both(self, tmp_path):
        """Both .venv/bin and node_modules/.bin prepended."""
        (tmp_path / ".venv" / "bin").mkdir(parents=True)
        (tmp_path / "node_modules" / ".bin").mkdir(parents=True)
        env = _get_worktree_env(tmp_path, {"PATH": "/usr/bin"})
        venv_pos = env["PATH"].index(str(tmp_path / ".venv" / "bin"))
        node_pos = env["PATH"].index(str(tmp_path / "node_modules" / ".bin"))
        assert venv_pos < node_pos  # venv first
        assert env["VIRTUAL_ENV"] == str(tmp_path / ".venv")

    def test_neither(self, tmp_path):
        """No tooling dirs — PATH unchanged, no VIRTUAL_ENV."""
        env = _get_worktree_env(tmp_path, {"PATH": "/usr/bin"})
        assert env["PATH"] == "/usr/bin"
        assert "VIRTUAL_ENV" not in env


# ---------------------------------------------------------------------------
# Ship stage tests
# ---------------------------------------------------------------------------


class TestShipStage:
    def _setup_ship(self, tmp_path, lode_id="test-id"):
        """Set up common ship test fixtures."""
        project_dir = tmp_path / "my-project"
        project_dir.mkdir()
        session_dir = tmp_path / "lodes" / lode_id
        session_dir.mkdir(parents=True)
        (session_dir / "worktree").mkdir()
        mock_project = MagicMock(path=str(project_dir))
        return session_dir, project_dir, mock_project

    def test_first_run_uses_ship_prompt(self, tmp_path):
        """First run loads ship prompt with branch and worktree context."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "ship")
        session_dir, project_dir, mock_project = self._setup_ship(tmp_path)
        (session_dir / "refine_out.md").write_text("Refine summary")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="ship", state="ready", project="my-project"),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.process.is_dirty", return_value=False),
            patch("hopper.process.prompt.load", return_value="loaded prompt") as mock_load,
            patch(
                "subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)
            ) as mock_popen,
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 0
        context = mock_load.call_args[1]["context"]
        assert context["branch"] == "hopper-test-id"
        assert context["worktree"] == str(session_dir / "worktree")
        assert context["input"] == "Refine summary"
        assert mock_popen.call_args[1]["cwd"] == str(session_dir / "worktree")

    def test_resume_uses_resume_flag(self, tmp_path):
        """Resume uses --resume."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "ship")
        session_dir, project_dir, mock_project = self._setup_ship(tmp_path)

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(
                    stage="ship",
                    state="running",
                    project="my-project",
                    claude=_claude_sessions(ship={"started": True}),
                ),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.process.is_dirty", return_value=False),
            patch(
                "subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)
            ) as mock_popen,
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 0
        cmd = mock_popen.call_args[0][0]
        assert "--resume" in cmd
        assert mock_popen.call_args[1]["cwd"] == str(session_dir / "worktree")

    def test_validates_stage(self, capsys):
        """Ship stage mismatch emits error and exits 0."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "ship")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="refine", project="my-project"),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()) as MockConn,
            patch("hopper.runner.get_current_pane_id", return_value="%0"),
        ):
            assert runner.run() == 0

        assert "not in ship stage" in capsys.readouterr().out
        MockConn.return_value.emit.assert_any_call(
            "lode_set_state",
            lode_id="test-id",
            state="error",
            status="Lode test-id is not in ship stage.",
        )
        MockConn.return_value.stop.assert_called_once()

    def test_fails_if_no_project(self):
        """Missing project emits error and exits 0."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "ship")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="ship", project=""),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()) as MockConn,
            patch("hopper.runner.get_current_pane_id", return_value="%0"),
        ):
            assert runner.run() == 0

        MockConn.return_value.emit.assert_any_call(
            "lode_set_state",
            lode_id="test-id",
            state="error",
            status="No project directory found for lode.",
        )
        MockConn.return_value.stop.assert_called_once()

    def test_fails_if_worktree_missing(self, tmp_path, capsys):
        """Missing worktree emits error and exits 0."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "ship")
        project_dir = tmp_path / "my-project"
        project_dir.mkdir()
        session_dir = tmp_path / "lodes" / "test-id"
        session_dir.mkdir(parents=True)
        # No worktree
        mock_project = MagicMock(path=str(project_dir))

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="ship", project="my-project"),
            ),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()) as MockConn,
            patch("hopper.runner.get_current_pane_id", return_value="%0"),
        ):
            assert runner.run() == 0

        assert "Worktree not found" in capsys.readouterr().out
        MockConn.return_value.emit.assert_any_call(
            "lode_set_state",
            lode_id="test-id",
            state="error",
            status=f"Worktree not found: {get_worktree_dir('test-id')}",
        )
        MockConn.return_value.stop.assert_called_once()

    def test_fails_if_repo_dirty(self, tmp_path, capsys):
        """Dirty repo emits error and exits 0."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "ship")
        session_dir, project_dir, mock_project = self._setup_ship(tmp_path)

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="ship", project="my-project"),
            ),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.process.is_dirty", return_value=True),
            patch("hopper.process.quarantine_dirty_repo", return_value=None),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()) as MockConn,
            patch("hopper.runner.get_current_pane_id", return_value="%0"),
        ):
            assert runner.run() == 0

        out = capsys.readouterr().out
        assert "uncommitted changes" in out
        assert "hint: after fixing, restart with: hop restart test-id" in out
        MockConn.return_value.emit.assert_any_call(
            "lode_set_state",
            lode_id="test-id",
            state="error",
            status=f"Project repo has uncommitted changes: {project_dir}",
        )
        MockConn.return_value.stop.assert_called_once()

    def test_quarantines_dirty_repo(self, tmp_path):
        """Dirty project repo is quarantined before shipping."""
        repo_dir = _init_git_repo(tmp_path)
        (repo_dir / "README.md").write_text("changed\n")
        (repo_dir / "new.txt").write_text("new\n")
        session_dir = tmp_path / "lodes" / "test-id"
        (session_dir / "worktree").mkdir(parents=True)
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "ship")
        runner.project_dir = str(repo_dir)
        runner.is_first_run = False

        with (
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.process.set_lode_status") as mock_status,
        ):
            assert runner._setup_ship() is None

        assert runner._setup_error is None
        assert is_dirty(str(repo_dir)) is False
        branch = _run_git(
            repo_dir,
            "for-each-ref",
            "--format=%(refname:short)",
            "refs/heads/hopper-quarantine-*",
        ).stdout.strip()
        assert branch.startswith("hopper-quarantine-")
        mock_status.assert_called_once_with(
            runner.socket_path,
            "test-id",
            QUARANTINE_STATUS.format(branch=branch),
        )
        assert mock_status.call_args[0][2].startswith(
            "Quarantined dirty project repo to branch hopper-quarantine-"
        )

    def test_emits_shipped_stage_transition_on_completion(self, tmp_path):
        """Ship emits a stage transition to shipped after completion."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "ship")
        emitted = []
        session_dir, project_dir, mock_project = self._setup_ship(tmp_path)
        (session_dir / "refine_out.md").write_text("done")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="ship", state="ready", project="my-project"),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn(emitted)),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.process.is_dirty", return_value=False),
            patch("hopper.process.prompt.load", return_value="prompt"),
            patch("subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            runner._done.set()
            runner.run()

        assert any(
            e[0] == "lode_set_state"
            and e[1]["state"] == "ready"
            and "Ship complete" in e[1]["status"]
            for e in emitted
        )
        assert any(e[0] == "lode_set_stage" and e[1]["stage"] == "shipped" for e in emitted)

    def test_ship_activates_worktree_env(self, tmp_path):
        """Ship activates worktree env when Makefile is present."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "ship")
        session_dir, project_dir, mock_project = self._setup_ship(tmp_path)
        (session_dir / "refine_out.md").write_text("Refine summary")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="ship", state="ready", project="my-project"),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.process.is_dirty", return_value=False),
            patch("hopper.process._has_makefile", return_value=True),
            patch("hopper.process.prompt.load", return_value="loaded prompt"),
            patch("subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 0
        assert runner.use_env is True

    def test_ship_no_env_without_makefile(self, tmp_path):
        """Ship does not activate worktree env without Makefile."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "ship")
        session_dir, project_dir, mock_project = self._setup_ship(tmp_path)
        (session_dir / "refine_out.md").write_text("Refine summary")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="ship", state="ready", project="my-project"),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.process.is_dirty", return_value=False),
            patch("hopper.process._has_makefile", return_value=False),
            patch("hopper.process.prompt.load", return_value="loaded prompt"),
            patch("subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 0
        assert runner.use_env is False

    def test_first_run_writes_diff_txt(self, tmp_path):
        """First run captures diff numstat to diff.txt."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "ship")
        session_dir, project_dir, mock_project = self._setup_ship(tmp_path)
        (session_dir / "refine_out.md").write_text("Refine summary")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="ship", state="ready", project="my-project"),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.process.is_dirty", return_value=False),
            patch("hopper.process.get_diff_numstat", return_value="10\t5\tfile.py"),
            patch("hopper.process.prompt.load", return_value="loaded prompt"),
            patch("subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 0
        diff_file = session_dir / "diff.txt"
        assert diff_file.exists()
        assert diff_file.read_text() == "10\t5\tfile.py"

    def test_first_run_no_diff_txt_when_empty(self, tmp_path):
        """No diff.txt when diff numstat returns empty."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "ship")
        session_dir, project_dir, mock_project = self._setup_ship(tmp_path)
        (session_dir / "refine_out.md").write_text("Refine summary")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="ship", state="ready", project="my-project"),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.process.is_dirty", return_value=False),
            patch("hopper.process.get_diff_numstat", return_value=""),
            patch("hopper.process.prompt.load", return_value="loaded prompt"),
            patch("subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 0
        assert not (session_dir / "diff.txt").exists()

    def test_diff_failure_does_not_abort_setup(self, tmp_path):
        """Diff numstat failure does not prevent ship setup."""
        runner = ProcessRunner("test-id", Path("/tmp/test.sock"), "ship")
        session_dir, project_dir, mock_project = self._setup_ship(tmp_path)
        (session_dir / "refine_out.md").write_text("Refine summary")

        with (
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(stage="ship", state="ready", project="my-project"),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()),
            patch("hopper.runner.find_project", return_value=mock_project),
            patch("hopper.process.get_lode_dir", return_value=session_dir),
            patch("hopper.process.is_dirty", return_value=False),
            patch("hopper.process.get_diff_numstat", side_effect=Exception("git broke")),
            patch("hopper.process.prompt.load", return_value="loaded prompt"),
            patch("subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = runner.run()

        assert exit_code == 0
        assert not (session_dir / "diff.txt").exists()


# ---------------------------------------------------------------------------
# run_process entry point tests
# ---------------------------------------------------------------------------


class TestRunProcess:
    def test_dispatches_to_correct_stage(self):
        """run_process reads stage from server and creates correct runner."""
        with (
            patch(
                "hopper.client.connect",
                return_value={"lode": {"stage": "mill"}},
            ),
            patch(
                "hopper.runner.connect",
                return_value=_mock_response(
                    stage="mill", state="running", claude=_claude_sessions(mill={"started": True})
                ),
            ),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()),
            patch("subprocess.Popen", return_value=MagicMock(returncode=0, stderr=None)),
            patch("hopper.runner.get_current_pane_id", return_value=None),
        ):
            exit_code = run_process("test-id", Path("/tmp/test.sock"))

        assert exit_code == 0

    def test_fails_on_unknown_stage(self, capsys):
        """Unknown stage emits error and exits 0."""
        with (
            patch(
                "hopper.client.connect",
                return_value={"lode": {"stage": "unknown"}},
            ),
            patch("hopper.process.set_lode_state") as mock_set_state,
        ):
            assert run_process("test-id", Path("/tmp/test.sock")) == 0

        assert "Unknown stage" in capsys.readouterr().out
        mock_set_state.assert_called_once_with(
            Path("/tmp/test.sock"),
            "test-id",
            "error",
            "Unknown stage: unknown",
        )

    def test_fails_if_lode_not_found(self, capsys):
        """run_process fails if lode not on server."""
        with patch("hopper.client.connect", return_value={"lode": None}):
            assert run_process("test-id", Path("/tmp/test.sock")) == 1

    def test_fails_if_connect_fails(self, capsys):
        """run_process fails if server connection fails."""
        with patch("hopper.client.connect", return_value=None):
            assert run_process("test-id", Path("/tmp/test.sock")) == 1

    def test_prints_on_unexpected_exception(self, capsys):
        """Unexpected exception emits error and exits 0."""
        with (
            patch(
                "hopper.client.connect",
                return_value={"lode": {"stage": "mill"}},
            ),
            patch.object(ProcessRunner, "run", side_effect=RuntimeError("unexpected crash")),
            patch("hopper.process.set_lode_state") as mock_set_state,
        ):
            assert run_process("test-id", Path("/tmp/test.sock")) == 0

        assert "unexpected crash" in capsys.readouterr().out
        mock_set_state.assert_called_once_with(
            Path("/tmp/test.sock"),
            "test-id",
            "error",
            "unexpected crash",
        )


class TestProcessingLog:
    """Tests for processing.log file handler setup."""

    def test_processing_log_created(self, isolate_config):
        """processing.log is created by run_process."""
        log_path = isolate_config / "processing.log"
        with (
            patch("hopper.client.connect", return_value={"lode": {"stage": "mill"}}),
            patch("hopper.runner.connect", return_value=_mock_response()),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()),
            patch("hopper.process.ProcessRunner.run", return_value=0),
        ):
            run_process("test-id", Path("/tmp/test.sock"))

        assert log_path.exists()
        content = log_path.read_text()
        assert "process start" in content
        assert "lode=test-id" in content

    def test_processing_log_contains_stage(self, isolate_config):
        """processing.log includes the loaded stage."""
        log_path = isolate_config / "processing.log"
        with (
            patch("hopper.client.connect", return_value={"lode": {"stage": "refine"}}),
            patch("hopper.runner.connect", return_value=_mock_response()),
            patch("hopper.runner.HopperConnection", return_value=_mock_conn()),
            patch("hopper.process.ProcessRunner.run", return_value=0),
        ):
            run_process("test-id", Path("/tmp/test.sock"))

        content = log_path.read_text()
        assert "stage=refine" in content

    def test_processing_log_error_path(self, isolate_config):
        """processing.log captures connection failures."""
        log_path = isolate_config / "processing.log"
        with patch("hopper.client.connect", return_value=None):
            run_process("fail-id", Path("/tmp/test.sock"))

        content = log_path.read_text()
        assert "connect failed" in content
        assert "lode=fail-id" in content

    def test_processing_log_handler_cleaned_up(self, isolate_config):
        """Handler is removed after run_process completes."""
        log_path = isolate_config / "processing.log"
        hopper_logger = logging.getLogger("hopper")
        initial_count = len(hopper_logger.handlers)
        with patch("hopper.client.connect", return_value=None):
            run_process("test-id", Path("/tmp/test.sock"))
        assert len(hopper_logger.handlers) == initial_count
        assert not any(
            isinstance(handler, logging.FileHandler)
            and Path(getattr(handler, "baseFilename", "")) == log_path
            for handler in hopper_logger.handlers
        )


class TestOomBoundary:
    class _ScopeResultClock:
        def __init__(self, reads):
            self.now = 0.0
            self.reads = reads
            self.read_count = 0
            self.sleeps = []
            self.read_calls = []

        def monotonic(self):
            return self.now

        def sleep(self, duration):
            self.sleeps.append(duration)
            self.now += duration

        def read_scope_result(self, systemctl, unit_name, *, timeout):
            self.read_calls.append((systemctl, unit_name, self.now, timeout))
            index = min(self.read_count, len(self.reads) - 1)
            result, consumed = self.reads[index]
            self.read_count += 1
            self.now += min(consumed, timeout)
            return result

    def test_pane_command_is_identical_when_guard_environment_is_present(self):
        with patch("hopper.claude.new_window", return_value="%1") as new_window:
            spawn_claude("test-id", "/repo")
            plain_command = new_window.call_args.args[0]
            spawn_claude(
                "test-id",
                "/repo",
                env={"HOPPER_RUN_GENERATION": "a" * 32, "HOPPER_OOM_SCOPE": "unit.scope"},
            )
            guarded_command = new_window.call_args.args[0]

        assert guarded_command == plain_command
        assert "hop process test-id" in guarded_command

    def test_scope_argv_is_unique_and_has_no_resource_limits(self):
        first = oom.scope_unit_name("test-id", "a" * 32)
        second = oom.scope_unit_name("test-id", "b" * 32)

        assert first != second
        argv = oom.build_scope_argv("/usr/bin/systemd-run", "/usr/bin/hop", first, "test-id")
        assert argv == [
            "/usr/bin/systemd-run",
            "--user",
            "--scope",
            f"--unit={first}",
            "--property=OOMPolicy=kill",
            "--",
            "/usr/bin/hop",
            "process-worker",
            "test-id",
        ]
        joined = " ".join(argv)
        for forbidden in ("MemoryMax", "MemorySwapMax", "TasksMax", "CPUQuota", "--collect"):
            assert forbidden not in joined

    def test_memory_group_is_read_once_from_resolved_cgroup(self, monkeypatch):
        reads = []

        def read_text(path):
            reads.append(path)
            if path == Path("/proc/self/cgroup"):
                return "0::/user.slice/session.scope\n"
            if path == Path("/proc/self/mountinfo"):
                return "36 25 0:32 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n"
            assert path == Path("/sys/fs/cgroup/user.slice/session.scope/memory.oom.group")
            return "1\n"

        monkeypatch.setattr(oom, "_read_text", read_text)

        assert oom._memory_oom_group_is_armed() is True
        assert reads.count(Path("/sys/fs/cgroup/user.slice/session.scope/memory.oom.group")) == 1

    def test_oom_score_is_written_and_verified(self, monkeypatch):
        writes = []
        monkeypatch.setattr(oom, "_write_text", lambda path, value: writes.append((path, value)))
        monkeypatch.setattr(oom, "_read_text", lambda path: "500\n")

        assert oom._set_oom_score() is True
        assert writes == [(Path("/proc/self/oom_score_adj"), "500")]

    def test_capability_requires_group_and_score(self, monkeypatch):
        monkeypatch.setattr(oom, "is_linux", lambda: True)
        monkeypatch.setattr(oom, "_memory_oom_group_is_armed", lambda: True)
        monkeypatch.setattr(oom, "_set_oom_score", lambda: True)
        assert REAL_ARM_WORKER(expect_scope=True) is oom.OomCapability.SUPPORTED

        monkeypatch.setattr(oom, "_memory_oom_group_is_armed", lambda: False)
        assert REAL_ARM_WORKER(expect_scope=True) is oom.OomCapability.DEGRADED_NO_CONTROLLER

        monkeypatch.setattr(oom, "_set_oom_score", lambda: False)
        assert REAL_ARM_WORKER(expect_scope=True) is oom.OomCapability.DEGRADED_NO_SCORE

    def test_supported_supervisor_reports_before_releasing(self, monkeypatch):
        generation = "a" * 32
        unit = oom.scope_unit_name("test-id", generation)
        events = []
        monkeypatch.setenv("HOPPER_RUN_GENERATION", generation)
        monkeypatch.setenv("HOPPER_OOM_SCOPE", unit)
        monkeypatch.setattr(oom, "is_linux", lambda: True)
        monkeypatch.setattr(oom, "find_scope_tools", lambda: ("systemd-run", "systemctl"))
        monkeypatch.setattr(oom, "find_hop_executable", lambda: "hop")
        monkeypatch.setattr(
            oom, "launch_scope", lambda argv: events.append(("launch", argv)) or 137
        )
        monkeypatch.setattr(
            oom,
            "read_scope_result",
            lambda systemctl, unit_name, *, timeout=oom.SYSTEMCTL_TIMEOUT_SEC: (
                events.append(("read", systemctl, unit_name)) or "oom-kill"
            ),
        )
        monkeypatch.setattr(
            "hopper.process.report_lode_run_result",
            lambda *args: events.append(("report", args)) or {"durable": True},
        )
        monkeypatch.setattr(
            oom,
            "release_scope",
            lambda systemctl, unit_name: events.append(("release", systemctl, unit_name)) or True,
        )

        assert run_process_supervisor("test-id", Path("server.sock")) == 0
        assert [event[0] for event in events] == ["launch", "read", "report", "release"]
        assert events[0][1] == oom.build_scope_argv("systemd-run", "hop", unit, "test-id")

    def test_failed_ack_retains_failed_unit_evidence(self, monkeypatch):
        generation = "b" * 32
        unit = oom.scope_unit_name("test-id", generation)
        monkeypatch.setenv("HOPPER_RUN_GENERATION", generation)
        monkeypatch.setenv("HOPPER_OOM_SCOPE", unit)
        monkeypatch.setattr(oom, "is_linux", lambda: True)
        monkeypatch.setattr(oom, "find_scope_tools", lambda: ("systemd-run", "systemctl"))
        monkeypatch.setattr(oom, "find_hop_executable", lambda: "hop")
        monkeypatch.setattr(oom, "launch_scope", lambda argv: 137)
        monkeypatch.setattr(
            oom,
            "read_scope_result",
            lambda systemctl, unit_name, *, timeout=oom.SYSTEMCTL_TIMEOUT_SEC: "oom-kill",
        )
        monkeypatch.setattr("hopper.process.report_lode_run_result", lambda *args: None)
        release = MagicMock()
        monkeypatch.setattr(oom, "release_scope", release)

        assert run_process_supervisor("test-id", Path("server.sock")) == 137
        release.assert_not_called()

    @pytest.mark.parametrize("unit_result", [None, "success"])
    def test_ordinary_exit_never_releases_scope(self, monkeypatch, unit_result):
        generation = "e" * 32
        unit = oom.scope_unit_name("test-id", generation)
        monkeypatch.setenv("HOPPER_RUN_GENERATION", generation)
        monkeypatch.setenv("HOPPER_OOM_SCOPE", unit)
        monkeypatch.setattr(oom, "is_linux", lambda: True)
        monkeypatch.setattr(oom, "find_scope_tools", lambda: ("systemd-run", "systemctl"))
        monkeypatch.setattr(oom, "find_hop_executable", lambda: "hop")
        monkeypatch.setattr(oom, "launch_scope", lambda argv: 0)
        monkeypatch.setattr(oom, "read_scope_result", lambda *args: unit_result)
        monkeypatch.setattr(
            "hopper.process.report_lode_run_result",
            lambda *args: {"durable": False, "disposition": "success"},
        )
        release = MagicMock()
        monkeypatch.setattr(oom, "release_scope", release)

        assert run_process_supervisor("test-id", Path("server.sock")) == 0
        release.assert_not_called()

    def test_durable_not_found_report_releases_failed_scope(self, monkeypatch):
        generation = "f" * 32
        unit = oom.scope_unit_name("archived-id", generation)
        monkeypatch.setenv("HOPPER_RUN_GENERATION", generation)
        monkeypatch.setenv("HOPPER_OOM_SCOPE", unit)
        monkeypatch.setattr(oom, "is_linux", lambda: True)
        monkeypatch.setattr(oom, "find_scope_tools", lambda: ("systemd-run", "systemctl"))
        monkeypatch.setattr(oom, "find_hop_executable", lambda: "hop")
        monkeypatch.setattr(oom, "launch_scope", lambda argv: 1)
        monkeypatch.setattr(
            oom,
            "read_scope_result",
            lambda systemctl, unit_name, *, timeout=oom.SYSTEMCTL_TIMEOUT_SEC: "exit-code",
        )
        monkeypatch.setattr(
            "hopper.process.report_lode_run_result",
            lambda *args: {"durable": True, "disposition": "not-found"},
        )
        release = MagicMock(return_value=True)
        monkeypatch.setattr(oom, "release_scope", release)

        assert run_process_supervisor("archived-id", Path("server.sock")) == 1
        release.assert_called_once_with("systemctl", unit)

    @pytest.mark.parametrize("transient_result", ["success", None])
    def test_nonzero_exit_reports_delayed_oom_before_releasing(self, monkeypatch, transient_result):
        generation = "1" * 32
        unit = oom.scope_unit_name("test-id", generation)
        events = []
        clock = self._ScopeResultClock([(transient_result, 0.0), ("oom-kill", 0.0)])
        monkeypatch.setenv("HOPPER_RUN_GENERATION", generation)
        monkeypatch.setenv("HOPPER_OOM_SCOPE", unit)
        monkeypatch.setattr(oom, "is_linux", lambda: True)
        monkeypatch.setattr(oom, "find_scope_tools", lambda: ("systemd-run", "systemctl"))
        monkeypatch.setattr(oom, "find_hop_executable", lambda: "hop")
        monkeypatch.setattr(
            oom,
            "launch_scope",
            lambda argv: events.append(("launch", argv)) or 137,
        )
        monkeypatch.setattr(oom.time, "monotonic", clock.monotonic)
        monkeypatch.setattr(oom.time, "sleep", clock.sleep)

        def read_scope_result(systemctl, unit_name, *, timeout):
            events.append(("read", systemctl, unit_name))
            return clock.read_scope_result(systemctl, unit_name, timeout=timeout)

        monkeypatch.setattr(oom, "read_scope_result", read_scope_result)
        monkeypatch.setattr(
            "hopper.process.report_lode_run_result",
            lambda *args: events.append(("report", args)) or {"durable": True},
        )
        monkeypatch.setattr(
            oom,
            "release_scope",
            lambda systemctl, unit_name: events.append(("release", systemctl, unit_name)) or True,
        )

        assert run_process_supervisor("test-id", Path("server.sock")) == 0
        assert [event[0] for event in events] == [
            "launch",
            "read",
            "read",
            "report",
            "release",
        ]
        assert events[3][1][4:] == ("oom-kill", 137)
        assert events[4][1:] == ("systemctl", unit)
        assert clock.sleeps == pytest.approx([oom.SCOPE_RESULT_POLL_SEC])
        assert [call[3] for call in clock.read_calls] == pytest.approx(
            [oom.SYSTEMCTL_TIMEOUT_SEC, oom.SYSTEMCTL_TIMEOUT_SEC]
        )

    def test_nonzero_exit_reports_exit_code_without_delay(self, monkeypatch):
        generation = "2" * 32
        unit = oom.scope_unit_name("test-id", generation)
        clock = self._ScopeResultClock([("exit-code", 0.0)])
        monkeypatch.setenv("HOPPER_RUN_GENERATION", generation)
        monkeypatch.setenv("HOPPER_OOM_SCOPE", unit)
        monkeypatch.setattr(oom, "is_linux", lambda: True)
        monkeypatch.setattr(oom, "find_scope_tools", lambda: ("systemd-run", "systemctl"))
        monkeypatch.setattr(oom, "find_hop_executable", lambda: "hop")
        monkeypatch.setattr(oom, "launch_scope", lambda argv: 1)
        monkeypatch.setattr(oom.time, "monotonic", clock.monotonic)
        monkeypatch.setattr(oom.time, "sleep", clock.sleep)
        monkeypatch.setattr(oom, "read_scope_result", clock.read_scope_result)
        report = MagicMock(return_value={"durable": True})
        monkeypatch.setattr("hopper.process.report_lode_run_result", report)
        release = MagicMock(return_value=True)
        monkeypatch.setattr(oom, "release_scope", release)

        assert run_process_supervisor("test-id", Path("server.sock")) == 1
        assert clock.read_count == 1
        assert [call[3] for call in clock.read_calls] == pytest.approx([oom.SYSTEMCTL_TIMEOUT_SEC])
        assert clock.sleeps == []
        report.assert_called_once_with(
            Path("server.sock"),
            "test-id",
            generation,
            unit,
            "exit-code",
            1,
        )
        assert report.call_args.args[4] != "oom-kill"
        release.assert_called_once_with("systemctl", unit)

    @pytest.mark.parametrize("stable_result", ["success", None])
    def test_nonzero_exit_reports_stable_transient_result_at_deadline(
        self, monkeypatch, stable_result
    ):
        generation = "3" * 32
        unit = oom.scope_unit_name("test-id", generation)
        clock = self._ScopeResultClock([(stable_result, 0.0)])
        monkeypatch.setenv("HOPPER_RUN_GENERATION", generation)
        monkeypatch.setenv("HOPPER_OOM_SCOPE", unit)
        monkeypatch.setattr(oom, "is_linux", lambda: True)
        monkeypatch.setattr(oom, "find_scope_tools", lambda: ("systemd-run", "systemctl"))
        monkeypatch.setattr(oom, "find_hop_executable", lambda: "hop")
        monkeypatch.setattr(oom, "launch_scope", lambda argv: 137)
        monkeypatch.setattr(oom.time, "monotonic", clock.monotonic)
        monkeypatch.setattr(oom.time, "sleep", clock.sleep)
        monkeypatch.setattr(oom, "read_scope_result", clock.read_scope_result)
        report = MagicMock(return_value={"durable": stable_result == "success"})
        monkeypatch.setattr("hopper.process.report_lode_run_result", report)
        release = MagicMock()
        monkeypatch.setattr(oom, "release_scope", release)

        assert run_process_supervisor("test-id", Path("server.sock")) == 137
        report.assert_called_once_with(
            Path("server.sock"),
            "test-id",
            generation,
            unit,
            stable_result,
            137,
        )
        assert report.call_args.args[4] != "oom-kill"
        release.assert_not_called()
        assert clock.now == pytest.approx(oom.SCOPE_RESULT_SETTLE_SEC)
        assert all(call[2] < oom.SCOPE_RESULT_SETTLE_SEC for call in clock.read_calls)
        assert all(0 < call[3] <= oom.SYSTEMCTL_TIMEOUT_SEC for call in clock.read_calls)
        assert clock.sleeps
        assert all(sleep <= oom.SCOPE_RESULT_POLL_SEC for sleep in clock.sleeps)

    def test_delayed_scope_reads_never_exceed_remaining_budget(self, monkeypatch):
        clock = self._ScopeResultClock([(None, 1.0)])
        monkeypatch.setattr(oom.time, "monotonic", clock.monotonic)
        monkeypatch.setattr(oom.time, "sleep", clock.sleep)
        monkeypatch.setattr(oom, "read_scope_result", clock.read_scope_result)

        assert oom.settle_scope_result("systemctl", "hopper-test.scope") is None
        assert clock.read_count == 2
        assert [call[2] for call in clock.read_calls] == pytest.approx(
            [0.0, 1.0 + oom.SCOPE_RESULT_POLL_SEC]
        )
        assert [call[3] for call in clock.read_calls] == pytest.approx(
            [
                1.0,
                oom.SCOPE_RESULT_SETTLE_SEC - 1.0 - oom.SCOPE_RESULT_POLL_SEC,
            ]
        )
        assert clock.now == pytest.approx(oom.SCOPE_RESULT_SETTLE_SEC)
        assert clock.sleeps == pytest.approx([oom.SCOPE_RESULT_POLL_SEC])
        final_start, final_timeout = clock.read_calls[-1][2:]
        assert final_start + final_timeout == pytest.approx(oom.SCOPE_RESULT_SETTLE_SEC)
        assert len(clock.sleeps) == clock.read_count - 1
        assert clock.now <= oom.SCOPE_RESULT_SETTLE_SEC
        for _, _, start, timeout in clock.read_calls:
            assert timeout > 0
            assert timeout == pytest.approx(
                min(
                    oom.SYSTEMCTL_TIMEOUT_SEC,
                    oom.SCOPE_RESULT_SETTLE_SEC - start,
                )
            )

    def test_zero_exit_reads_scope_once_without_delay(self, monkeypatch):
        generation = "4" * 32
        unit = oom.scope_unit_name("test-id", generation)
        monkeypatch.setenv("HOPPER_RUN_GENERATION", generation)
        monkeypatch.setenv("HOPPER_OOM_SCOPE", unit)
        monkeypatch.setattr(oom, "is_linux", lambda: True)
        monkeypatch.setattr(oom, "find_scope_tools", lambda: ("systemd-run", "systemctl"))
        monkeypatch.setattr(oom, "find_hop_executable", lambda: "hop")
        monkeypatch.setattr(oom, "launch_scope", lambda argv: 0)
        read_result = MagicMock(return_value="success")
        monkeypatch.setattr(oom, "read_scope_result", read_result)
        settle_result = MagicMock()
        monkeypatch.setattr(oom, "settle_scope_result", settle_result)
        monotonic = MagicMock()
        monkeypatch.setattr(oom.time, "monotonic", monotonic)
        sleep = MagicMock()
        monkeypatch.setattr(oom.time, "sleep", sleep)
        report = MagicMock(return_value={"durable": True})
        monkeypatch.setattr("hopper.process.report_lode_run_result", report)
        release = MagicMock()
        monkeypatch.setattr(oom, "release_scope", release)

        assert run_process_supervisor("test-id", Path("server.sock")) == 0
        read_result.assert_called_once_with("systemctl", unit)
        settle_result.assert_not_called()
        monotonic.assert_not_called()
        sleep.assert_not_called()
        report.assert_called_once_with(
            Path("server.sock"),
            "test-id",
            generation,
            unit,
            "success",
            0,
        )
        release.assert_not_called()

    def test_non_linux_supervisor_uses_inline_worker_without_probes(self, monkeypatch):
        monkeypatch.setattr(oom, "is_linux", lambda: False)
        monkeypatch.setattr(oom, "find_scope_tools", MagicMock(side_effect=AssertionError))
        monkeypatch.setattr(oom, "find_hop_executable", MagicMock(side_effect=AssertionError))
        inline = MagicMock(return_value=7)
        monkeypatch.setattr("hopper.process.run_process", inline)

        assert run_process_supervisor("test-id", Path("server.sock")) == 7
        inline.assert_called_once_with("test-id", Path("server.sock"), expect_scope=False)

    def test_degraded_warning_is_printed_and_logged_once(self, monkeypatch, capsys, caplog):
        monkeypatch.setattr(
            oom,
            "arm_worker",
            lambda **kwargs: oom.OomCapability.DEGRADED_NO_CONTROLLER,
        )
        monkeypatch.setattr(
            "hopper.client.connect", lambda *args, **kwargs: {"lode": {"stage": "mill"}}
        )
        monkeypatch.setattr(ProcessRunner, "run", lambda self: 0)

        with caplog.at_level(logging.WARNING, logger="hopper.process"):
            assert run_process("test-id", Path("server.sock")) == 0

        assert capsys.readouterr().out.count(oom.OOM_DEGRADED_WARNING) == 1
        assert caplog.messages.count(oom.OOM_DEGRADED_WARNING) == 1

    def test_read_scope_result_passes_bounded_timeout(self):
        """The systemctl show probe uses the shared bounded timeout."""
        unit = oom.scope_unit_name("test-id", "a" * 32)
        result = subprocess.CompletedProcess([], 0, stdout="oom-kill\n", stderr="")

        with patch("hopper.oom.subprocess.run", return_value=result) as mock_run:
            assert oom.read_scope_result("systemctl", unit) == "oom-kill"

        mock_run.assert_called_once_with(
            [
                "systemctl",
                "--user",
                "show",
                unit,
                "--property=Result",
                "--value",
            ],
            capture_output=True,
            text=True,
            timeout=oom.SYSTEMCTL_TIMEOUT_SEC,
        )

    def test_release_scope_passes_bounded_timeout(self):
        """The systemctl reset-failed probe uses the shared bounded timeout."""
        unit = oom.scope_unit_name("test-id", "b" * 32)
        result = subprocess.CompletedProcess([], 0, stdout="", stderr="")

        with patch("hopper.oom.subprocess.run", return_value=result) as mock_run:
            assert oom.release_scope("systemctl", unit) is True

        mock_run.assert_called_once_with(
            ["systemctl", "--user", "reset-failed", unit],
            capture_output=True,
            text=True,
            timeout=oom.SYSTEMCTL_TIMEOUT_SEC,
        )

    def test_read_scope_result_timeout_returns_none(self):
        """A wedged systemctl show probe degrades to an unreadable result."""
        unit = oom.scope_unit_name("test-id", "c" * 32)
        error = subprocess.TimeoutExpired("systemctl", oom.SYSTEMCTL_TIMEOUT_SEC)

        with patch("hopper.oom.subprocess.run", side_effect=error):
            assert oom.read_scope_result("systemctl", unit) is None

    def test_release_scope_timeout_returns_false(self):
        """A wedged reset-failed probe leaves the failed scope retained."""
        unit = oom.scope_unit_name("test-id", "d" * 32)
        error = subprocess.TimeoutExpired("systemctl", oom.SYSTEMCTL_TIMEOUT_SEC)

        with patch("hopper.oom.subprocess.run", side_effect=error):
            assert oom.release_scope("systemctl", unit) is False

    def test_launch_scope_remains_unbounded(self):
        """The worker-owning systemd scope remains deliberately unbounded."""
        unit = oom.scope_unit_name("test-id", "e" * 32)
        argv = oom.build_scope_argv("systemd-run", "hop", unit, "test-id")
        result = subprocess.CompletedProcess(argv, 137)

        with patch("hopper.oom.subprocess.run", return_value=result) as mock_run:
            assert oom.launch_scope(argv) == 137

        mock_run.assert_called_once_with(argv)
        assert "timeout" not in mock_run.call_args.kwargs


class TestArmedRegistration:
    def test_registration_ack_precedes_setup_and_model_launch(self):
        events = []
        generation = "c" * 32
        runner = ProcessRunner(
            "test-id",
            Path("server.sock"),
            "mill",
            run_generation=generation,
            oom_capability=oom.OomCapability.SUPPORTED,
            actual_unit="hopper.scope",
        )
        connection = MagicMock()

        def emit(msg_type, **fields):
            events.append((msg_type, fields))
            return True

        def start(callback=None, on_connect=None):
            on_connect()
            callback({"type": "lode_registered", "lode_id": "test-id"})

        connection.emit.side_effect = emit
        connection.start.side_effect = start
        with (
            patch("hopper.runner.connect", return_value=_mock_response()),
            patch("hopper.runner.HopperConnection", return_value=connection),
            patch.object(runner, "_setup", side_effect=lambda: events.append(("setup", {}))),
            patch.object(
                runner,
                "_run_claude",
                side_effect=lambda: events.append(("model", {})) or (0, None),
            ),
        ):
            assert runner.run() == 0

        assert [event[0] for event in events[:3]] == ["lode_register", "setup", "model"]
        assert events[0][1]["armed_mode"] == "supported"
        assert events[0][1]["actual_unit"] == "hopper.scope"

    def test_registration_refusal_launches_no_setup_or_model(self):
        generation = "d" * 32
        runner = ProcessRunner(
            "test-id",
            Path("server.sock"),
            "mill",
            run_generation=generation,
            oom_capability=oom.OomCapability.DEGRADED_NO_SCORE,
        )
        connection = MagicMock()

        def start(callback=None, on_connect=None):
            on_connect()
            callback({"type": "lode_register_refused", "lode_id": "test-id"})

        connection.emit.return_value = True
        connection.start.side_effect = start
        with (
            patch("hopper.runner.connect", return_value=_mock_response()),
            patch("hopper.runner.HopperConnection", return_value=connection),
            patch.object(runner, "_setup") as setup,
            patch.object(runner, "_run_claude") as run_model,
        ):
            assert runner.run() == 1

        setup.assert_not_called()
        run_model.assert_not_called()
