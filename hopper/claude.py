# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Claude interactive-stage driver adapter."""

from hopper.tmux import (
    KeyboardOwnership,
    PanePhase,
)
from hopper.tmux import (
    observe_pane as _observe_pane,
)
from hopper.tmux import (
    pane_answer_choices as _pane_answer_choices,
)
from hopper.tmux import (
    pane_answer_identity as _pane_answer_identity,
)
from hopper.tmux import (
    pane_surface_readable as _pane_surface_readable,
)
from hopper.tmux import (
    read_pane_input as _read_pane_input,
)

LABEL = "Claude"
_FLAGS = ("--dangerously-skip-permissions", "--disallowed-tools=AskUserQuestion")


def build_command(*, session_id: str, prompt: str | None, resume: bool) -> list[str]:
    """Build one Claude command while preserving its durable session identity."""
    if resume:
        return ["claude", *_FLAGS, "--resume", session_id]
    if not isinstance(prompt, str):
        raise ValueError("a Claude first launch requires a prompt")
    return ["claude", *_FLAGS, "--session-id", session_id, prompt]


def subprocess_environment() -> dict[str, str]:
    """Return Claude-only environment additions."""
    return {
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_DISABLE_MEMORY_PERIODIC_RESYNC": "1",
        "CLAUDE_CODE_DISABLE_MEMORY_BULK_INFLATE": "1",
        "CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION": "false",
    }


def requires_workspace_trust() -> bool:
    return True


def observe_pane(
    title: str | None,
    snapshot: str,
    *,
    background_work_active: bool = False,
) -> tuple[PanePhase, KeyboardOwnership]:
    return _observe_pane(title, snapshot, background_work_active=background_work_active)


def read_pane_input(snapshot: str) -> str | None:
    return _read_pane_input(snapshot)


def pane_surface_readable(snapshot: str) -> bool:
    return _pane_surface_readable(snapshot)


def pane_answer_choices(snapshot: str):
    return _pane_answer_choices(snapshot)


def pane_blocked_identity(snapshot: str):
    return _pane_answer_identity(snapshot)
