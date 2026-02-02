"""Shared pytest fixtures for all tests."""

import pytest

from hopper import config, sessions


@pytest.fixture(autouse=True)
def isolate_config(tmp_path, monkeypatch):
    """Isolate all tests from the real config directory.

    This fixture runs automatically for every test, ensuring that:
    - Sessions are never written to the real ~/.local/share/hopper/
    - Each test gets a fresh, isolated temporary directory
    - Test data cannot leak to production files

    The autouse=True ensures this runs even if developers forget to
    explicitly request the fixture.
    """
    # Patch both config module (source of truth) and sessions module (where it's imported)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SOCKET_PATH", tmp_path / "server.sock")
    monkeypatch.setattr(config, "SESSIONS_FILE", tmp_path / "sessions.jsonl")
    monkeypatch.setattr(config, "ARCHIVED_FILE", tmp_path / "archived.jsonl")
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path / "sessions")

    monkeypatch.setattr(sessions, "SESSIONS_FILE", tmp_path / "sessions.jsonl")
    monkeypatch.setattr(sessions, "ARCHIVED_FILE", tmp_path / "archived.jsonl")
    monkeypatch.setattr(sessions, "SESSIONS_DIR", tmp_path / "sessions")

    return tmp_path


@pytest.fixture
def temp_config(isolate_config):
    """Alias for isolate_config for tests that need the path.

    Tests can request this fixture to get the temporary config directory path.
    The isolation is already applied by the autouse isolate_config fixture.
    """
    return isolate_config
