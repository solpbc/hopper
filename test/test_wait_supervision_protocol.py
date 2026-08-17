# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Composed wait supervision tests across the real server protocol boundary."""

import json
import threading
from pathlib import Path

import pytest

from hopper import cli, wait
from hopper.deadline import make_deadline
from hopper.lodes import save_lodes
from hopper.server import Server
from hopper.tmux import Liveness


@pytest.fixture
def supervision_protocol(tmp_path, monkeypatch, make_lode, release_server_lock):
    socket_path = tmp_path / "supervision.sock"
    monkeypatch.setattr("hopper.server.pane_liveness", lambda *_args, **_kwargs: Liveness.UNKNOWN)
    monkeypatch.setattr("hopper.server.oom.find_systemctl", lambda: None)
    monkeypatch.setattr("hopper.server.get_active_projects", lambda: [])
    server = Server(socket_path)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    assert server.ready.wait(3), server.startup_error or "server did not become ready"

    class Harness:
        def seed(self, *lodes):
            server.lodes[:] = lodes
            server.archived_lodes[:] = []
            save_lodes(server.lodes)

        def lode(self, lode_id, **overrides):
            fields = {
                "id": lode_id,
                "state": "running",
                "status": "Working",
                "active": True,
                **overrides,
            }
            return make_lode(**fields)

        def run(self, query):
            return wait.wait_for_lode(
                Path(socket_path),
                query,
                deadline=make_deadline(2),
                poll_s=10,
                observer_timeout_s=1,
                json_output=True,
                resolver=cli._resolve_lode,
                probe_remote=cli._remote_lode_status,
            )

    try:
        yield Harness()
    finally:
        server.stop()
        thread.join(timeout=3)
        assert not thread.is_alive()
        release_server_lock(server)


@pytest.mark.parametrize(
    ("scenario", "expected_outcomes", "expected_exit"),
    [
        ("absent", ["target_absent"], 1),
        ("ambiguous", ["target_ambiguous"], 1),
        ("unavailable", ["status_unavailable"], 4),
        ("interrupted", ["interrupted"], 130),
    ],
)
def test_new_lifecycle_outcomes_cross_real_protocol(
    supervision_protocol,
    monkeypatch,
    capsys,
    scenario,
    expected_outcomes,
    expected_exit,
):
    harness = supervision_protocol
    query = "missing"
    monkeypatch.delenv("HOPPER_LID", raising=False)
    monkeypatch.setenv("HOP_NO_ROUTE", "1")
    if scenario == "ambiguous":
        harness.seed(harness.lode("prefix11"), harness.lode("prefix22"))
        query = "prefix"
    elif scenario == "unavailable":
        harness.seed()
        monkeypatch.delenv("HOP_NO_ROUTE", raising=False)
        monkeypatch.setattr(cli, "_remote_hosts", lambda **_kwargs: ["unavailable.example"])
        monkeypatch.setattr("hopper.remote.load_lode_cache", lambda **_kwargs: {})
        monkeypatch.setattr(
            "hopper.remote.run_remote",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("transport unavailable")),
        )
    elif scenario == "interrupted":
        harness.seed(harness.lode("running1"))
        query = "running1"
        monkeypatch.setattr(
            wait,
            "_condition_wait",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt),
        )
    else:
        harness.seed()

    result = harness.run(query)

    captured = capsys.readouterr()
    payloads = [json.loads(line) for line in captured.out.splitlines()]
    assert result == expected_exit
    assert [payload["outcome"] for payload in payloads] == expected_outcomes
    assert all(set(payload) == wait.FINAL_RECORD_KEYS for payload in payloads)
    assert all(payload["reason_code"] for payload in payloads)
    assert all(isinstance(payload["recovery"], str) for payload in payloads)
    assert captured.err.endswith(f"exited {expected_exit}\n")
