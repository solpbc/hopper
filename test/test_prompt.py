# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for prompt loader."""

import pytest

from hopper import prompt


@pytest.fixture
def mock_config(tmp_path):
    """Return the config file path (isolation handled by conftest)."""
    return tmp_path / "config.json"


def test_load_existing_prompt(mock_config):
    """Loading an existing prompt returns its content."""
    content = prompt.load("mill")
    assert "mill" in content.lower()  # Contains expected keyword


def test_load_with_md_extension(mock_config):
    """Loading with .md extension works the same."""
    content = prompt.load("mill.md")
    assert "mill" in content.lower()  # Contains expected keyword


def test_load_nonexistent_prompt(mock_config):
    """Loading a nonexistent prompt raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError, match="Prompt not found: nope.md"):
        prompt.load("nope")


def test_load_strips_trailing_whitespace(mock_config):
    """Loaded prompts have trailing whitespace stripped."""
    content = prompt.load("mill")
    assert content == content.strip()


def test_load_with_context_substitutes_variables(tmp_path, monkeypatch, mock_config):
    """Context variables are substituted in the prompt."""
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "greet.md").write_text("Hello, $name!")
    monkeypatch.setattr(prompt, "PROMPTS_DIR", prompts_dir)

    content = prompt.load("greet", context={"name": "Alice"})
    assert content == "Hello, Alice!"


def test_load_with_context_provides_uppercase_first(tmp_path, monkeypatch, mock_config):
    """Context variables also provide uppercase-first versions."""
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "greet.md").write_text("$Name said hello")
    monkeypatch.setattr(prompt, "PROMPTS_DIR", prompts_dir)

    content = prompt.load("greet", context={"name": "bob"})
    assert content == "Bob said hello"


def test_load_undefined_variables_unchanged(tmp_path, monkeypatch, mock_config):
    """Undefined variables are left as-is (safe_substitute behavior)."""
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "test.md").write_text("Hello $undefined")
    monkeypatch.setattr(prompt, "PROMPTS_DIR", prompts_dir)

    content = prompt.load("test")
    assert content == "Hello $undefined"


def test_load_uses_config_values(tmp_path, monkeypatch, mock_config):
    """Config values are available as template variables."""
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "greet.md").write_text("Hello, $name!")
    mock_config.write_text('{"name": "jer"}')
    monkeypatch.setattr(prompt, "PROMPTS_DIR", prompts_dir)

    content = prompt.load("greet")
    assert content == "Hello, jer!"


def test_load_context_overrides_config(tmp_path, monkeypatch, mock_config):
    """Context values override config values."""
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "greet.md").write_text("Hello, $name!")
    mock_config.write_text('{"name": "config-name"}')
    monkeypatch.setattr(prompt, "PROMPTS_DIR", prompts_dir)

    content = prompt.load("greet", context={"name": "context-name"})
    assert content == "Hello, context-name!"


def test_load_skips_non_string_config_values(tmp_path, monkeypatch, mock_config):
    """Non-string config values (lists, dicts) are skipped without error."""
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "greet.md").write_text("Hello, $name! Projects: $projects")
    mock_config.write_text('{"name": "jer", "projects": [{"path": "/foo"}]}')
    monkeypatch.setattr(prompt, "PROMPTS_DIR", prompts_dir)

    content = prompt.load("greet")
    assert content == "Hello, jer! Projects: $projects"
