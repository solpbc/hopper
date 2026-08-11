# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Hermetic replacement-server composition tests for truthful wait authority."""

import json
import socket
import threading
from pathlib import Path

import pytest

from hopper import actions, wait
from hopper.client import read_lode_snapshot
from hopper.deadline import make_deadline
from hopper.lodes import load_lodes, save_lodes, touch
from hopper.server import Server
from hopper.tmux import Liveness

GENERATION = "a" * 32


@pytest.fixture
def restart_harness(tmp_path, monkeypatch, make_lode, release_server_lock):
    """Run replaceable real servers with deterministic external identity evidence."""
    socket_path = tmp_path / "restart.sock"
    liveness_by_pane: dict[str, Liveness] = {}
    wait_connections = 0
    wait_connection_condition = threading.Condition()
    reconnect_observed = threading.Event()
    runner_sockets: list[socket.socket] = []
    wait_runs: list[dict] = []

    real_connection = wait.client.HopperConnection
    real_read_snapshot = read_lode_snapshot

    class ObservedWaitConnection(real_connection):
        def start(self, callback=None, on_connect=None, *, deadline=None):
            def observed_connect():
                nonlocal wait_connections
                with wait_connection_condition:
                    wait_connections += 1
                    wait_connection_condition.notify_all()
                if on_connect:
                    on_connect()

            super().start(callback=callback, on_connect=observed_connect, deadline=deadline)

    def observed_read(path, prefix, timeout=2.0, *, deadline=None):
        result = real_read_snapshot(path, prefix, timeout, deadline=deadline)
        if (
            result[0] == "found"
            and isinstance(result[1], dict)
            and result[1].get("state") == "reconnecting"
        ):
            reconnect_observed.set()
        return result

    def synchronous_registration(server, kind, lode, message, conn):
        assert kind == "worker"
        accepted = server._register_lode_client(
            lode["id"],
            conn,
            message.get("tmux_pane"),
            message.get("pid"),
            message.get("run_generation"),
            proof_mode="other-bounded-no-birth",
        )
        server._send_response(
            conn,
            {
                "type": "lode_registered" if accepted else "lode_register_refused",
                "lode_id": lode["id"],
                "accepted": accepted,
            },
        )

    monkeypatch.setattr(
        "hopper.server.pane_liveness",
        lambda pane: liveness_by_pane.get(pane, Liveness.UNKNOWN),
    )
    monkeypatch.setattr("hopper.server.oom.find_systemctl", lambda: None)
    monkeypatch.setattr("hopper.server.get_active_projects", lambda: [])
    monkeypatch.setattr(Server, "_start_registration_capture", synchronous_registration)
    monkeypatch.setattr(wait.client, "HopperConnection", ObservedWaitConnection)
    monkeypatch.setattr(wait.client, "read_lode_snapshot", observed_read)

    class Harness:
        server: Server | None = None
        server_thread: threading.Thread | None = None

        def start_server(self) -> Server:
            server = Server(socket_path)
            thread = threading.Thread(target=server.start, daemon=True)
            thread.start()
            assert server.ready.wait(3), server.startup_error or "server did not become ready"
            self.server = server
            self.server_thread = thread
            return server

        def stop_server(self) -> None:
            if self.server is None:
                return
            self.server.stop()
            assert self.server_thread is not None
            self.server_thread.join(timeout=3)
            assert not self.server_thread.is_alive()
            release_server_lock(self.server)
            self.server = None
            self.server_thread = None

        def seed(self, *lodes: dict) -> None:
            assert self.server is not None
            self.server.lodes[:] = lodes
            save_lodes(self.server.lodes)

        def replace(self, mutate_during_disconnect=None) -> Server:
            self.stop_server()
            if mutate_during_disconnect:
                durable = load_lodes()
                mutate_during_disconnect(durable)
                save_lodes(durable)
            return self.start_server()

        def set_liveness(self, **values: Liveness) -> None:
            liveness_by_pane.update({f"%{pane}": value for pane, value in values.items()})

        def snapshot(self, lode_id: str) -> dict:
            status, payload = real_read_snapshot(socket_path, lode_id)
            assert status == "found", payload
            assert isinstance(payload, dict)
            return payload

        def assert_allowed_snapshots(self) -> None:
            assert self.server is not None
            for stored in [*self.server.lodes, *self.server.archived_lodes]:
                snapshot = self.snapshot(stored["id"])
                pending = snapshot.get("pending_action")
                valid_action = isinstance(pending, dict) and (
                    pending.get("phase") in actions.PHASES
                    or (
                        pending.get("phase") == "blocked"
                        and pending.get("action_type") in {"invalid", "legacy-v1"}
                    )
                )
                categories = [
                    snapshot.get("archived") is True
                    or snapshot.get("stage") == "shipped"
                    or snapshot.get("state") in {"error", "gated"},
                    snapshot.get("active") is True and stored["id"] in self.server.lode_clients,
                    snapshot.get("active") is False
                    and pending is None
                    and snapshot.get("state") == "reconnecting",
                    snapshot.get("active") is False
                    and pending is None
                    and snapshot.get("state") == "new",
                    snapshot.get("active") is False
                    and pending is None
                    and snapshot.get("state") == "ready",
                    snapshot.get("active") is False and snapshot.get("state") == "stuck",
                    snapshot.get("active") is False and valid_action,
                ]
                assert sum(categories) == 1, snapshot

        def register_runner(self, lode_id: str, pane: str) -> socket.socket:
            runner = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            runner.settimeout(3)
            runner.connect(str(socket_path))
            exchange_id = f"register-{lode_id}"
            runner.sendall(
                (
                    json.dumps(
                        {
                            "type": "lode_register",
                            "exchange_id": exchange_id,
                            "lode_id": lode_id,
                            "run_generation": GENERATION,
                            "tmux_pane": pane,
                            "pid": 4242,
                        }
                    )
                    + "\n"
                ).encode()
            )
            buffer = b""
            while True:
                buffer += runner.recv(4096)
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    response = json.loads(line)
                    if response.get("exchange_id") == exchange_id:
                        assert response["type"] == "lode_registered"
                        assert response["accepted"] is True
                        runner_sockets.append(runner)
                        return runner

        def ship_from_runner(self, runner: socket.socket, lode_id: str) -> None:
            runner.sendall(
                (
                    json.dumps(
                        {
                            "type": "lode_set_stage",
                            "lode_id": lode_id,
                            "run_generation": GENERATION,
                            "stage": "shipped",
                        }
                    )
                    + "\n"
                ).encode()
            )

        def start_wait(self, lode_id: str) -> dict:
            run = {"done": threading.Event(), "resolved": threading.Event(), "result": None}

            def resolver(path: Path, prefix: str, **_kwargs) -> dict:
                status, payload = real_read_snapshot(path, prefix)
                run["resolved"].set()
                probes = [
                    {
                        "kind": "local",
                        "server": "test-host",
                        "route": "local",
                        "candidate_id": None,
                        "outcome": status,
                        "detail": None,
                        "attempts": 1,
                        "observed_age_s": 0.0,
                    }
                ]
                if status == "found":
                    return {
                        "outcome": "found",
                        "lode": payload,
                        "host": "local",
                        "canonical_id": payload["id"],
                        "probes": probes,
                        "exit_code": 0,
                    }
                return {
                    "outcome": "unavailable" if status == "unavailable" else status,
                    "error": str(payload),
                    "probes": probes,
                    "exit_code": 2 if status == "unavailable" else 1,
                }

            def run_wait() -> None:
                run["result"] = wait.wait_for_lodes(
                    socket_path,
                    [lode_id],
                    deadline=make_deadline(5),
                    poll_s=30,
                    observer_timeout_s=5,
                    json_output=True,
                    resolver=resolver,
                    probe_remote=lambda *args, **kwargs: (None, "unreadable"),
                )
                run["done"].set()

            thread = threading.Thread(target=run_wait, daemon=True)
            run["thread"] = thread
            wait_runs.append(run)
            thread.start()
            assert run["resolved"].wait(3)
            return run

        def wait_for_observer_connections(self, count: int) -> None:
            with wait_connection_condition:
                assert wait_connection_condition.wait_for(
                    lambda: wait_connections >= count,
                    timeout=3,
                )

        def wait_for_reconnecting_observation(self) -> None:
            assert reconnect_observed.wait(3)

        def finish_wait(self, run: dict, expected: int) -> None:
            assert run["done"].wait(3)
            run["thread"].join(timeout=1)
            assert run["result"] == expected

    harness = Harness()
    harness.start_server()
    try:
        yield harness
    finally:
        for runner in runner_sockets:
            runner.close()
        for run in wait_runs:
            if not run["done"].is_set() and harness.server is not None:
                for lode in harness.server.lodes:
                    lode["stage"] = "shipped"
                    touch(lode)
                save_lodes(harness.server.lodes)
                harness.server.broadcast({"type": "lode_updated", "lode": harness.server.lodes[0]})
                run["done"].wait(1)
        harness.stop_server()


def _runner_lode(make_lode, lode_id: str, pane: str, **overrides) -> dict:
    fields = {
        "id": lode_id,
        "state": "running",
        "status": "Working",
        "active": True,
        "tmux_pane": pane,
        "pid": 1234,
        "run_generation": GENERATION,
        **overrides,
    }
    return make_lode(**fields)


def test_observer_first_reconnect_recovers_after_durable_disconnect_mutation(
    restart_harness, make_lode
):
    lode = _runner_lode(make_lode, "observe1", "%1")
    restart_harness.seed(lode)
    wait_run = restart_harness.start_wait(lode["id"])
    restart_harness.wait_for_observer_connections(1)
    restart_harness.set_liveness(**{"1": Liveness.ALIVE})
    server = restart_harness.replace(
        lambda rows: rows[0].update(status="Changed while the server was down")
    )
    restart_harness.wait_for_observer_connections(2)
    restart_harness.wait_for_reconnecting_observation()

    reconnecting = restart_harness.snapshot(lode["id"])
    assert reconnecting["reconnect_prior_status"] == "Changed while the server was down"
    restart_harness.assert_allowed_snapshots()
    runner = restart_harness.register_runner(lode["id"], "%1")
    assert (server.lodes[0]["state"], server.lodes[0]["active"]) == ("running", True)
    restart_harness.assert_allowed_snapshots()
    restart_harness.ship_from_runner(runner, lode["id"])
    restart_harness.finish_wait(wait_run, 0)


def test_runner_first_reconnect_recovers_before_wait_attaches(restart_harness, make_lode):
    lode = _runner_lode(make_lode, "runner11", "%2")
    restart_harness.seed(lode)
    restart_harness.set_liveness(**{"2": Liveness.ALIVE})
    server = restart_harness.replace()

    runner = restart_harness.register_runner(lode["id"], "%2")
    assert (server.lodes[0]["state"], server.lodes[0]["active"]) == ("running", True)
    restart_harness.assert_allowed_snapshots()
    wait_run = restart_harness.start_wait(lode["id"])
    restart_harness.ship_from_runner(runner, lode["id"])
    restart_harness.finish_wait(wait_run, 0)


def test_persistent_reconnect_confirms_bounded_wait_outcome(
    restart_harness, make_lode, monkeypatch
):
    lode = _runner_lode(make_lode, "stall111", "%3")
    restart_harness.seed(lode)
    restart_harness.set_liveness(**{"3": Liveness.ALIVE})
    restart_harness.replace()
    restart_harness.assert_allowed_snapshots()
    monkeypatch.setattr(wait, "STUCK_GRACE_MS", 0)

    wait_run = restart_harness.start_wait(lode["id"])
    restart_harness.finish_wait(wait_run, 1)


def test_unknown_inventory_gates_every_ordinary_registration_grace(restart_harness, make_lode):
    rows = [
        _runner_lode(make_lode, "ord11111", "%4"),
        _runner_lode(make_lode, "new11111", "%5", state="new"),
        _runner_lode(make_lode, "ready111", "%6", state="ready"),
    ]
    restart_harness.seed(*rows)
    restart_harness.set_liveness(
        **{"4": Liveness.UNKNOWN, "5": Liveness.UNKNOWN, "6": Liveness.UNKNOWN}
    )
    restart_harness.replace()

    for row in rows:
        snapshot = restart_harness.snapshot(row["id"])
        assert (snapshot["state"], snapshot["active"], snapshot["spawn_disposition"]) == (
            "gated",
            False,
            "unknown",
        )
        assert "Do not restart" in snapshot["status"]
    restart_harness.assert_allowed_snapshots()


def test_live_new_and_ready_retain_their_bounded_graces(restart_harness, make_lode):
    rows = [
        _runner_lode(make_lode, "new22222", "%7", state="new"),
        _runner_lode(make_lode, "ready222", "%8", state="ready"),
    ]
    restart_harness.seed(*rows)
    restart_harness.set_liveness(**{"7": Liveness.ALIVE, "8": Liveness.ALIVE})
    restart_harness.replace()

    for row in rows:
        snapshot = restart_harness.snapshot(row["id"])
        assert (snapshot["state"], snapshot["active"]) == (row["state"], False)
    restart_harness.assert_allowed_snapshots()


def test_gone_inventory_uses_safe_failure_or_ready_handoff(restart_harness, make_lode):
    rows = [
        _runner_lode(make_lode, "gone1111", "%9"),
        _runner_lode(make_lode, "new33333", "%10", state="new"),
        _runner_lode(make_lode, "ready333", "%11", state="ready"),
    ]
    restart_harness.seed(*rows)
    restart_harness.set_liveness(**{"9": Liveness.GONE, "10": Liveness.GONE, "11": Liveness.GONE})
    restart_harness.replace()

    assert restart_harness.snapshot("gone1111")["state"] == "error"
    assert restart_harness.snapshot("new33333")["state"] == "error"
    ready = restart_harness.snapshot("ready333")
    assert (ready["state"], ready["active"]) == ("ready", False)
    assert "deliberate handoff" in ready["status"]
    restart_harness.assert_allowed_snapshots()


def test_stuck_and_action_rows_retain_stronger_inactive_authority(restart_harness, make_lode):
    stuck = _runner_lode(make_lode, "stuck111", "%12", state="stuck")
    action = _runner_lode(
        make_lode,
        "action11",
        "%13",
        state="teardown",
        pending_action={"phase": "blocked", "action_type": "invalid", "status": "Repair"},
    )
    restart_harness.seed(stuck, action)
    restart_harness.set_liveness(**{"12": Liveness.UNKNOWN, "13": Liveness.GONE})
    restart_harness.replace()

    stuck_snapshot = restart_harness.snapshot(stuck["id"])
    action_snapshot = restart_harness.snapshot(action["id"])
    assert (stuck_snapshot["state"], stuck_snapshot["active"]) == ("stuck", False)
    assert (action_snapshot["state"], action_snapshot["active"]) == ("teardown", False)
    assert action_snapshot["pending_action"]["action_type"] == "invalid"
    restart_harness.assert_allowed_snapshots()
