# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Durable compatibility tests for frozen pre-foundation stage-session bytes."""

import json
from pathlib import Path

import pytest

from hopper.lodes import (
    load_archived_lodes,
    load_lodes,
    lode_coder,
    lode_driver,
    lode_stage_session,
    save_archived_lodes,
    save_lodes,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "stage-sessions"


def _install_fixture(tmp_path: Path, name: str, *, archived: bool = False) -> bytes:
    """Copy frozen bytes to the real loader location and return their original form."""
    payload = (FIXTURE_DIR / name).read_bytes()
    target = tmp_path / ("archived.jsonl" if archived else "active.jsonl")
    target.write_bytes(payload)
    return payload


@pytest.mark.parametrize(
    ("name", "started"),
    [
        ("fresh-active-legacy.jsonl", False),
        ("started-legacy.jsonl", True),
    ],
)
def test_active_legacy_fixtures_materialize_deterministic_sessions(tmp_path, name, started):
    frozen = json.loads(_install_fixture(tmp_path, name))

    loaded = load_lodes()

    assert len(loaded) == 1
    assert lode_driver(loaded[0]) == "claude"
    assert lode_stage_session(loaded[0], "mill")["started"] is started
    assert loaded[0]["claude"] == frozen["claude"]
    identities = {
        stage: (
            lode_stage_session(loaded[0], stage)["provider_session_id"],
            lode_stage_session(loaded[0], stage)["launch_id"],
            lode_stage_session(loaded[0], stage)["started"],
        )
        for stage in ("mill", "refine", "ship")
    }

    save_lodes(loaded)
    first_roundtrip = (tmp_path / "active.jsonl").read_bytes()
    first_saved = json.loads(first_roundtrip)
    loaded_again = load_lodes()
    save_lodes(loaded_again)
    second_roundtrip = (tmp_path / "active.jsonl").read_bytes()
    second_saved = json.loads(second_roundtrip)

    assert second_roundtrip == first_roundtrip
    assert first_saved["claude"] == frozen["claude"]
    assert second_saved["claude"] == frozen["claude"]
    assert {
        stage: (
            lode_stage_session(loaded_again[0], stage)["provider_session_id"],
            lode_stage_session(loaded_again[0], stage)["launch_id"],
            lode_stage_session(loaded_again[0], stage)["started"],
        )
        for stage in ("mill", "refine", "ship")
    } == identities


@pytest.mark.parametrize(
    ("name", "action_id"),
    [
        ("direct-archived-legacy.jsonl", None),
        ("action-archived-legacy.jsonl", "capture-action"),
    ],
)
def test_archived_legacy_fixtures_materialize_without_losing_archive_shape(
    tmp_path, name, action_id
):
    frozen = json.loads(_install_fixture(tmp_path, name, archived=True))

    loaded = load_archived_lodes()

    assert len(loaded) == 1
    assert lode_driver(loaded[0]) == "claude"
    assert loaded[0]["archived_at"] is not None
    assert loaded[0]["archive_action_id"] == action_id
    assert lode_stage_session(loaded[0], "mill")["provider_session_id"]
    assert loaded[0]["claude"] == frozen["claude"]
    identities = {
        stage: (
            lode_stage_session(loaded[0], stage)["provider_session_id"],
            lode_stage_session(loaded[0], stage)["launch_id"],
            lode_stage_session(loaded[0], stage)["started"],
        )
        for stage in ("mill", "refine", "ship")
    }

    save_archived_lodes(loaded)
    first_roundtrip = (tmp_path / "archived.jsonl").read_bytes()
    first_saved = json.loads(first_roundtrip)
    loaded_again = load_archived_lodes()
    save_archived_lodes(loaded_again)
    second_roundtrip = (tmp_path / "archived.jsonl").read_bytes()
    second_saved = json.loads(second_roundtrip)

    assert second_roundtrip == first_roundtrip
    assert first_saved["claude"] == frozen["claude"]
    assert second_saved["claude"] == frozen["claude"]
    assert {
        stage: (
            lode_stage_session(loaded_again[0], stage)["provider_session_id"],
            lode_stage_session(loaded_again[0], stage)["launch_id"],
            lode_stage_session(loaded_again[0], stage)["started"],
        )
        for stage in ("mill", "refine", "ship")
    } == identities


def test_coder_fixture_keeps_refine_coder_session_independent(tmp_path):
    _install_fixture(tmp_path, "coder-null-session-legacy.jsonl")

    loaded = load_lodes()[0]

    assert lode_coder(loaded) == ("grok", None)
    assert lode_stage_session(loaded, "refine")["provider_session_id"]


def test_agreeing_hybrid_fixture_preserves_pinned_identities(tmp_path):
    payload = _install_fixture(tmp_path, "agreeing-hybrid.jsonl")
    frozen = json.loads(payload)

    loaded = load_lodes()[0]

    assert lode_driver(loaded) == frozen["driver"]
    for stage in ("mill", "refine", "ship"):
        assert lode_stage_session(loaded, stage) == frozen["stage_sessions"][stage]


@pytest.mark.parametrize(
    ("name", "match"),
    [
        ("canonical-missing-launch-id.jsonl", "launch_id"),
        ("hybrid-provider-session-conflict.jsonl", "provider_session_id"),
    ],
)
def test_invalid_frozen_shapes_fail_without_rewriting_bytes(tmp_path, name, match):
    original = _install_fixture(tmp_path, name)

    with pytest.raises(ValueError, match=match):
        load_lodes()

    assert (tmp_path / "active.jsonl").read_bytes() == original


@pytest.mark.parametrize(
    ("field", "mutate"),
    [
        ("driver", lambda record: record.__setitem__("driver", "other")),
        (
            "launch_id",
            lambda record: record["stage_sessions"]["mill"].__setitem__(
                "launch_id", "11111111-1111-1111-1111-111111111111"
            ),
        ),
        (
            "started",
            lambda record: record["stage_sessions"]["mill"].__setitem__("started", True),
        ),
    ],
)
def test_hybrid_conflicts_refuse_before_rewriting_bytes(tmp_path, field, mutate):
    record = json.loads((FIXTURE_DIR / "agreeing-hybrid.jsonl").read_bytes())
    mutate(record)
    target = tmp_path / "active.jsonl"
    original = (json.dumps(record) + "\n").encode()
    target.write_bytes(original)

    with pytest.raises(ValueError, match=field):
        load_lodes()

    assert target.read_bytes() == original
