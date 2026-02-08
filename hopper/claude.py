# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Claude Code wrapper for hopper."""

import sys

from hopper.tmux import new_window, select_window


def spawn_claude(
    lode_id: str,
    project_path: str | None = None,
    foreground: bool = True,
) -> str | None:
    """Spawn Claude via hopper in a new tmux window.

    Args:
        lode_id: The hopper lode ID.
        project_path: Working directory for the Claude session.
        foreground: If True, switch to the new window. If False, stay in current window.

    Returns:
        The tmux pane ID on success, None on failure.
    """
    hop = sys.argv[0]
    # On failure, pause so user can see the error before window closes
    command = f"{hop} process {lode_id} || {{ echo 'Failed. Press Enter to close.'; read; }}"
    return new_window(command, cwd=project_path, background=not foreground)


def switch_to_pane(pane_id: str) -> bool:
    """Switch to the tmux window containing the given pane.

    Args:
        pane_id: The tmux pane ID to switch to (e.g., "%1").

    Returns:
        True if successfully switched, False otherwise.
    """
    return select_window(pane_id)
