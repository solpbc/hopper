# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Project management for hopper."""

import subprocess
from dataclasses import dataclass
from pathlib import Path

from hopper.config import load_config, save_config
from hopper.lodes import current_time_ms


@dataclass
class Project:
    """A registered project directory."""

    path: str  # Absolute path to git directory
    name: str  # Basename of directory
    disabled: bool = False  # True if removed but has existing sessions
    last_used_at: int = 0


def validate_git_dir(path: str) -> bool:
    """Check if a path is a valid git repository.

    Args:
        path: Path to check.

    Returns:
        True if the path is a git repository, False otherwise.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=path,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    except (FileNotFoundError, OSError):
        return False


def validate_makefile_install(path: str) -> bool:
    """Check if a path has a Makefile with an install target.

    Args:
        path: Path to check.

    Returns:
        True if `make -n install` succeeds, False otherwise.
    """
    try:
        result = subprocess.run(
            ["make", "-n", "install", "-C", path],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    except (FileNotFoundError, OSError):
        return False


def load_projects() -> list[Project]:
    """Load projects from config.

    Returns:
        List of Project objects, empty if none configured.
    """
    config = load_config()
    projects_data = config.get("projects", [])
    if not isinstance(projects_data, list):
        return []

    projects = []
    for item in projects_data:
        if isinstance(item, dict) and "path" in item and "name" in item:
            projects.append(
                Project(
                    path=item["path"],
                    name=item["name"],
                    disabled=item.get("disabled", False),
                    last_used_at=item.get("last_used_at", 0),
                )
            )
    return projects


def save_projects(projects: list[Project]) -> None:
    """Save projects to config.

    Args:
        projects: List of projects to save.
    """
    config = load_config()
    config["projects"] = [
        {
            "path": p.path,
            "name": p.name,
            "disabled": p.disabled,
            "last_used_at": p.last_used_at,
        }
        for p in projects
    ]
    save_config(config)


def touch_project(name: str) -> None:
    """Update last_used_at timestamp for a project."""
    projects = load_projects()
    for p in projects:
        if p.name == name:
            p.last_used_at = current_time_ms()
            break
    save_projects(projects)


def add_project(path: str) -> Project:
    """Add a new project.

    Args:
        path: Path to git directory.

    Returns:
        The newly created Project.

    Raises:
        ValueError: If path is not a directory, not a git repository,
            has no Makefile with install target, or name already exists.
    """
    # Resolve to absolute path
    abs_path = str(Path(path).resolve())

    if not Path(abs_path).is_dir():
        raise ValueError(f"Not a directory: {abs_path}")

    if not validate_git_dir(abs_path):
        raise ValueError(f"Not a git repository: {abs_path}")
    if not validate_makefile_install(abs_path):
        raise ValueError(f"No Makefile with 'install' target: {abs_path}")

    name = Path(abs_path).name
    projects = load_projects()

    # Check for duplicate name (including disabled projects)
    for p in projects:
        if p.name == name:
            raise ValueError(f"Project with name '{name}' already exists")

    project = Project(path=abs_path, name=name)
    projects.append(project)
    save_projects(projects)
    return project


def remove_project(name: str) -> bool:
    """Disable a project by name.

    Args:
        name: Project name to disable.

    Returns:
        True if project was found and disabled, False otherwise.
    """
    projects = load_projects()
    for p in projects:
        if p.name == name:
            p.disabled = True
            save_projects(projects)
            return True
    return False


def rename_project(current_name: str, new_name: str) -> None:
    """Rename an active project."""
    projects = load_projects()

    target = None
    for p in projects:
        if p.name == current_name:
            target = p
            break

    if target is None:
        raise ValueError(f"Project not found: {current_name}")
    if target.disabled:
        raise ValueError(f"Project '{current_name}' is disabled")

    for p in projects:
        if p.name == new_name:
            raise ValueError(f"Project with name '{new_name}' already exists")

    target.name = new_name
    save_projects(projects)


def rename_project_in_data(current_name: str, new_name: str) -> None:
    """Update project name in all lode and backlog data files."""
    from hopper.backlog import load_backlog, save_backlog
    from hopper.lodes import (
        load_archived_lodes,
        load_lodes,
        save_archived_lodes,
        save_lodes,
    )

    # Update active lodes
    lodes = load_lodes()
    for lode in lodes:
        if lode.get("project") == current_name:
            lode["project"] = new_name
    save_lodes(lodes)

    # Update archived lodes
    archived = load_archived_lodes()
    for lode in archived:
        if lode.get("project") == current_name:
            lode["project"] = new_name
    save_archived_lodes(archived)

    # Update backlog items
    items = load_backlog()
    for item in items:
        if item.project == current_name:
            item.project = new_name
    save_backlog(items)


def find_project(name: str) -> Project | None:
    """Find a project by name.

    Args:
        name: Project name to find.

    Returns:
        The Project if found, None otherwise.
    """
    projects = load_projects()
    for p in projects:
        if p.name == name:
            return p
    return None


def get_active_projects() -> list[Project]:
    """Return non-disabled projects sorted by most recently used first.

    Returns:
        List of active (non-disabled) projects.
    """
    active = [p for p in load_projects() if not p.disabled]
    active.sort(key=lambda p: p.last_used_at, reverse=True)
    return active
