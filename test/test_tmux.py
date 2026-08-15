# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for tmux interaction utilities."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hopper import actions
from hopper.tmux import (
    Liveness,
    PanePhase,
    WindowSpawnOutcome,
    bootstrap_spawn_receipt,
    capture_pane,
    classify_pane_phase,
    completion_action_panes,
    get_current_pane_id,
    get_current_tmux_location,
    get_pane_pid,
    get_tmux_sessions,
    is_inside_tmux,
    is_tmux_server_running,
    kill_pane,
    new_window,
    pane_answer_choices,
    pane_answer_identity,
    pane_identity,
    pane_liveness,
    pane_needs_answer,
    pane_surface_readable,
    pane_title,
    paste_buffer,
    read_pane_input,
    rename_window,
    send_keys,
)


def _tmux_result(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (_tmux_result(stdout="%17\n"), (WindowSpawnOutcome.SPAWNED, "%17")),
        (_tmux_result(stdout=""), (WindowSpawnOutcome.UNKNOWN, None)),
        (_tmux_result(stdout="pane-17\n"), (WindowSpawnOutcome.UNKNOWN, None)),
        (_tmux_result(stdout="%17 extra\n"), (WindowSpawnOutcome.UNKNOWN, None)),
        (_tmux_result(returncode=1, stderr="no server"), (WindowSpawnOutcome.PROVEN_NO_PANE, None)),
        (FileNotFoundError("tmux"), (WindowSpawnOutcome.PROVEN_NO_PANE, None)),
        (PermissionError("tmux"), (WindowSpawnOutcome.UNKNOWN, None)),
    ],
    ids=[
        "well-formed-pane",
        "empty-identity",
        "malformed-identity",
        "multiple-fields",
        "tmux-refused",
        "tmux-absent",
        "execution-unknown",
    ],
)
def test_new_window_ordinary_tri_state_matrix(result, expected):
    with patch("hopper.tmux.subprocess.run", side_effect=[result]):
        assert new_window("hop process abc12345", background=True) == expected


def test_new_window_lost_identity_never_proves_live_pane_absent():
    pane_created = False

    def create_without_reporting_identity(*args, **kwargs):
        nonlocal pane_created
        pane_created = True
        return _tmux_result(stdout="")

    with patch("hopper.tmux.subprocess.run", side_effect=create_without_reporting_identity):
        outcome = new_window("hop process abc12345", background=True)

    assert pane_created is True
    assert outcome == (WindowSpawnOutcome.UNKNOWN, None)


@pytest.mark.parametrize(
    ("results", "expected"),
    [
        (
            [_tmux_result(returncode=1, stderr="lock refused")],
            (WindowSpawnOutcome.UNKNOWN, None),
        ),
        ([FileNotFoundError("tmux")], (WindowSpawnOutcome.UNKNOWN, None)),
        (
            [_tmux_result(), _tmux_result(returncode=1), _tmux_result()],
            (WindowSpawnOutcome.PROVEN_NO_PANE, None),
        ),
        (
            [_tmux_result(), _tmux_result(stdout=""), _tmux_result()],
            (WindowSpawnOutcome.UNKNOWN, None),
        ),
        (
            [
                _tmux_result(),
                _tmux_result(stdout="%9\n"),
                _tmux_result(returncode=1, stderr="wait failed"),
                # A failed wait now consults the pane tag; no pane reports this
                # action, so the outcome must still be UNKNOWN.
                _tmux_result(),
                _tmux_result(),
            ],
            (WindowSpawnOutcome.UNKNOWN, None),
        ),
        (
            [
                _tmux_result(),
                _tmux_result(stdout="%9\n"),
                OSError("wait broke"),
                _tmux_result(),
            ],
            (WindowSpawnOutcome.UNKNOWN, None),
        ),
        (
            [
                _tmux_result(),
                _tmux_result(stdout="%9\n"),
                _tmux_result(),
                _tmux_result(returncode=1, stderr="unlock failed"),
            ],
            (WindowSpawnOutcome.UNKNOWN, None),
        ),
        (
            [
                _tmux_result(),
                _tmux_result(stdout="%9\n"),
                _tmux_result(),
                _tmux_result(),
            ],
            (WindowSpawnOutcome.SPAWNED, "%9"),
        ),
    ],
    ids=[
        "lock-refused",
        "lock-command-absent",
        "new-window-refused",
        "identity-lost",
        "wait-refused",
        "wait-oserror",
        "unlock-refused",
        "receipt-complete",
    ],
)
def test_new_window_receipt_tri_state_matrix(tmp_path, results, expected):
    receipt = {
        "path": str(tmp_path / "receipt.json"),
        "action_id": "a" * 32,
        "source_lode_id": "abcd2345",
        "target_lode_id": "bcde2345",
        "target_generation": "b" * 32,
    }
    with patch("hopper.tmux.subprocess.run", side_effect=results):
        assert new_window("hop process bcde2345", spawn_receipt=receipt) == expected


def test_spawn_bootstrap_tags_exact_pane_before_durable_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr("hopper.actions.config.hopper_dir", lambda: tmp_path)
    monkeypatch.setenv("TMUX_PANE", "%17")
    action_id = "a" * 32
    generation = "b" * 32
    path = actions.spawn_receipt_path("abcd2345", action_id)

    with patch("hopper.tmux.subprocess.run") as run:
        run.return_value.returncode = 0
        bootstrap_spawn_receipt(str(path), action_id, "abcd2345", "bcde2345", generation)

    run.assert_called_once_with(
        [
            "tmux",
            "set-option",
            "-p",
            "-t",
            "%17",
            "@hopper_completion_action",
            action_id,
        ],
        capture_output=True,
        text=True,
    )
    assert actions.load_spawn_receipt("abcd2345", action_id)["pane_id"] == "%17"


def test_completion_action_panes_fails_closed_on_ambiguous_rows():
    with patch("hopper.tmux.subprocess.run") as run:
        run.return_value.returncode = 0
        run.return_value.stdout = "%1\taction\nmalformed\n"
        assert completion_action_panes("action") is None


# Constructed by trimming the real prep capture while retaining its verified U+2500 rules,
# U+276F prompt, U+00A0 spacing, and processing content.
PROCESSING_EMPTY_INPUT_CAPTURE = (
    """\
● Running 1 shell command · 2m 45s…
  ⎿  $ hop code prep <<'EOF'

· Clauding…

────────────────────
❯"""
    "\u00a0\n"
    """────────────────────
  ⏵⏵ bypass permissions on
"""
)

# Constructed from the scope §4.5 staged text and the prep-verified U+2500/U+276F/U+00A0
# input-box structure.
IDLE_STAGED_INPUT_CAPTURE = """\
────────────────────
❯ gate 1 approved, let's keep it moving
────────────────────
"""

# Constructed from the prep-verified U+2500 rule, U+276F prompt, and U+00A0 spacing.
IDLE_PASTED_PLACEHOLDER_CAPTURE = """\
────────────────────
❯ [Pasted text #1 +40 lines]
────────────────────
"""


class TestGetPanePid:
    def test_returns_pane_root_pid(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "12345\n"

            assert get_pane_pid("%7") == 12345

        mock_run.assert_called_once_with(
            ["tmux", "display-message", "-p", "-t", "%7", "#{pane_pid}"],
            capture_output=True,
            text=True,
        )

    @pytest.mark.parametrize("stdout", ["", "not-a-pid\n"])
    def test_returns_none_for_invalid_pid(self, stdout):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = stdout

            assert get_pane_pid("%7") is None

    def test_returns_none_when_tmux_fails(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""

            assert get_pane_pid("%7") is None


class TestPaneIdentity:
    def test_returns_exact_pane_window_and_root_pid(self):
        with patch("hopper.tmux.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "%7\t@3\t12345\n"

            assert pane_identity("%7") == {
                "pane_id": "%7",
                "window_id": "@3",
                "pane_pid": 12345,
            }

        mock_run.assert_called_once_with(
            [
                "tmux",
                "display-message",
                "-p",
                "-t",
                "%7",
                "#{pane_id}\t#{window_id}\t#{pane_pid}",
            ],
            capture_output=True,
            text=True,
        )

    @pytest.mark.parametrize(
        "stdout",
        ["", "%8\t@3\t12345\n", "%7\t\t12345\n", "%7\t@3\tbad\n", "%7\t@3\t0\n"],
    )
    def test_ambiguous_identity_is_none(self, stdout):
        with patch("hopper.tmux.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = stdout

            assert pane_identity("%7") is None

    def test_command_failure_is_none(self):
        with patch("hopper.tmux.subprocess.run", side_effect=PermissionError):
            assert pane_identity("%7") is None


class TestPaneTitle:
    def test_returns_nonempty_title(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "✳ Ready for input\n"

            assert pane_title("%7") == "✳ Ready for input"

        mock_run.assert_called_once_with(
            ["tmux", "display-message", "-p", "-t", "%7", "#{pane_title}"],
            capture_output=True,
            text=True,
        )

    def test_blank_is_none_even_on_status_zero(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "\n"

            assert pane_title("%99999") is None

    @pytest.mark.parametrize("returncode", [1, 2])
    def test_failure_is_none(self, returncode):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = returncode
            mock_run.return_value.stdout = "ignored"

            assert pane_title("%7") is None

    @pytest.mark.parametrize("error", [FileNotFoundError(), PermissionError()])
    def test_execution_failure_is_none(self, error):
        with patch("subprocess.run", side_effect=error):
            assert pane_title("%7") is None


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("✳ Ready", PanePhase.IDLE),
        ("◐ Thinking", PanePhase.PROCESSING),
        ("◑ Thinking", PanePhase.PROCESSING),
        ("◒ Thinking", PanePhase.PROCESSING),
        ("◓ Thinking", PanePhase.PROCESSING),
        ("⠀ Thinking", PanePhase.PROCESSING),
        ("⠐ Thinking", PanePhase.PROCESSING),
        ("⣿ Thinking", PanePhase.PROCESSING),
        (None, PanePhase.UNKNOWN),
        ("", PanePhase.UNKNOWN),
        ("Fix auth bug", PanePhase.UNKNOWN),
        ("extro", PanePhase.UNKNOWN),
        ("★ Other", PanePhase.UNKNOWN),
    ],
)
def test_classify_pane_phase_literal_titles(title, expected):
    assert classify_pane_phase(title) is expected


class TestReadPaneInput:
    def test_real_processing_capture_has_empty_input(self):
        assert read_pane_input(PROCESSING_EMPTY_INPUT_CAPTURE) == ""

    def test_scope_idle_capture_has_staged_input(self):
        assert read_pane_input(IDLE_STAGED_INPUT_CAPTURE) == (
            "gate 1 approved, let's keep it moving"
        )

    def test_constructed_placeholder_is_staged(self):
        assert read_pane_input(IDLE_PASTED_PLACEHOLDER_CAPTURE) == ("[Pasted text #1 +40 lines]")

    def test_uses_last_complete_input_box(self):
        combined = IDLE_STAGED_INPUT_CAPTURE + PROCESSING_EMPTY_INPUT_CAPTURE
        assert read_pane_input(combined) == ""

    def test_no_prompt_returns_none(self):
        # Constructed from the verified U+2500 layout with its U+276F prompt removed.
        capture = "───\nstatus only\n───\n"
        assert read_pane_input(capture) is None

    def test_incomplete_box_returns_none(self):
        # Constructed from the verified layout with only one U+2500 rule.
        assert read_pane_input("───\n❯ staged\n") is None


class TestPaneLiveness:
    def test_alive_on_success(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""

            assert pane_liveness("%1") is Liveness.ALIVE

        mock_run.assert_called_once_with(
            ["tmux", "has-session", "-t", "%1"],
            capture_output=True,
            text=True,
        )

    def test_gone_only_for_missing_pane(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "can't find pane: %9\n"

            assert pane_liveness("%9") is Liveness.GONE

    @pytest.mark.parametrize(
        "stderr",
        [
            "no server running on /tmp/tmux/default\n",
            "error connecting to /tmp/tmux/default (No such file or directory)\n",
            "permission denied\n",
            "unexpected tmux failure\n",
        ],
    )
    def test_unknown_for_other_tmux_failures(self, stderr):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = stderr

            assert pane_liveness("%1") is Liveness.UNKNOWN

    @pytest.mark.parametrize("error", [FileNotFoundError(), PermissionError()])
    def test_unknown_when_tmux_cannot_execute(self, error):
        with patch("subprocess.run", side_effect=error):
            assert pane_liveness("%1") is Liveness.UNKNOWN


class TestIsInsideTmux:
    def test_returns_true_when_tmux_env_set(self):
        with patch.dict("os.environ", {"TMUX": "/tmp/tmux-1000/default,12345,0"}):
            assert is_inside_tmux() is True

    def test_returns_false_when_tmux_env_not_set(self):
        with patch.dict("os.environ", {}, clear=True):
            assert is_inside_tmux() is False


class TestIsTmuxServerRunning:
    def test_returns_true_when_sessions_exist(self):
        with patch("hopper.tmux.get_tmux_sessions", return_value=["main"]):
            assert is_tmux_server_running() is True

    def test_returns_false_when_no_sessions(self):
        with patch("hopper.tmux.get_tmux_sessions", return_value=[]):
            assert is_tmux_server_running() is False


class TestGetTmuxSessions:
    def test_returns_session_names(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "main\ndev\nhopper\n"
            sessions = get_tmux_sessions()
            assert sessions == ["main", "dev", "hopper"]
            mock_run.assert_called_once_with(
                ["tmux", "list-sessions", "-F", "#{session_name}"],
                capture_output=True,
                text=True,
            )

    def test_returns_empty_list_when_no_sessions(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            assert get_tmux_sessions() == []

    def test_returns_empty_list_when_tmux_not_installed(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert get_tmux_sessions() == []


class TestGetCurrentPaneId:
    def test_returns_pane_id_when_set(self):
        with patch.dict("os.environ", {"TMUX_PANE": "%5"}):
            assert get_current_pane_id() == "%5"

    def test_returns_none_when_not_set(self):
        with patch.dict("os.environ", {}, clear=True):
            assert get_current_pane_id() is None

    def test_returns_none_when_empty(self):
        with patch.dict("os.environ", {"TMUX_PANE": ""}):
            assert get_current_pane_id() is None


class TestGetCurrentTmuxLocation:
    def test_returns_location_when_inside_tmux(self):
        with patch.dict(
            "os.environ",
            {"TMUX": "/tmp/tmux-1000/default,12345,0", "TMUX_PANE": "%5"},
        ):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value.returncode = 0
                mock_run.return_value.stdout = "main\n"
                result = get_current_tmux_location()
                assert result == {"session": "main", "pane": "%5"}
                mock_run.assert_called_once_with(
                    ["tmux", "display-message", "-t", "%5", "-p", "#{session_name}"],
                    capture_output=True,
                    text=True,
                )

    def test_returns_none_when_not_inside_tmux(self):
        with patch.dict("os.environ", {}, clear=True):
            result = get_current_tmux_location()
            assert result is None

    def test_returns_none_when_no_tmux_pane(self):
        with patch.dict("os.environ", {"TMUX": "/tmp/tmux-1000/default,12345,0"}, clear=True):
            result = get_current_tmux_location()
            assert result is None

    def test_returns_none_when_command_fails(self):
        with patch.dict(
            "os.environ",
            {"TMUX": "/tmp/tmux-1000/default,12345,0", "TMUX_PANE": "%5"},
        ):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value.returncode = 1
                mock_run.return_value.stdout = ""
                result = get_current_tmux_location()
                assert result is None

    def test_returns_none_when_tmux_not_installed(self):
        with patch.dict(
            "os.environ",
            {"TMUX": "/tmp/tmux-1000/default,12345,0", "TMUX_PANE": "%5"},
        ):
            with patch("subprocess.run", side_effect=FileNotFoundError):
                result = get_current_tmux_location()
                assert result is None


class TestCapturePane:
    def test_returns_content_on_success(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "\x1b[32mGreen text\x1b[0m\n"
            result = capture_pane("@0")
            assert result == "\x1b[32mGreen text\x1b[0m\n"
            mock_run.assert_called_once_with(
                ["tmux", "capture-pane", "-e", "-p", "-t", "@0"],
                capture_output=True,
                text=True,
            )

    def test_plain_omits_ansi_flag(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "Plain text\n"
            result = capture_pane("@0", plain=True)
            assert result == "Plain text\n"
            mock_run.assert_called_once_with(
                ["tmux", "capture-pane", "-p", "-t", "@0"],
                capture_output=True,
                text=True,
            )

    def test_returns_none_when_command_fails(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            result = capture_pane("@99")
            assert result is None

    def test_returns_none_when_tmux_not_installed(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = capture_pane("@0")
            assert result is None


class TestKillPane:
    def test_kill_pane_success(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0

            result = kill_pane("%1")

        assert result is True
        mock_run.assert_called_once_with(
            ["tmux", "kill-pane", "-t", "%1"],
            capture_output=True,
            text=True,
        )

    def test_kill_pane_failure(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1

            result = kill_pane("%1")

        assert result is False


class TestRenameWindow:
    def test_renames_successfully(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            result = rename_window("%0", "hop:mill")
            assert result is True
            mock_run.assert_called_once_with(
                ["tmux", "rename-window", "-t", "%0", "hop:mill"],
                capture_output=True,
                text=True,
            )

    def test_returns_false_when_command_fails(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            result = rename_window("%99", "test")
            assert result is False

    def test_returns_false_when_tmux_not_installed(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = rename_window("%0", "test")
            assert result is False


class TestSendKeys:
    def test_sends_keys_successfully(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            result = send_keys("@0", "C-d")
            assert result is True
            mock_run.assert_called_once_with(
                ["tmux", "send-keys", "-t", "@0", "C-d"],
                capture_output=True,
                text=True,
            )

    def test_returns_false_when_command_fails(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            result = send_keys("@99", "C-d")
            assert result is False

    def test_returns_false_when_tmux_not_installed(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = send_keys("@0", "C-d")
            assert result is False


class TestPasteBuffer:
    def test_pastes_buffer_successfully(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0

            result = paste_buffer("%1", "hello\nthere")

        assert result is True
        assert mock_run.call_args_list[0].args[0] == ["tmux", "set-buffer", "hello\nthere"]
        assert mock_run.call_args_list[1].args[0] == ["tmux", "paste-buffer", "-t", "%1"]

    def test_returns_false_when_set_buffer_fails(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1

            result = paste_buffer("%1", "hello")

        assert result is False
        assert mock_run.call_count == 1

    def test_returns_false_when_paste_fails(self):
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                type("Result", (), {"returncode": 0})(),
                type("Result", (), {"returncode": 1})(),
            ]

            result = paste_buffer("%1", "hello")

        assert result is False


def test_pane_needs_answer_detects_a_colorized_selector():
    """Real captures carry ANSI escapes; the detector must see through them.

    `capture_pane` keeps escapes by default (`-e`), and the runner's prompt
    gating passes exactly that. Measured 2026-08-09: the gating had never once
    fired in production because Claude colorizes the selector, so the cursor and
    its number are separated by a colour sequence.
    """
    colorized = (
        "Which cutover should I take?\n"
        "\x1b[36m❯\x1b[39m \x1b[1m1.\x1b[22m Delete the Python wire now\n"
        "  \x1b[1m2.\x1b[22m Keep both behind a flag\n"
        "  \x1b[1m3.\x1b[22m Type something.\n"
        "\x1b[2m↑/↓ to navigate · Enter to select · Esc to cancel\x1b[22m\n"
    )
    assert pane_needs_answer(colorized) is True
    assert pane_answer_choices(colorized) == (1, (1, 2, 3), frozenset({3}))


def test_pane_needs_answer_detects_a_plain_selector():
    plain = (
        "Which cutover should I take?\n"
        "❯ 1. Delete the Python wire now\n"
        "  2. Keep both behind a flag\n"
        "  3. Type something.\n"
        "↑/↓ to navigate · Enter to select · Esc to cancel\n"
    )
    assert pane_needs_answer(plain) is True
    assert pane_answer_choices(plain) == (1, (1, 2, 3), frozenset({3}))


def test_pane_answer_identity_distinguishes_same_shape_questions():
    # Constructed from the existing plain-selector fixture with different question and label text.
    deploy = (
        "Deploy where?\n"
        "❯ 1. Production\n"
        "  2. Staging\n"
        "↑/↓ to navigate · Enter to select · Esc to cancel\n"
    )
    delete = (
        "Delete the database?\n"
        "❯ 1. Delete permanently\n"
        "  2. Cancel\n"
        "↑/↓ to navigate · Enter to select · Esc to cancel\n"
    )

    assert pane_answer_choices(deploy) == pane_answer_choices(delete)
    assert pane_answer_identity(deploy) == (
        "Deploy where?",
        ((1, "Production"), (2, "Staging")),
    )
    assert pane_answer_identity(deploy) != pane_answer_identity(delete)


def test_pane_answer_identity_is_total_for_a_selector_without_a_question():
    # Constructed from the existing edited-free-text fixture by omitting all preceding prose.
    selector = "❯ 1. Keep current behavior\n  2. Change it\nEnter to select · Esc to cancel\n"

    assert pane_answer_identity(selector) == (
        "",
        ((1, "Keep current behavior"), (2, "Change it")),
    )


def test_pane_answer_identity_normalizes_wrapped_rendered_text():
    # Constructed from the existing plain-selector fixture with terminal-wrapped question and label.
    selector = (
        "Which environment should receive\n"
        "  this deployment?\n"
        "❯ 1. The production\n"
        "     environment\n"
        "  2. Staging\n"
        "↑/↓ to navigate · Enter to select · Esc to cancel\n"
    )

    assert pane_answer_identity(selector) == (
        "Which environment should receive this deployment?",
        ((1, "The production environment"), (2, "Staging")),
    )


def test_pane_answer_identity_ignores_ancillary_line_after_last_option():
    # Constructed from the existing plain-selector fixture with an unnumbered ancillary control.
    first = (
        "Deploy where?\n"
        "❯ 1. Production\n"
        "  2. Staging\n"
        "  n to add notes\n"
        "↑/↓ to navigate · Enter to select · Esc to cancel\n"
    )
    second = first.replace("n to add notes", "n notes unavailable")

    assert pane_answer_identity(first) == pane_answer_identity(second)


def test_pane_surface_readable_accepts_empty_composer_and_selector():
    # Composer shape comes from PROCESSING_EMPTY_INPUT_CAPTURE; selector shape is the plain fixture.
    selector = (
        "Which cutover should I take?\n"
        "❯ 1. Delete the Python wire now\n"
        "  2. Keep both behind a flag\n"
        "↑/↓ to navigate · Enter to select · Esc to cancel\n"
    )

    assert read_pane_input(PROCESSING_EMPTY_INPUT_CAPTURE) == ""
    assert pane_surface_readable(PROCESSING_EMPTY_INPUT_CAPTURE) is True
    assert pane_surface_readable(selector) is True


def test_pane_answer_choices_recognizes_edited_free_text_row_before_rule():
    capture = (
        "  2. Keep both behind a flag\n"
        "  3. deny.toml-only stopgap for it\n"
        "❯ 4. 3\n"
        "────────────────────────────────────────────────────────────────\n"
        "  5. Chat about this\n"
        "Enter to select · Tab/Arrow keys to navigate · Esc to cancel\n"
    )

    assert pane_answer_choices(capture) == (4, (2, 3, 4, 5), frozenset({4}))


def test_pane_answer_choices_ignores_numbered_prose_above_current_selector():
    capture = (
        "Earlier plan:\n"
        "  1. inspect the repository\n"
        "  2. run the gate\n"
        "\n"
        "Which cutover should I take?\n"
        "  1. Delete the Python wire now\n"
        "❯ 2. Keep both behind a flag\n"
        "  3. Type something.\n"
        "↑/↓ to navigate · Enter to select · Esc to cancel\n"
    )

    assert pane_answer_choices(capture) == (2, (1, 2, 3), frozenset({3}))


def test_pane_needs_answer_ignores_an_ordinary_composer():
    """The other direction: a normal pane must never read as a selector."""
    assert pane_needs_answer("─────\n❯ Please revise\n─────\n") is False
    assert pane_needs_answer("") is False
    # Numbered prose without selector chrome is not a prompt.
    assert pane_needs_answer("❯ 1. first item\n❯ 2. second item\n") is False


def _receipt(action_id="a" * 32):
    return {
        "path": f"/tmp/{action_id}.json",
        "action_id": action_id,
        "source_lode_id": "abc12345",
        "target_lode_id": "abc12345",
        "target_generation": "b" * 32,
    }


@pytest.mark.parametrize(
    ("tagged", "expected"),
    [
        (["%17"], (WindowSpawnOutcome.SPAWNED, "%17")),
        ([], (WindowSpawnOutcome.UNKNOWN, None)),
        (None, (WindowSpawnOutcome.UNKNOWN, None)),
        (["%99"], (WindowSpawnOutcome.UNKNOWN, None)),
        (["%17", "%99"], (WindowSpawnOutcome.UNKNOWN, None)),
    ],
    ids=[
        "our-pane-tagged-is-proof",
        "no-pane-tagged-stays-unknown",
        "tmux-unreadable-stays-unknown",
        "other-pane-tagged-stays-unknown",
        "ambiguous-tags-stay-unknown",
    ],
)
def test_failed_receipt_wait_consults_the_pane_tag_before_giving_up(tagged, expected):
    """A failed wait is a lost notification, not proof the pane was never claimed.

    The bootstrap tags its pane and fsyncs its receipt before releasing the lock, so
    tmux can still answer which pane carries the action tag. Only our exact pane counts;
    an unreadable tmux stays UNKNOWN.
    """
    results = [
        _tmux_result(),  # wait-for -L : acquire
        _tmux_result(stdout="%17\n"),  # new-window returns the pane id
        _tmux_result(returncode=1, stderr="wait failed"),  # wait-for -L : receipt wait
        _tmux_result(),  # wait-for -U : release
    ]
    with (
        patch("hopper.tmux.subprocess.run", side_effect=results),
        patch("hopper.tmux.completion_action_panes", return_value=tagged) as lookup,
    ):
        outcome = new_window("hop process abc12345", background=True, spawn_receipt=_receipt())

    assert outcome == expected
    lookup.assert_called_once_with("a" * 32)
