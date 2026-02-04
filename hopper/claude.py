"""Claude Code wrapper for hopper."""

from hopper.tmux import new_window, select_window


def spawn_claude(
    session_id: str,
    project_path: str | None = None,
    foreground: bool = True,
    stage: str = "ore",
) -> str | None:
    """Spawn Claude via hopper in a new tmux window.

    Args:
        session_id: The hopper session ID.
        project_path: Working directory for the Claude session.
        foreground: If True, switch to the new window. If False, stay in current window.
        stage: Session stage ("ore", "processing", or "ship") to determine which runner to use.

    Returns:
        The tmux pane ID on success, None on failure.
    """
    # Select runner based on stage
    stage_cmds = {"ore": "hop ore", "processing": "hop refine", "ship": "hop ship"}
    hop_cmd = stage_cmds.get(stage, "hop ore")
    # On failure, pause so user can see the error before window closes
    command = f"{hop_cmd} {session_id} || {{ echo 'Failed. Press Enter to close.'; read; }}"
    return new_window(command, cwd=project_path, background=not foreground)


def switch_to_pane(pane_id: str) -> bool:
    """Switch to the tmux window containing the given pane.

    Args:
        pane_id: The tmux pane ID to switch to (e.g., "%1").

    Returns:
        True if successfully switched, False otherwise.
    """
    return select_window(pane_id)
