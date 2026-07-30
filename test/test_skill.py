# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Contract tests for the bundled hop skill."""

from pathlib import Path

SKILL_PATH = Path(__file__).parents[1] / "skills" / "hop" / "SKILL.md"


def test_skill_has_one_implement_to_blocking_wait_recipe():
    text = SKILL_PATH.read_text()

    assert text.count("Practical create + blocking-wait workflow:") == 1
    assert "cat scope.md | hop implement myproject" in text
    assert "hop wait <lode-id>" in text


def test_skill_has_no_repeat_status_poll_recipe():
    text = SKILL_PATH.read_text()

    assert "repeat on an interval" not in text
    assert "poll to completion" not in text


def test_skill_documents_exec_exit_code_forwarding():
    text = SKILL_PATH.read_text()

    assert "inspect `r.exit_code` directly" in text
    assert "text(JSON.stringify({exit_code: r.exit_code, output: r.output}))" in text
    assert "never emit only `r.output` when success matters" in text
