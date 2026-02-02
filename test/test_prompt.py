"""Tests for prompt loader."""

import pytest

from hopper import prompt


def test_load_existing_prompt():
    """Loading an existing prompt returns its content."""
    content = prompt.load("shovel")
    assert content == "hello, what's your name?"


def test_load_with_md_extension():
    """Loading with .md extension works the same."""
    content = prompt.load("shovel.md")
    assert content == "hello, what's your name?"


def test_load_nonexistent_prompt():
    """Loading a nonexistent prompt raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError, match="Prompt not found: nope.md"):
        prompt.load("nope")


def test_load_strips_trailing_whitespace():
    """Loaded prompts have trailing whitespace stripped."""
    content = prompt.load("shovel")
    assert content == content.strip()
