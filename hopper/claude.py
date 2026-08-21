# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Claude interactive-stage driver adapter."""

LABEL = "Claude"
_FLAGS = ("--dangerously-skip-permissions", "--disallowed-tools=AskUserQuestion")


def build_command(*, session_id: str, prompt: str | None, resume: bool) -> list[str]:
    """Build one Claude command while preserving its durable session identity."""
    if resume:
        return ["claude", *_FLAGS, "--resume", session_id]
    if not isinstance(prompt, str):
        raise ValueError("a Claude first launch requires a prompt")
    return ["claude", *_FLAGS, "--session-id", session_id, prompt]
