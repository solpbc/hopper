# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Durable action-result fixture validation tests."""

import copy
import json
from pathlib import Path

import pytest

from hopper import actions

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "action-results"


def _load_fixture(filename: str):
    path = FIXTURES_DIR / filename
    return json.loads(path.read_text(encoding="utf-8"))


def test_action_result_fixtures_validate_unmodified():
    single_fixtures = [
        "manual-pause-ste6ema2.json",
        "manual-restart-uudsl5ig.json",
        "manual-kill-rjklpm77.json",
        "manual-archive-bvvqtsmb.json",
    ]
    for filename in single_fixtures:
        receipt = _load_fixture(filename)
        assert actions._validate_action_result(receipt) is receipt

    shipped_list = _load_fixture("shipped-lode-jmqs2lah.json")
    assert isinstance(shipped_list, list)
    for receipt in shipped_list:
        assert actions._validate_action_result(receipt) is receipt


def test_action_result_fixture_list_find_and_append_tri_state():
    shipped_list = _load_fixture("shipped-lode-jmqs2lah.json")
    lode = {"id": "jmqs2lah", "action_results": copy.deepcopy(shipped_list)}

    for item in shipped_list:
        found = actions.find_action_result(lode, item["action_id"])
        assert found == item

    new_receipt = {
        "schema_version": 2,
        "action_id": "11112222333344445555666677778888",
        "lode_id": "jmqs2lah",
        "expected_generation": "99990000111122223333444455556666",
        "action_type": "completion",
        "target_disposition": "advance_refine",
        "force_consent": False,
        "terminal_disposition": "advance_refine",
        "completed_at_ms": 1789600000000,
        "containment_proof": "strict Linux containment proven",
        "retained": {
            "worktree": None,
            "branch": None,
            "session": None,
        },
        "successor": None,
    }
    actions.append_action_result(lode, new_receipt)
    assert len(lode["action_results"]) == 4
    assert lode["action_results"][-1] == new_receipt
    assert actions._validate_action_result(new_receipt) is new_receipt


def test_action_result_fixture_mutation_refusals():
    receipt = _load_fixture("manual-pause-ste6ema2.json")

    mutated = copy.deepcopy(receipt)
    mutated["retained"]["worktree"] = "unknown"
    with pytest.raises(ValueError, match="must be a boolean"):
        actions._validate_action_result(mutated)

    mutated = copy.deepcopy(receipt)
    mutated["retained"]["worktree"] = 0
    with pytest.raises(ValueError, match="must be a boolean"):
        actions._validate_action_result(mutated)

    mutated = copy.deepcopy(receipt)
    del mutated["retained"]["branch"]
    with pytest.raises(ValueError, match="missing keys"):
        actions._validate_action_result(mutated)

    mutated = copy.deepcopy(receipt)
    mutated["retained"]["extra"] = True
    with pytest.raises(ValueError, match="unknown keys"):
        actions._validate_action_result(mutated)


def test_action_result_fixtures_json_roundtrip():
    single_fixtures = [
        "manual-pause-ste6ema2.json",
        "manual-restart-uudsl5ig.json",
        "manual-kill-rjklpm77.json",
        "manual-archive-bvvqtsmb.json",
    ]
    for filename in single_fixtures:
        receipt = _load_fixture(filename)
        roundtripped = json.loads(json.dumps(receipt))
        assert roundtripped == receipt
        assert actions._validate_action_result(roundtripped) == receipt

    shipped_list = _load_fixture("shipped-lode-jmqs2lah.json")
    roundtripped_list = json.loads(json.dumps(shipped_list))
    assert roundtripped_list == shipped_list
    for item in roundtripped_list:
        assert actions._validate_action_result(item) == item
