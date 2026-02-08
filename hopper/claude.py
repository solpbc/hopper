# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Claude Code wrapper for hopper."""

import os
import shlex

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
    path = os.environ.get("PATH", "/usr/bin:/bin")
    # Run through /bin/sh so PATH and || work regardless of tmux's default shell
    fail = "echo 'Failed. Press Enter to close.'; read"
    inner = f"export PATH={shlex.quote(path)}; hop process {lode_id} || {{ {fail}; }}"
    command = f"/bin/sh -c {shlex.quote(inner)}"
    return new_window(command, cwd=project_path, background=not foreground)


def switch_to_pane(pane_id: str) -> bool:
    """Switch to the tmux window containing the given pane.

    Args:
        pane_id: The tmux pane ID to switch to (e.g., "%1").

    Returns:
        True if successfully switched, False otherwise.
    """
    return select_window(pane_id)
