# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Claude Code wrapper for hopper."""

import os
import shlex

from hopper.tmux import WindowSpawnOutcome, new_window, select_window


def spawn_claude(
    lode_id: str,
    project_path: str | None = None,
    foreground: bool = False,
    env: dict[str, str] | None = None,
    spawn_receipt: dict | None = None,
) -> tuple[WindowSpawnOutcome, str | None]:
    """Spawn Claude via hopper in a new tmux window.

    Args:
        lode_id: The hopper lode ID.
        project_path: Working directory for the Claude session.
        foreground: If True, switch to the new window. Defaults to staying in current window.

    Returns:
        The authoritative tmux creation result and pane ID when known.
    """
    path = os.environ.get("PATH", "/usr/bin:/bin")
    # Run through /bin/sh so PATH and || work regardless of tmux's default shell
    fail = "echo 'Failed. Press Enter to close.'; read"
    inner = f"export PATH={shlex.quote(path)}; hop process {lode_id} || {{ {fail}; }}"
    command = f"/bin/sh -c {shlex.quote(inner)}"
    kwargs = {"cwd": project_path, "env": env, "background": not foreground}
    if spawn_receipt is not None:
        kwargs["spawn_receipt"] = spawn_receipt
    return new_window(command, **kwargs)


def switch_to_pane(pane_id: str) -> bool:
    """Switch to the tmux window containing the given pane.

    Args:
        pane_id: The tmux pane ID to switch to (e.g., "%1").

    Returns:
        True if successfully switched, False otherwise.
    """
    return select_window(pane_id)
