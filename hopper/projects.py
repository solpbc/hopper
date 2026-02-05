# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Project management for hopper."""

import subprocess
from dataclasses import dataclass
from pathlib import Path

from hopper.config import load_config, save_config


@dataclass
class Project:
    """A registered project directory."""

    path: str  # Absolute path to git directory
    name: str  # Basename of directory
    disabled: bool = False  # True if removed but has existing sessions


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
        {"path": p.path, "name": p.name, "disabled": p.disabled} for p in projects
    ]
    save_config(config)


def add_project(path: str) -> Project:
    """Add a new project.

    Args:
        path: Path to git directory.

    Returns:
        The newly created Project.

    Raises:
        ValueError: If path is not a git directory or name already exists.
    """
    # Resolve to absolute path
    abs_path = str(Path(path).resolve())

    if not Path(abs_path).is_dir():
        raise ValueError(f"Not a directory: {abs_path}")

    if not validate_git_dir(abs_path):
        raise ValueError(f"Not a git repository: {abs_path}")

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
    """Get all non-disabled projects.

    Returns:
        List of active (non-disabled) projects.
    """
    return [p for p in load_projects() if not p.disabled]
