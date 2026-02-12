# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for project management."""

import subprocess

import pytest

from hopper.config import load_config, save_config
from hopper.projects import (
    Project,
    add_project,
    find_project,
    get_active_projects,
    load_projects,
    remove_project,
    rename_project,
    rename_project_in_data,
    save_projects,
    touch_project,
    validate_git_dir,
)


@pytest.fixture
def mock_config(tmp_path):
    """Return the config file path (isolation handled by conftest)."""
    return tmp_path / "config.json"


@pytest.fixture
def git_dir(tmp_path):
    """Create a temporary git repository."""
    repo_path = tmp_path / "test-repo"
    repo_path.mkdir()
    subprocess.run(["git", "init"], cwd=repo_path, capture_output=True, check=True)
    return repo_path


@pytest.fixture
def non_git_dir(tmp_path):
    """Create a temporary non-git directory."""
    dir_path = tmp_path / "not-a-repo"
    dir_path.mkdir()
    return dir_path


# Tests for validate_git_dir


def test_validate_git_dir_valid(git_dir):
    """validate_git_dir returns True for git repository."""
    assert validate_git_dir(str(git_dir)) is True


def test_validate_git_dir_invalid(non_git_dir):
    """validate_git_dir returns False for non-git directory."""
    assert validate_git_dir(str(non_git_dir)) is False


def test_validate_git_dir_nonexistent(tmp_path):
    """validate_git_dir returns False for nonexistent path."""
    assert validate_git_dir(str(tmp_path / "nonexistent")) is False


# Tests for load_projects / save_projects


def test_load_projects_empty(mock_config):
    """load_projects returns empty list when no projects configured."""
    projects = load_projects()
    assert projects == []


def test_save_and_load_projects(mock_config):
    """save_projects and load_projects roundtrip."""
    projects = [
        Project(path="/path/to/foo", name="foo"),
        Project(path="/path/to/bar", name="bar", disabled=True),
    ]
    save_projects(projects)

    loaded = load_projects()
    assert len(loaded) == 2
    assert loaded[0].path == "/path/to/foo"
    assert loaded[0].name == "foo"
    assert loaded[0].disabled is False
    assert loaded[1].path == "/path/to/bar"
    assert loaded[1].name == "bar"
    assert loaded[1].disabled is True


def test_load_projects_preserves_other_config(mock_config):
    """save_projects preserves other config keys."""
    import json

    mock_config.write_text('{"name": "jer", "other": "value"}')

    projects = [Project(path="/path/to/foo", name="foo")]
    save_projects(projects)

    config = json.loads(mock_config.read_text())
    assert config["name"] == "jer"
    assert config["other"] == "value"
    assert "projects" in config


# Tests for add_project


def test_add_project_success(mock_config, git_dir):
    """add_project adds a valid git directory."""
    project = add_project(str(git_dir))

    assert project.name == "test-repo"
    assert project.path == str(git_dir)
    assert project.disabled is False

    # Verify persisted
    loaded = load_projects()
    assert len(loaded) == 1
    assert loaded[0].name == "test-repo"


def test_add_project_resolves_path(mock_config, git_dir, monkeypatch):
    """add_project resolves relative paths to absolute."""
    # Use a relative path
    monkeypatch.chdir(git_dir.parent)
    project = add_project("test-repo")

    assert project.path == str(git_dir)


def test_add_project_not_a_directory(mock_config, tmp_path):
    """add_project raises ValueError for non-directory."""
    file_path = tmp_path / "file.txt"
    file_path.write_text("content")

    with pytest.raises(ValueError, match="Not a directory"):
        add_project(str(file_path))


def test_add_project_not_git(mock_config, non_git_dir):
    """add_project raises ValueError for non-git directory."""
    with pytest.raises(ValueError, match="Not a git repository"):
        add_project(str(non_git_dir))


def test_add_project_duplicate_name(mock_config, git_dir, tmp_path):
    """add_project raises ValueError for duplicate project name."""
    # Add the first project
    add_project(str(git_dir))

    # Create another repo with same basename
    other_repo = tmp_path / "other" / "test-repo"
    other_repo.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=other_repo, capture_output=True, check=True)

    with pytest.raises(ValueError, match="already exists"):
        add_project(str(other_repo))


def test_add_project_duplicate_includes_disabled(mock_config, git_dir, tmp_path):
    """add_project rejects duplicates even if existing project is disabled."""
    # Add and disable the first project
    add_project(str(git_dir))
    remove_project("test-repo")

    # Create another repo with same basename
    other_repo = tmp_path / "other" / "test-repo"
    other_repo.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=other_repo, capture_output=True, check=True)

    with pytest.raises(ValueError, match="already exists"):
        add_project(str(other_repo))


# Tests for remove_project


def test_remove_project_success(mock_config, git_dir):
    """remove_project disables the project."""
    add_project(str(git_dir))

    result = remove_project("test-repo")
    assert result is True

    # Verify disabled but still in list
    loaded = load_projects()
    assert len(loaded) == 1
    assert loaded[0].disabled is True


def test_remove_project_not_found(mock_config):
    """remove_project returns False for unknown project."""
    result = remove_project("nonexistent")
    assert result is False


# Tests for rename_project


def test_rename_project_success(mock_config, git_dir):
    """rename_project changes the project name."""
    add_project(str(git_dir))
    rename_project("test-repo", "new-name")

    projects = load_projects()
    assert len(projects) == 1
    assert projects[0].name == "new-name"
    assert projects[0].path == str(git_dir)  # path unchanged


def test_rename_project_not_found(mock_config):
    """rename_project raises ValueError for unknown project."""
    with pytest.raises(ValueError, match="not found"):
        rename_project("nonexistent", "new-name")


def test_rename_project_disabled(mock_config, git_dir):
    """rename_project raises ValueError for disabled project."""
    add_project(str(git_dir))
    remove_project("test-repo")
    with pytest.raises(ValueError, match="disabled"):
        rename_project("test-repo", "new-name")


def test_rename_project_duplicate_new_name(mock_config, tmp_path):
    """rename_project raises ValueError if new name already exists."""
    repo1 = tmp_path / "repo1"
    repo1.mkdir()
    subprocess.run(["git", "init"], cwd=repo1, capture_output=True, check=True)
    repo2 = tmp_path / "repo2"
    repo2.mkdir()
    subprocess.run(["git", "init"], cwd=repo2, capture_output=True, check=True)
    add_project(str(repo1))
    add_project(str(repo2))
    with pytest.raises(ValueError, match="already exists"):
        rename_project("repo1", "repo2")


def test_rename_project_in_data(mock_config, git_dir):
    """rename_project_in_data updates project name in all data files."""
    from hopper.backlog import BacklogItem, load_backlog, save_backlog
    from hopper.lodes import (
        load_archived_lodes,
        load_lodes,
        save_archived_lodes,
        save_lodes,
    )

    add_project(str(git_dir))

    # Create active lodes
    active = [
        {"id": "aaa", "project": "test-repo", "state": "running"},
        {"id": "bbb", "project": "other", "state": "running"},
    ]
    save_lodes(active)

    # Create archived lodes
    archived = [
        {"id": "ccc", "project": "test-repo", "state": "done"},
    ]
    save_archived_lodes(archived)

    # Create backlog items
    items = [
        BacklogItem(id="d1", project="test-repo", description="task 1", created_at=1000),
        BacklogItem(id="d2", project="other", description="task 2", created_at=2000),
    ]
    save_backlog(items)

    rename_project_in_data("test-repo", "new-name")

    # Verify active lodes updated
    lodes = load_lodes()
    assert lodes[0]["project"] == "new-name"
    assert lodes[1]["project"] == "other"  # unchanged

    # Verify archived lodes updated
    arch = load_archived_lodes()
    assert arch[0]["project"] == "new-name"

    # Verify backlog updated
    bl = load_backlog()
    assert bl[0].project == "new-name"
    assert bl[1].project == "other"  # unchanged


# Tests for find_project


def test_find_project_found(mock_config, git_dir):
    """find_project returns the project when found."""
    add_project(str(git_dir))

    project = find_project("test-repo")
    assert project is not None
    assert project.name == "test-repo"


def test_find_project_not_found(mock_config):
    """find_project returns None for unknown project."""
    project = find_project("nonexistent")
    assert project is None


def test_find_project_includes_disabled(mock_config, git_dir):
    """find_project finds disabled projects."""
    add_project(str(git_dir))
    remove_project("test-repo")

    project = find_project("test-repo")
    assert project is not None
    assert project.disabled is True


# Tests for get_active_projects


def test_get_active_projects_filters_disabled(mock_config, git_dir, tmp_path):
    """get_active_projects excludes disabled projects."""
    # Create two repos
    other_repo = tmp_path / "other-repo"
    other_repo.mkdir()
    subprocess.run(["git", "init"], cwd=other_repo, capture_output=True, check=True)

    add_project(str(git_dir))
    add_project(str(other_repo))
    remove_project("test-repo")

    active = get_active_projects()
    assert len(active) == 1
    assert active[0].name == "other-repo"


def test_get_active_projects_empty(mock_config):
    """get_active_projects returns empty list when no projects."""
    active = get_active_projects()
    assert active == []


def test_load_save_roundtrip_last_used_at(mock_config, git_dir):
    """last_used_at survives save/load roundtrip."""
    add_project(str(git_dir))
    projects = load_projects()
    projects[0].last_used_at = 12345
    save_projects(projects)
    reloaded = load_projects()
    assert reloaded[0].last_used_at == 12345


def test_touch_project(mock_config, git_dir, monkeypatch):
    """touch_project sets last_used_at to current time."""
    add_project(str(git_dir))
    monkeypatch.setattr("hopper.projects.current_time_ms", lambda: 99999)
    touch_project("test-repo")
    projects = load_projects()
    assert projects[0].last_used_at == 99999


def test_get_active_projects_sorted_by_last_used(mock_config, tmp_path):
    """get_active_projects returns most recently used first."""
    # Create three repos
    for name in ["alpha", "beta", "gamma"]:
        repo = tmp_path / name
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
        add_project(str(repo))

    # Set different last_used_at timestamps
    projects = load_projects()
    projects[0].last_used_at = 100  # alpha
    projects[1].last_used_at = 300  # beta
    projects[2].last_used_at = 200  # gamma
    save_projects(projects)

    active = get_active_projects()
    assert [p.name for p in active] == ["beta", "gamma", "alpha"]


def test_load_projects_missing_last_used_at(mock_config, git_dir):
    """Projects without last_used_at field default to 0."""
    add_project(str(git_dir))
    # Manually remove last_used_at from config
    config = load_config()
    for p in config["projects"]:
        p.pop("last_used_at", None)
    save_config(config)
    projects = load_projects()
    assert projects[0].last_used_at == 0
