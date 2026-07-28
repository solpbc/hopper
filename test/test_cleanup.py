# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for platform process cleanup."""

import signal
from unittest.mock import MagicMock, call, patch

from hopper.cleanup import _orphan_process_pids, reap_swiftpm_testing_helpers


def test_orphan_scan_requires_ppid_one_and_exact_argv0_basename():
    result = MagicMock(returncode=0)
    result.stdout = "\n".join(
        [
            "101 1 /tmp/build/swiftpm-testing-helper --flag",
            "102 55 /tmp/build/swiftpm-testing-helper",
            "103 1 /bin/sh -c swiftpm-testing-helper",
            "104 1 /tmp/build/swiftpm-testing-helper-extra",
            "bad row",
        ]
    )

    with patch("hopper.cleanup.subprocess.run", return_value=result):
        assert _orphan_process_pids("swiftpm-testing-helper") == [101]


def test_orphan_scan_failure_is_fail_safe(caplog):
    with patch("hopper.cleanup.subprocess.run", side_effect=TimeoutError("slow ps")):
        assert _orphan_process_pids("swiftpm-testing-helper") == []

    assert "orphan process scan failed: TimeoutError: slow ps" in caplog.messages


def test_reaper_terms_targets_and_kills_only_reverified_survivors():
    with (
        patch("hopper.cleanup.sys.platform", "darwin"),
        patch(
            "hopper.cleanup._orphan_process_pids",
            side_effect=[[101, 102], [101, 102], [102, 999], [999]],
        ),
        patch("hopper.cleanup.os.kill") as kill,
        patch("hopper.cleanup.time.sleep") as sleep,
    ):
        assert reap_swiftpm_testing_helpers() == [101, 102]

    assert kill.call_args_list == [
        call(101, signal.SIGTERM),
        call(102, signal.SIGTERM),
        call(102, signal.SIGKILL),
    ]
    sleep.assert_called_once()


def test_reaper_reports_only_helpers_confirmed_gone(caplog):
    with (
        patch("hopper.cleanup.sys.platform", "darwin"),
        patch(
            "hopper.cleanup._orphan_process_pids",
            side_effect=[[101], [101], [101], [101]],
        ),
        patch("hopper.cleanup.os.kill", side_effect=PermissionError),
        patch("hopper.cleanup.time.sleep"),
    ):
        assert reap_swiftpm_testing_helpers() == []

    assert "SwiftPM testing helpers survived cleanup: 101" in caplog.messages
    assert not any(message.startswith("reaped ") for message in caplog.messages)


def test_reaper_is_noop_off_macos():
    with (
        patch("hopper.cleanup.sys.platform", "linux"),
        patch("hopper.cleanup._orphan_process_pids") as scan,
    ):
        assert reap_swiftpm_testing_helpers() == []

    scan.assert_not_called()
