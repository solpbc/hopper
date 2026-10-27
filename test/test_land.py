# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for feature landing and pre-land command configuration."""

import subprocess

import pytest

from hopper import config
from hopper.cli import cmd_config, cmd_land, cmd_preland
from hopper.config import config_transaction, load_config
from hopper.land import PRELAND_CONFIG_PREFIX
from hopper.projects import (
    disable_project,
    enable_project,
    rename_project,
    touch_project,
)


class FakeSubprocessRunner:
    """Mock subprocess runner for git and preland commands."""

    def __init__(
        self,
        branch: str = "main",
        remotes: tuple[str, ...] = ("origin",),
        pre_sha: str = "sha_pre123",
        post_sha: str = "sha_post456",
    ):
        self.branch = branch
        self.remotes = list(remotes)
        self.pre_sha = pre_sha
        self.post_sha = post_sha
        self.merge_code = 0
        self.push_code = 0
        self.remote_code = 0
        self.preland_code = 0
        self.preland_output = "preland test output\n"
        self.preland_exc: Exception | None = None
        self.calls: list[tuple[list[str], str | None]] = []
        self.merged = False
        self.pushed = False
        self.fail_head_after_merge = False
        self.fail_head_after_push = False

    def run(self, cmd, *args, **kwargs):
        cmd_list = list(cmd)
        cwd = kwargs.get("cwd")
        self.calls.append((cmd_list, cwd))

        if len(cmd_list) >= 3 and cmd_list[0] == "git" and cmd_list[1] == "-C":
            git_args = cmd_list[3:]
            if git_args == ["rev-parse", "--abbrev-ref", "HEAD"]:
                return subprocess.CompletedProcess(
                    cmd_list, 0, stdout=f"{self.branch}\n", stderr=""
                )
            if git_args == ["rev-parse", "HEAD"]:
                if (self.merged and self.fail_head_after_merge) or (
                    self.pushed and self.fail_head_after_push
                ):
                    return subprocess.CompletedProcess(
                        cmd_list, 128, stdout="", stderr="HEAD unreadable"
                    )
                sha = self.post_sha if self.merged else self.pre_sha
                return subprocess.CompletedProcess(cmd_list, 0, stdout=f"{sha}\n", stderr="")
            if git_args[:2] == ["merge", "--ff-only"]:
                if self.merge_code == 0:
                    self.merged = True
                return subprocess.CompletedProcess(
                    cmd_list,
                    self.merge_code,
                    stdout="",
                    stderr="merge failure" if self.merge_code else "",
                )
            if git_args == ["remote"]:
                out = "\n".join(self.remotes) + ("\n" if self.remotes else "")
                return subprocess.CompletedProcess(
                    cmd_list,
                    self.remote_code,
                    stdout=out,
                    stderr="remote failure" if self.remote_code else "",
                )
            if git_args == ["push"]:
                self.pushed = True
                return subprocess.CompletedProcess(
                    cmd_list,
                    self.push_code,
                    stdout="",
                    stderr="push failure" if self.push_code else "",
                )
            raise AssertionError(f"unexpected git invocation: {cmd_list}")

        # Pre-land command invocation
        if self.preland_exc is not None:
            raise self.preland_exc

        return subprocess.CompletedProcess(
            cmd_list,
            self.preland_code,
            stdout=self.preland_output,
            stderr="",
        )


def _setup_project(name: str = "projA", path: str = "/repo/projA"):
    with config_transaction() as cfg:
        projects = cfg.setdefault("projects", [])
        projects.append(
            {
                "path": path,
                "name": name,
                "disabled": False,
                "disabled_reason": "",
                "last_used_at": 1000,
            }
        )


def test_land_unset_preland_success(capsys, monkeypatch):
    """Unset pre-land reaches merge --ff-only and push when origin exists."""
    _setup_project("projA")
    tracker = FakeSubprocessRunner(branch="main", remotes=("origin",))
    monkeypatch.setattr(subprocess, "run", tracker.run)

    res = cmd_land(
        [
            "--project",
            "projA",
            "--checkout",
            "/repo/projA",
            "--worktree",
            "/worktree/feat",
            "--branch",
            "feat-abc12345",
        ]
    )

    assert res == 0
    captured = capsys.readouterr()
    assert "fast-forwarded" in captured.out
    assert "push: ok" in captured.out

    # Verify git invocations
    commands = [c[0] for c in tracker.calls]
    assert ["git", "-C", "/repo/projA", "merge", "--ff-only", "feat-abc12345"] in commands
    assert ["git", "-C", "/repo/projA", "push"] in commands


def test_land_successful_preland_runs_in_worktree_cwd(capsys, monkeypatch, tmp_path):
    """Successful pre-land runs in candidate worktree cwd before merge and push."""
    _setup_project("projA")
    with config_transaction() as cfg:
        cfg[f"{PRELAND_CONFIG_PREFIX}projA"] = "make preland"

    tracker = FakeSubprocessRunner(branch="main", remotes=("origin",))
    monkeypatch.setattr(subprocess, "run", tracker.run)

    # Change current working directory to somewhere unrelated
    monkeypatch.chdir(tmp_path)

    res = cmd_land(
        [
            "--project",
            "projA",
            "--checkout",
            "/repo/projA",
            "--worktree",
            "/custom/worktree",
            "--branch",
            "feat-abc12345",
        ]
    )

    assert res == 0
    captured = capsys.readouterr()
    assert "fast-forwarded" in captured.out
    assert "push: ok" in captured.out

    # Preland command was executed with cwd set to candidate worktree
    preland_calls = [c for c in tracker.calls if c[0] == ["make", "preland"]]
    assert len(preland_calls) == 1
    assert preland_calls[0][1] == "/custom/worktree"

    # Merge and push were both executed
    commands = [c[0] for c in tracker.calls]
    assert ["git", "-C", "/repo/projA", "merge", "--ff-only", "feat-abc12345"] in commands
    assert ["git", "-C", "/repo/projA", "push"] in commands


def test_land_failing_preland_command_stops_without_merge_or_push(capsys, monkeypatch):
    """Failing pre-land command stops, does not merge or push, and outputs failure block."""
    _setup_project("projA")
    with config_transaction() as cfg:
        cfg[f"{PRELAND_CONFIG_PREFIX}projA"] = "npm test"

    tracker = FakeSubprocessRunner()
    tracker.preland_code = 42
    tracker.preland_output = "1 test failed\n"
    monkeypatch.setattr(subprocess, "run", tracker.run)

    res = cmd_land(
        [
            "--project",
            "projA",
            "--checkout",
            "/repo/projA",
            "--worktree",
            "/worktrees/feat1",
            "--branch",
            "feat-12345678",
        ]
    )

    assert res == 42
    captured = capsys.readouterr()
    assert "error: pre-land command failed" in captured.out
    assert "command: npm test" in captured.out
    assert "worktree: /worktrees/feat1" in captured.out
    assert "status: 42" in captured.out
    assert "1 test failed" in captured.out
    assert "Hopper did not merge and did not push." in captured.out
    assert "recover with: hop preland set projA <command>" in captured.out
    assert "recover with: hop preland clear projA" in captured.out

    # No merge or push calls
    commands = [c[0] for c in tracker.calls]
    assert not any("merge" in c for c in commands)
    assert not any("push" in c for c in commands)


def test_land_unstartable_preland_command_exits_127(capsys, monkeypatch):
    """Unstartable pre-land command (FileNotFoundError) exits 127 without merge or push."""
    _setup_project("projA")
    with config_transaction() as cfg:
        cfg[f"{PRELAND_CONFIG_PREFIX}projA"] = "nonexistent_binary"

    tracker = FakeSubprocessRunner()
    tracker.preland_exc = FileNotFoundError("No such file or directory: 'nonexistent_binary'")
    monkeypatch.setattr(subprocess, "run", tracker.run)

    res = cmd_land(
        [
            "--project",
            "projA",
            "--checkout",
            "/repo/projA",
            "--worktree",
            "/worktrees/feat1",
            "--branch",
            "feat-12345678",
        ]
    )

    assert res == 127
    captured = capsys.readouterr()
    assert "error: pre-land command failed" in captured.out
    assert "command: nonexistent_binary" in captured.out
    assert "status: 127" in captured.out
    assert "recover with: hop preland set projA <command>" in captured.out
    assert "recover with: hop preland clear projA" in captured.out

    commands = [c[0] for c in tracker.calls]
    assert not any("merge" in c for c in commands)
    assert not any("push" in c for c in commands)


@pytest.mark.parametrize(
    "branch_name,expected_label", [("topic", "topic"), ("HEAD", "detached HEAD")]
)
def test_land_checkout_not_main_or_master_refuses(capsys, monkeypatch, branch_name, expected_label):
    """Checkout on topic or detached refuses with no pre-land, no merge, no push."""
    _setup_project("projA")
    with config_transaction() as cfg:
        cfg[f"{PRELAND_CONFIG_PREFIX}projA"] = "make test"

    tracker = FakeSubprocessRunner(branch=branch_name)
    monkeypatch.setattr(subprocess, "run", tracker.run)

    res = cmd_land(
        [
            "--project",
            "projA",
            "--checkout",
            "/repo/projA",
            "--worktree",
            "/worktrees/feat1",
            "--branch",
            "feat-12345678",
        ]
    )

    assert res == 1
    captured = capsys.readouterr()
    assert "error: landing refused" in captured.out
    assert f"observed: /repo/projA is on '{expected_label}', not main or master" in captured.out
    assert (
        "Hopper did not run the pre-land command, did not merge, and did not push." in captured.out
    )

    # No pre-land, no merge, no push, no checkout argv
    commands = [c[0] for c in tracker.calls]
    assert ["make", "test"] not in commands
    assert not any("merge" in c for c in commands)
    assert not any("push" in c for c in commands)
    assert not any("checkout" in c for c in commands)


def test_land_accepts_master_branch(capsys, monkeypatch):
    """Master branch is accepted and lands."""
    _setup_project("projA")
    tracker = FakeSubprocessRunner(branch="master", remotes=("origin",))
    monkeypatch.setattr(subprocess, "run", tracker.run)

    res = cmd_land(
        [
            "--project",
            "projA",
            "--checkout",
            "/repo/projA",
            "--worktree",
            "/worktree/feat",
            "--branch",
            "feat-abc12345",
        ]
    )

    assert res == 0
    captured = capsys.readouterr()
    assert "push: ok" in captured.out


def test_land_no_origin_merges_without_push(capsys, monkeypatch):
    """No origin remote: merges, does not push, and reports push: not-run."""
    _setup_project("projA")
    tracker = FakeSubprocessRunner(branch="main", remotes=())
    monkeypatch.setattr(subprocess, "run", tracker.run)

    res = cmd_land(
        [
            "--project",
            "projA",
            "--checkout",
            "/repo/projA",
            "--worktree",
            "/worktree/feat",
            "--branch",
            "feat-abc12345",
        ]
    )

    assert res == 0
    captured = capsys.readouterr()
    assert "fast-forwarded" in captured.out
    assert "push: not-run" in captured.out

    commands = [c[0] for c in tracker.calls]
    assert ["git", "-C", "/repo/projA", "merge", "--ff-only", "feat-abc12345"] in commands
    assert not any("push" in c for c in commands)


def test_land_failed_merge_does_not_push_and_reports_local_moved_no(capsys, monkeypatch):
    """Failed merge does not push and reports push: not-run, local-moved: no."""
    _setup_project("projA")
    tracker = FakeSubprocessRunner(branch="main", remotes=("origin",), pre_sha="sha_pre111")
    tracker.merge_code = 1
    monkeypatch.setattr(subprocess, "run", tracker.run)

    res = cmd_land(
        [
            "--project",
            "projA",
            "--checkout",
            "/repo/projA",
            "--worktree",
            "/worktree/feat",
            "--branch",
            "feat-abc12345",
        ]
    )

    assert res == 1
    captured = capsys.readouterr()
    assert "error: merge failed" in captured.out
    assert "push: not-run" in captured.out
    assert "local-moved: no" in captured.out
    assert "sha_pre111" in captured.out
    assert "merge failure" in captured.out

    commands = [c[0] for c in tracker.calls]
    assert not any("push" in c for c in commands)


def test_land_failed_push_reports_both_shas_without_reset(capsys, monkeypatch):
    """Failed push prints push: failed, local-moved, both SHAs, and does not reset."""
    _setup_project("projA")
    tracker = FakeSubprocessRunner(
        branch="main",
        remotes=("origin",),
        pre_sha="sha_pre111",
        post_sha="sha_post222",
    )
    tracker.push_code = 1
    monkeypatch.setattr(subprocess, "run", tracker.run)

    res = cmd_land(
        [
            "--project",
            "projA",
            "--checkout",
            "/repo/projA",
            "--worktree",
            "/worktree/feat",
            "--branch",
            "feat-abc12345",
        ]
    )

    assert res == 1
    captured = capsys.readouterr()
    assert "error: push failed" in captured.out
    assert "push: failed" in captured.out
    assert "local-moved: yes" in captured.out
    assert "pre-merge HEAD: sha_pre111" in captured.out
    assert "current HEAD: sha_post222" in captured.out
    assert "push failure" in captured.out

    commands = [c[0] for c in tracker.calls]
    assert not any("reset" in c for c in commands)


def test_second_cmd_land_runs_preland_command_again(capsys, monkeypatch):
    """A second cmd_land call runs the pre-land command again."""
    _setup_project("projA")
    with config_transaction() as cfg:
        cfg[f"{PRELAND_CONFIG_PREFIX}projA"] = "make test"

    tracker = FakeSubprocessRunner(branch="main", remotes=())
    monkeypatch.setattr(subprocess, "run", tracker.run)

    args = [
        "--project",
        "projA",
        "--checkout",
        "/repo/projA",
        "--worktree",
        "/worktree/feat",
        "--branch",
        "feat-abc12345",
    ]

    assert cmd_land(args) == 0
    assert cmd_land(args) == 0

    preland_calls = [c for c in tracker.calls if c[0] == ["make", "test"]]
    assert len(preland_calls) == 2


@pytest.mark.parametrize(
    "malformed_val",
    [
        123,
        {"cmd": "make"},
        "",
        "   ",
        "make 'unclosed_quote",
    ],
)
def test_malformed_preland_config_refuses(capsys, monkeypatch, malformed_val):
    """Malformed config value refuses with exact output and no git or preland execution."""
    _setup_project("projA")
    with config_transaction() as cfg:
        cfg[f"{PRELAND_CONFIG_PREFIX}projA"] = malformed_val

    tracker = FakeSubprocessRunner()
    monkeypatch.setattr(subprocess, "run", tracker.run)

    res = cmd_land(
        [
            "--project",
            "projA",
            "--checkout",
            "/repo/projA",
            "--worktree",
            "/worktree/feat",
            "--branch",
            "feat-abc12345",
        ]
    )

    assert res == 1
    captured = capsys.readouterr()
    assert "error: pre-land command refused" in captured.out
    assert (
        f"config key 'preland.projA' in {config.config_path()} is {malformed_val!r}" in captured.out
    )
    assert "Hopper did not run a pre-land command, did not merge, and did not push." in captured.out
    assert "recover with: hop preland set projA <command>" in captured.out
    assert "recover with: hop preland clear projA" in captured.out
    assert len(tracker.calls) == 0


def test_project_a_preland_not_used_for_project_b(capsys, monkeypatch):
    """Project A's pre-land command is not invoked when landing Project B."""
    _setup_project("projA")
    _setup_project("projB")
    with config_transaction() as cfg:
        cfg[f"{PRELAND_CONFIG_PREFIX}projA"] = "make preland_for_a"

    tracker = FakeSubprocessRunner(branch="main", remotes=())
    monkeypatch.setattr(subprocess, "run", tracker.run)

    res = cmd_land(
        [
            "--project",
            "projB",
            "--checkout",
            "/repo/projB",
            "--worktree",
            "/worktree/featB",
            "--branch",
            "feat-abc12345",
        ]
    )

    assert res == 0
    commands = [c[0] for c in tracker.calls]
    assert not any("preland_for_a" in c for c in commands)


def test_preland_survives_project_lifecycle_and_renames(monkeypatch):
    """Pre-land command survives touch, disable, enable, rename, and unrelated config set."""
    _setup_project("projA")
    with config_transaction() as cfg:
        cfg[f"{PRELAND_CONFIG_PREFIX}projA"] = "make test"

    # touch_project
    touch_project("projA")
    assert load_config()[f"{PRELAND_CONFIG_PREFIX}projA"] == "make test"

    # disable_project
    disable_project("projA")
    assert load_config()[f"{PRELAND_CONFIG_PREFIX}projA"] == "make test"

    # enable_project
    enable_project("projA")
    assert load_config()[f"{PRELAND_CONFIG_PREFIX}projA"] == "make test"

    # unrelated hop config set
    assert cmd_config(["set", "unrelated_key", "value"]) == 0
    assert load_config()[f"{PRELAND_CONFIG_PREFIX}projA"] == "make test"

    # rename_project
    rename_project("projA", "projRenamed")
    cfg_after = load_config()
    assert f"{PRELAND_CONFIG_PREFIX}projA" not in cfg_after
    assert cfg_after[f"{PRELAND_CONFIG_PREFIX}projRenamed"] == "make test"


def test_hop_config_set_and_delete_refuses_preland_prefix(capsys):
    """hop config set and delete refuse keys starting with preland."""
    _setup_project("projA")

    # hop config set preland.projA
    assert cmd_config(["set", "preland.projA", "make test"]) == 1
    captured = capsys.readouterr()
    assert "error: config mutation refused" in captured.out
    assert "observed: 'preland.projA' is reserved for pre-land commands" in captured.out
    assert "Hopper did not change config.json." in captured.out
    assert "recover with: hop preland set projA <command>" in captured.out
    assert f"{PRELAND_CONFIG_PREFIX}projA" not in load_config()

    # hop config delete preland.projA
    assert cmd_config(["delete", "preland.projA"]) == 1
    captured = capsys.readouterr()
    assert "error: config mutation refused" in captured.out
    assert "observed: 'preland.projA' is reserved for pre-land commands" in captured.out
    assert "Hopper did not change config.json." in captured.out
    assert "recover with: hop preland clear projA" in captured.out


def test_hop_preland_cli_set_and_clear(capsys):
    """hop preland set and clear CLI commands function as expected."""
    _setup_project("projA")

    # Set command with leading -- stripped
    assert cmd_preland(["set", "projA", "--", "make", "check", "-j4"]) == 0
    captured = capsys.readouterr()
    assert "preland.projA=make check -j4" in captured.out
    assert load_config()["preland.projA"] == "make check -j4"

    # Refuse unknown project
    assert cmd_preland(["set", "unknownProj", "make", "test"]) == 1
    captured = capsys.readouterr()
    assert "error: project 'unknownProj' not found" in captured.out

    # Refuse empty command
    assert cmd_preland(["set", "projA", "--"]) == 1
    captured = capsys.readouterr()
    assert "error: command required" in captured.out

    # Clear existing command
    assert cmd_preland(["clear", "projA"]) == 0
    captured = capsys.readouterr()
    assert "Cleared 'preland.projA'." in captured.out
    assert "preland.projA" not in load_config()

    # Clear non-existent command refuses
    assert cmd_preland(["clear", "projA"]) == 1
    captured = capsys.readouterr()
    assert "error: config deletion refused" in captured.out
    assert "observed: config key 'preland.projA' is not set" in captured.out
    assert "Hopper did not change config.json." in captured.out
    assert "recover with: hop preland set projA <command>" in captured.out


@pytest.mark.parametrize("remotes", [("origin",), ()])
def test_unreadable_head_after_merge_never_reports_success_or_pushes(monkeypatch, capsys, remotes):
    tracker = FakeSubprocessRunner(remotes=remotes)
    tracker.fail_head_after_merge = True
    monkeypatch.setattr(subprocess, "run", tracker.run)
    assert (
        cmd_land(
            [
                "--project",
                "projA",
                "--checkout",
                "/repo/projA",
                "--worktree",
                "/worktree/feat",
                "--branch",
                "feat-abc12345",
            ]
        )
        != 0
    )
    output = capsys.readouterr().out
    assert "HEAD unreadable" in output
    assert "local-moved: unknown" in output
    assert "fast-forwarded" not in output
    assert not tracker.pushed


def test_failed_push_does_not_substitute_an_old_head_for_an_unreadable_one(monkeypatch, capsys):
    tracker = FakeSubprocessRunner()
    tracker.push_code = 1
    tracker.fail_head_after_push = True
    monkeypatch.setattr(subprocess, "run", tracker.run)
    assert (
        cmd_land(
            [
                "--project",
                "projA",
                "--checkout",
                "/repo/projA",
                "--worktree",
                "/worktree/feat",
                "--branch",
                "feat-abc12345",
            ]
        )
        != 0
    )
    output = capsys.readouterr().out
    assert "current HEAD: unknown" in output
    assert "local-moved: unknown" in output
    assert "push failure" in output
