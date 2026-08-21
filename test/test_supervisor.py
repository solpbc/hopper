# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Focused contracts for selectable interactive supervisors."""

import copy
import json
import subprocess
import uuid
from unittest.mock import MagicMock, patch

import pytest

from hopper import claude, codex, grok
from hopper.cli import _extract_create_supervisor, cmd_supervisor
from hopper.driver import RUNNABLE_STAGE_DRIVERS, resolve_driver
from hopper.lodes import (
    bind_lode_stage_session,
    create_lode,
    lode_stage_session,
    stage_launch_id,
)
from hopper.runner import BaseRunner, StageDriverProtocol
from hopper.server import _attempt_character_delivery, _attempt_pane_delivery
from hopper.supervisor import (
    SupervisorDefaultRefusal,
    resolve_supervisor_default,
    set_supervisor_default,
    supervisor_check,
    validate_supervisor_provider,
)
from hopper.tmux import KeyboardOwnership, PanePhase

CODEX_SESSION_ID = "11111111-1111-4111-8111-111111111111"

CODEX_IDLE = """
› Ask Codex to do anything

  gpt-5.6-terra xhigh · /repo
"""
CODEX_STAGED = """
› Please inspect the failing test

  gpt-5.6-terra xhigh · /repo
"""
CODEX_BUSY = """
• Working (0s • esc to interrupt)

› Ask Codex to do anything
  gpt-5.6-terra xhigh · /repo
"""
CODEX_WAIT = """
Our systems are thinking a bit more about this request before responding.
› 1. Retry with a faster model
2. Dismiss and keep waiting
3. Learn more
No action is required. Codex will keep waiting, and this menu will close when the response is ready.
"""

GROK_IDLE = """
╭──────────────────────────────────────╮
│ ❯                                    │
╰──────────────── Grok 4.6 (high) ─────╯
Shift+Tab:mode  │  Ctrl+x:shortcuts
"""
GROK_STAGED = """
╭──────────────────────────────────────╮
│ ❯ Please inspect the failing test    │
╰──────────────── Grok 4.6 (high) ─────╯
Shift+Tab:mode  │  Ctrl+x:shortcuts
"""
GROK_BUSY = """
⠙ Waiting for response… 0.2s  0.2s ⇣14.6k [stop]
╭──────────────────────────────────────╮
│ ❯                                    │
╰──────────────── Grok 4.6 (high) ─────╯
Shift+Tab:mode  │  Esc:cancel  │  Ctrl+x:shortcuts
"""
GROK_BACKGROUND = """
◎ 1 command still running
╭──────────────────────────────────────╮
│ ❯                                    │
╰──────────────── Grok 4.6 (high) ─────╯
Shift+Tab:mode  │  Ctrl+x:shortcuts
"""
GROK_CARD = """
◆ Waiting on plan approval
a:approve │ q:quit plan
"""


def _ansi(text: str) -> str:
    return f"\x1b[2m{text}\x1b[0m"


def test_registry_resolves_exact_three_supervisors():
    assert RUNNABLE_STAGE_DRIVERS == ("claude", "codex", "grok")
    assert [resolve_driver(name) for name in RUNNABLE_STAGE_DRIVERS] == [claude, codex, grok]


def test_supervisor_default_round_trip_and_validation():
    assert resolve_supervisor_default() == ("claude", "built in")
    set_supervisor_default("grok")
    assert resolve_supervisor_default() == ("grok", "saved")
    assert validate_supervisor_provider("codex") == "codex"
    with pytest.raises(SupervisorDefaultRefusal):
        set_supervisor_default("other")
    with pytest.raises(ValueError, match="claude, codex, grok"):
        validate_supervisor_provider("other")


def test_supervisor_cli_default_and_readiness_json(capsys):
    assert cmd_supervisor(["default"]) == 0
    assert capsys.readouterr().out == "claude (built in)\n"
    assert cmd_supervisor(["default", "grok"]) == 0
    assert resolve_supervisor_default() == ("grok", "saved")
    capsys.readouterr()
    readiness = {"provider": "grok", "ready": True, "version": "1.0.4", "error": ""}
    with patch("hopper.cli.supervisor_check", return_value=readiness):
        assert cmd_supervisor(["check", "grok", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == readiness


def test_explicit_create_supervisor_does_not_consult_saved_default():
    with patch(
        "hopper.cli.resolve_supervisor_default",
        side_effect=AssertionError("saved preference was consulted"),
    ):
        assert (
            _extract_create_supervisor("implement", ["project", "--supervisor", "grok"]) == "grok"
        )
        assert _extract_create_supervisor("implement", ["project", "--supervisor=codex"]) == "codex"


def test_claude_supervisor_check_uses_version_command(monkeypatch):
    monkeypatch.setattr("hopper.supervisor.shutil.which", lambda _name: "/bin/claude")
    result = subprocess.CompletedProcess([], 0, stdout="2.1.238 (Claude Code)\n", stderr="")
    with patch("hopper.supervisor.subprocess.run", return_value=result) as run:
        assert supervisor_check("claude") == {
            "provider": "claude",
            "ready": True,
            "version": "2.1.238 (Claude Code)",
            "error": "",
        }
    assert run.call_args.args[0] == ["claude", "--version"]


def test_supervisor_commands_are_interactive_and_exact():
    claude_cmd = claude.build_command(session_id=CODEX_SESSION_ID, prompt="scope", resume=False)
    assert claude_cmd == [
        "claude",
        "--dangerously-skip-permissions",
        "--disallowed-tools=AskUserQuestion",
        "--session-id",
        CODEX_SESSION_ID,
        "scope",
    ]

    codex_cmd = codex.build_command(session_id=CODEX_SESSION_ID, prompt="scope", resume=False)
    assert codex_cmd == [
        "codex",
        "resume",
        CODEX_SESSION_ID,
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
        "-c",
        "check_for_update_on_startup=false",
        "scope",
    ]
    assert "exec" not in codex_cmd and "--json" not in codex_cmd
    assert (
        codex.build_command(session_id=CODEX_SESSION_ID, prompt=None, resume=True) == codex_cmd[:-1]
    )

    grok_cmd = grok.build_command(session_id=CODEX_SESSION_ID, prompt="scope", resume=False)
    assert grok_cmd[0] == "grok"
    assert grok_cmd[-3:] == ["--session-id", CODEX_SESSION_ID, "scope"]
    assert "-p" not in grok_cmd and "--output-format" not in grok_cmd
    resume = grok.build_command(session_id=CODEX_SESSION_ID, prompt=None, resume=True)
    assert resume[-2:] == ["--resume", CODEX_SESSION_ID]
    for required in (
        "--fullscreen",
        "bypassPermissions",
        "off",
        "--no-memory",
        "--no-plan",
        "--no-subagents",
        "--disable-web-search",
    ):
        assert required in grok_cmd and required in resume


def _codex_proc(stdout: str, *, returncode: int = 0, stderr: str = "") -> MagicMock:
    proc = MagicMock(returncode=returncode)
    proc.communicate.return_value = (stdout, stderr)
    return proc


def test_codex_bootstrap_pins_terra_xhigh_and_returns_one_uuid():
    event = json.dumps({"type": "thread.started", "thread_id": CODEX_SESSION_ID})
    proc = _codex_proc(event)
    with patch("hopper.codex.subprocess.Popen", return_value=proc) as launch:
        assert codex.prepare_session(cwd="/repo", env={"PATH": "/bin"}) == (
            0,
            CODEX_SESSION_ID,
            None,
        )
    cmd = launch.call_args.args[0]
    assert cmd[:2] == ["codex", "exec"]
    assert "--ignore-user-config" in cmd
    assert "--ignore-rules" in cmd
    assert cmd[cmd.index("-s") + 1] == "read-only"
    assert cmd[cmd.index("-a") + 1] == "never"
    assert cmd[cmd.index("-m") + 1] == "gpt-5.6-terra"
    assert 'model_reasoning_effort="xhigh"' in cmd
    assert "--json" in cmd


@pytest.mark.parametrize(
    ("stdout", "returncode"),
    [
        ("", 0),
        (json.dumps({"type": "thread.started", "thread_id": "not-a-uuid"}), 0),
        (
            "\n".join(
                [
                    json.dumps({"type": "thread.started", "thread_id": CODEX_SESSION_ID}),
                    json.dumps({"type": "thread.started", "thread_id": str(uuid.uuid4())}),
                ]
            ),
            0,
        ),
        (
            "\n".join(
                [
                    json.dumps({"type": "thread.started", "thread_id": CODEX_SESSION_ID}),
                    json.dumps({"type": "turn.failed", "error": {"message": "quota unavailable"}}),
                ]
            ),
            0,
        ),
        (json.dumps({"type": "thread.started", "thread_id": CODEX_SESSION_ID}), 7),
    ],
)
def test_codex_bootstrap_rejects_ambiguous_or_failed_output(stdout, returncode):
    with patch(
        "hopper.codex.subprocess.Popen", return_value=_codex_proc(stdout, returncode=returncode)
    ):
        exit_code, thread_id, error = codex.prepare_session(cwd="/repo", env={})
    assert exit_code != 0
    assert thread_id is None
    assert error


def test_codex_bootstrap_interrupt_terminates_process_group():
    proc = MagicMock()
    proc.communicate.side_effect = KeyboardInterrupt
    with (
        patch("hopper.codex.subprocess.Popen", return_value=proc),
        patch("hopper.codex._terminate_process_group") as terminate,
    ):
        assert codex.prepare_session(cwd="/repo", env={}) == (130, None, None)
    terminate.assert_called_once_with(proc)


def test_codex_binding_replaces_provisional_pair_atomically(temp_config):
    lodes: list[dict] = []
    lode = create_lode(lodes, "project", "scope", driver="codex")
    lode["run_generation"] = "generation-1"
    session_before = copy.deepcopy(lode_stage_session(lode, "mill"))
    actual_launch_id = stage_launch_id(lode["id"], "mill", CODEX_SESSION_ID)

    bound, outcome = bind_lode_stage_session(
        lodes,
        lode["id"],
        driver="codex",
        stage="mill",
        launch_id=actual_launch_id,
        provider_session_id=CODEX_SESSION_ID,
        run_generation="generation-1",
    )

    assert outcome == "committed"
    session = lode_stage_session(bound, "mill")
    assert session["provider_session_id"] == CODEX_SESSION_ID
    assert session["launch_id"] == actual_launch_id
    assert session["started"] is True
    assert session["start_attempt"]["provider_session_id"] == CODEX_SESSION_ID
    assert session_before["provider_session_id"] != CODEX_SESSION_ID


def test_codex_conflicting_binding_leaves_pair_unchanged():
    lodes: list[dict] = []
    lode = create_lode(lodes, "project", "scope", driver="codex")
    before = copy.deepcopy(lode_stage_session(lode, "mill"))
    with pytest.raises(ValueError, match="provider session"):
        bind_lode_stage_session(
            lodes,
            lode["id"],
            driver="codex",
            stage="mill",
            launch_id=str(uuid.uuid4()),
            provider_session_id=CODEX_SESSION_ID,
            run_generation="generation-1",
        )
    assert lode_stage_session(lode, "mill") == before


@pytest.mark.parametrize("decorator", [lambda value: value, _ansi], ids=["plain", "ansi"])
def test_codex_pane_states_and_composer(decorator):
    assert codex.observe_pane(None, decorator(CODEX_IDLE)) == (
        PanePhase.IDLE,
        KeyboardOwnership.COMPOSER,
    )
    assert codex.read_pane_input(decorator(CODEX_STAGED)) == "Please inspect the failing test"
    assert codex.observe_pane(None, decorator(CODEX_BUSY))[0] is PanePhase.BUSY
    assert codex.observe_pane(None, decorator(CODEX_WAIT)) == (
        PanePhase.BLOCKED,
        KeyboardOwnership.CARD,
    )
    assert codex.observe_pane(None, decorator(CODEX_WAIT + CODEX_IDLE))[0] is PanePhase.IDLE
    assert codex.observe_pane(None, decorator("OpenAI Codex\n"))[0] is PanePhase.STARTING
    assert codex.observe_pane(None, decorator("please run `codex login`\n"))[0] is PanePhase.AUTH
    assert codex.observe_pane(None, decorator("unrecognized chrome\n"))[0] is PanePhase.UNKNOWN


@pytest.mark.parametrize("decorator", [lambda value: value, _ansi], ids=["plain", "ansi"])
def test_grok_pane_states_and_composer(decorator):
    assert grok.observe_pane(None, decorator(GROK_IDLE)) == (
        PanePhase.IDLE,
        KeyboardOwnership.COMPOSER,
    )
    assert grok.read_pane_input(decorator(GROK_STAGED)) == "Please inspect the failing test"
    assert grok.observe_pane(None, decorator(GROK_BUSY))[0] is PanePhase.BUSY
    assert grok.observe_pane(None, decorator(GROK_BACKGROUND))[0] is PanePhase.BACKGROUND
    assert grok.observe_pane(None, decorator(GROK_CARD)) == (
        PanePhase.BLOCKED,
        KeyboardOwnership.CARD,
    )
    assert grok.observe_pane(None, decorator("Grok Build starting\n"))[0] is PanePhase.STARTING
    auth = "Approve in your browser to finish signing in.\nWaiting for approval...\n"
    assert grok.observe_pane(None, decorator(auth))[0] is PanePhase.AUTH
    assert grok.observe_pane(None, decorator("unrecognized chrome\n"))[0] is PanePhase.UNKNOWN


@pytest.mark.parametrize(
    ("driver_name", "idle", "staged", "busy"),
    [("codex", CODEX_IDLE, CODEX_STAGED, CODEX_BUSY), ("grok", GROK_IDLE, GROK_STAGED, GROK_BUSY)],
)
def test_non_claude_delivery_uses_idle_composer_and_verifies_acceptance(
    driver_name, idle, staged, busy
):
    with (
        patch("hopper.server.capture_pane", side_effect=[idle, idle, staged, busy]),
        patch("hopper.server.pane_title", return_value=None),
        patch("hopper.server.paste_buffer", return_value=True) as paste,
        patch("hopper.server.send_keys", return_value=True) as send,
        patch("hopper.server.time.sleep"),
    ):
        result = _attempt_pane_delivery(
            "%1", "Please inspect the failing test", paste=True, driver_name=driver_name
        )
    assert result["reason"] == "enter_accepted"
    paste.assert_called_once_with("%1", "Please inspect the failing test")
    send.assert_called_once_with("%1", "Enter")


@pytest.mark.parametrize("driver_name,blocked", [("codex", CODEX_WAIT), ("grok", GROK_CARD)])
def test_non_claude_blockers_and_character_shortcut_send_nothing(driver_name, blocked):
    with (
        patch("hopper.server.capture_pane", side_effect=[blocked, blocked]),
        patch("hopper.server.pane_title", return_value=None),
        patch("hopper.server.paste_buffer") as paste,
        patch("hopper.server.send_keys") as send,
        patch("hopper.server.time.sleep"),
    ):
        result = _attempt_pane_delivery("%1", "body", paste=True, driver_name=driver_name)
    assert result["reason"] == "pane_blocked"
    paste.assert_not_called()
    send.assert_not_called()

    with (
        patch("hopper.server.capture_pane", return_value=blocked),
        patch("hopper.server.pane_title", return_value=None),
        patch("hopper.server.send_keys") as send,
    ):
        result = _attempt_character_delivery("%1", "y", driver_name=driver_name)
    assert result["reason"] == "pane_character_unsupported"
    send.assert_not_called()


@pytest.mark.parametrize(
    ("driver", "snapshot", "expected"),
    [(grok, GROK_CARD, "waiting"), (codex, "please run `codex login`", "authentication")],
)
def test_runner_publishes_visible_gate_for_provider_blocker_or_auth(driver, snapshot, expected):
    runner = BaseRunner("lode1234", MagicMock())
    runner.driver = driver
    runner.driver_name = driver.__name__.rsplit(".", 1)[-1]
    runner.driver_label = driver.LABEL
    runner._pane_id = "%1"
    runner._record_pane_snapshot = MagicMock()
    runner._emit_gate = MagicMock(return_value=True)
    with patch.object(runner, "_capture_activity_pane", return_value=snapshot):
        runner._check_activity()
    assert runner._gated.is_set()
    assert expected.lower() in runner._emit_gate.call_args.args[1].lower()


def test_runner_bootstraps_codex_before_binding_and_interactive_launch():
    runner = BaseRunner("lode1234", MagicMock(), run_generation="generation-1")
    runner._claude_stage = "mill"
    runner._cwd = "/repo"
    runner.is_first_run = True
    runner.driver = codex
    runner.driver_name = "codex"
    runner.driver_label = "Codex"
    runner._stage_protocol = StageDriverProtocol.CURRENT
    runner.claude_session_id = str(uuid.uuid4())
    runner.launch_id = stage_launch_id("lode1234", "mill", runner.claude_session_id)
    proc = MagicMock(returncode=0, stderr=None)
    proc.poll.return_value = 0
    observed = []

    def admit():
        observed.append(("bind", runner.claude_session_id, runner.launch_id))
        return True

    def build():
        return (
            codex.build_command(
                session_id=runner.claude_session_id, prompt="real stage prompt", resume=False
            ),
            "/repo",
        )

    def launch(command, **_kwargs):
        observed.append(("launch", command))
        return proc

    with (
        patch.object(codex, "prepare_session", return_value=(0, CODEX_SESSION_ID, None)),
        patch.object(runner, "_build_command", side_effect=build),
        patch.object(runner, "_admit_stage_start_before_launch", side_effect=admit),
        patch.object(runner, "_admit_stage_start_after_launch", return_value=True),
        patch("hopper.runner.subprocess.Popen", side_effect=launch),
        patch("hopper.runner.trust_claude_workspace") as trust,
        patch.object(runner, "_emit_state"),
        patch.object(runner, "_start_monitor"),
    ):
        assert runner._run_claude() == (0, None)

    expected_launch = stage_launch_id("lode1234", "mill", CODEX_SESSION_ID)
    assert observed[0] == ("bind", CODEX_SESSION_ID, expected_launch)
    assert observed[1][0] == "launch"
    assert observed[1][1][-1] == "real stage prompt"
    trust.assert_not_called()


def test_runner_codex_bootstrap_failure_launches_nothing():
    runner = BaseRunner("lode1234", MagicMock())
    runner._claude_stage = "mill"
    runner._cwd = "/repo"
    runner.is_first_run = True
    runner.driver = codex
    runner.driver_name = "codex"
    runner.driver_label = "Codex"
    with (
        patch.object(codex, "prepare_session", return_value=(1, None, "quota unavailable")),
        patch.object(runner, "_admit_stage_start_before_launch") as bind,
        patch("hopper.runner.subprocess.Popen") as launch,
    ):
        assert runner._run_claude() == (1, "quota unavailable")
    bind.assert_not_called()
    launch.assert_not_called()
