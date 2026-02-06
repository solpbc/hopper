# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Code runner - runs a prompt via Codex, resuming the lode's Codex thread."""

import json
import logging
import os
from pathlib import Path

from hopper import prompt
from hopper.client import connect, set_lode_state
from hopper.codex import run_codex
from hopper.lodes import current_time_ms, format_duration_ms, get_lode_dir
from hopper.projects import find_project

logger = logging.getLogger(__name__)


def run_code(lode_id: str, socket_path: Path, stage_name: str, request: str) -> int:
    """Run a stage prompt via Codex for a refine-stage lode.

    Resumes the lode's Codex thread so that context accumulates across
    stages. Validates the prompt exists, lode is in refine stage,
    cwd matches the lode worktree, and a Codex thread ID is present.
    Saves artifacts (<stage>.in.md, <stage>.out.md, <stage>.json) to the
    lode directory and prints the output to stdout.

    Args:
        lode_id: The hopper lode ID.
        socket_path: Path to the server Unix socket.
        stage_name: Name of the prompt file (without .md extension).
        request: The user's directions/request text from stdin.

    Returns:
        Exit code (0 on success).
    """
    # Query server for lode data
    response = connect(socket_path, lode_id=lode_id)
    if not response:
        print("Failed to connect to server.")
        return 1

    lode_data = response.get("lode")
    if not lode_data:
        print(f"Lode {lode_id} not found.")
        return 1

    # Validate lode is in refine stage
    if lode_data.get("stage") != "refine":
        print(f"Lode {lode_id} is not in refine stage.")
        return 1

    # Validate Codex thread ID exists
    codex_thread_id = lode_data.get("codex_thread_id")
    if not codex_thread_id:
        print(f"Lode {lode_id} has no Codex thread ID.")
        print("The Codex session is bootstrapped during 'hop refine' first run.")
        print("Re-run 'hop refine' to bootstrap the Codex session.")
        return 1

    # Validate cwd is the lode worktree
    worktree_path = get_lode_dir(lode_id) / "worktree"
    cwd = Path.cwd()
    try:
        if cwd.resolve() != worktree_path.resolve():
            print(f"Must run from lode worktree: {worktree_path}")
            return 1
    except OSError:
        print(f"Must run from lode worktree: {worktree_path}")
        return 1

    # Build context for prompt template
    context: dict[str, str] = {"request": request}
    project_name = lode_data.get("project", "")
    if project_name:
        context["project"] = project_name
        project = find_project(project_name)
        if project:
            context["dir"] = project.path
    scope = lode_data.get("scope", "")
    if scope:
        context["scope"] = scope

    # Load prompt with context
    try:
        prompt_text = prompt.load(stage_name, context=context if context else None)
    except FileNotFoundError:
        print(f"Prompt not found: prompts/{stage_name}.md")
        return 1

    # Save input prompt
    lode_dir = get_lode_dir(lode_id)
    version = _next_version(lode_dir, stage_name)
    if version is None:
        suffix = stage_name
    else:
        suffix = f"{stage_name}_{version}"
    input_path = lode_dir / f"{suffix}.in.md"
    _atomic_write(input_path, prompt_text)

    # Set state to stage name while running
    set_lode_state(socket_path, lode_id, stage_name, f"Running {stage_name}")

    # Run codex (resume existing thread)
    output_path = lode_dir / f"{suffix}.out.md"
    started_at = current_time_ms()
    exit_code, cmd = run_codex(prompt_text, str(cwd), str(output_path), codex_thread_id)
    finished_at = current_time_ms()

    # Save run metadata
    metadata = {
        "stage": stage_name,
        "lode_id": lode_id,
        "codex_thread_id": codex_thread_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": finished_at - started_at,
        "exit_code": exit_code,
        "cmd": cmd,
    }
    meta_path = lode_dir / f"{suffix}.json"
    _atomic_write(meta_path, json.dumps(metadata, indent=2) + "\n")

    # Update status with stage result and duration
    duration = format_duration_ms(finished_at - started_at)
    if exit_code == 0:
        status = f"{stage_name} ran for {duration}"
    else:
        status = f"{stage_name} failed after {duration}"
    set_lode_state(socket_path, lode_id, "running", status)

    # Print output if it was written
    if output_path.exists():
        content = output_path.read_text()
        if content:
            print(content)

    return exit_code


def _next_version(lode_dir: Path, stage_name: str) -> int | None:
    """Return the next version number for stage artifacts, or None for first run.

    Checks if the base output file exists. If not, returns None (first run uses
    base names). If it does, probes _1, _2, ... until a free slot is found.
    """
    if not (lode_dir / f"{stage_name}.out.md").exists():
        return None
    n = 1
    while (lode_dir / f"{stage_name}_{n}.out.md").exists():
        n += 1
    return n


def _atomic_write(path: Path, content: str) -> None:
    """Write content to a file atomically via tmp + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    os.replace(tmp, path)
