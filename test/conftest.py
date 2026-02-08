# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Shared pytest fixtures for all tests."""

import pytest

from hopper import config


@pytest.fixture(autouse=True)
def isolate_config(tmp_path, monkeypatch):
    """Isolate all tests from the real config directory.

    Redirects hopper_dir() to a temporary directory so all file paths
    (lodes, backlog, config, socket) resolve there automatically.
    """
    monkeypatch.setattr(config, "hopper_dir", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def temp_config(isolate_config):
    """Alias for isolate_config for tests that need the path.

    Tests can request this fixture to get the temporary config directory path.
    The isolation is already applied by the autouse isolate_config fixture.
    """
    return isolate_config


@pytest.fixture
def make_lode():
    """Factory for creating lode dicts with all default fields.

    Returns a callable that creates lode dicts. Override any field via kwargs.
    """

    def _make(auto=True, **overrides):
        lode = {
            "id": "testid11",
            "stage": "mill",
            "created_at": 1000,
            "updated_at": 1000,
            "project": "",
            "scope": "",
            "state": "new",
            "status": "",
            "title": "",
            "branch": "",
            "active": False,
            "auto": auto,
            "tmux_pane": None,
            "pid": None,
            "codex_thread_id": None,
            "backlog": None,
            "claude": {
                "mill": {"session_id": "00000000-0000-0000-0000-000000000001", "started": False},
                "refine": {"session_id": "00000000-0000-0000-0000-000000000002", "started": False},
                "ship": {"session_id": "00000000-0000-0000-0000-000000000003", "started": False},
            },
            **overrides,
        }
        return lode

    return _make
