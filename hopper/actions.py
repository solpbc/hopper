# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Durable storage primitives for accepted lode actions."""

import hashlib
import json
import os
import re
import secrets
import time
import uuid
from pathlib import Path

from hopper import config

# Fleet-cutover ABI: changing this name would orphan an in-flight v1 fence.
PENDING_ACTION_FILENAME = "pending-completion.json"
COMPLETION_STAGING_DIRNAME = "completion-staging"
RUN_OWNERSHIP_PREFIX = "run-ownership-"
SCHEMA_VERSION = 2
RUN_OWNERSHIP_SCHEMA_VERSION = 1
SPAWN_RECEIPT_SCHEMA_VERSION = 1
ACTION_RESULT_LIMIT = 8
DIGEST_ALGORITHM = "sha256"
POLL_INTERVAL_MS = 50

STAGES = {"mill", "refine", "ship"}
ACTION_TYPES = {"completion", "pause", "restart", "kill", "archive"}
TARGET_DISPOSITIONS = {
    "completion": {"advance_refine", "advance_ship", "shipped_archived"},
    "pause": {"paused"},
    "restart": {"replacement_spawned"},
    "kill": {"killed_archived"},
    "archive": {"archived"},
}
PHASES = {
    "accepted",
    "output_blocked",
    "publishing_output",
    "capturing_ownership",
    "closing_pane",
    "observing_containment",
    "force_killing",
    "containment_blocked",
    "proving_ship_landing",
    "ship_blocked",
    "quarantining",
    "checking_durability",
    "rechecking_durability",
    "durability_blocked",
    "publishing_terminal",
    "spawning",
    "cleanup_blocked",
    "complete",
}
MARKER_NAMES = (
    "output_publish",
    "ownership_capture",
    "pane_close",
    "containment",
    "scope_kill",
    "supervisor_kill",
    "ship_landing",
    "quarantine_rename",
    "worktree_repair",
    "cleanup_authorization",
    "durability_recheck",
    "lode_mutation",
    "archive",
    "backlog",
    "spawn",
    "worktree_remove",
    "branch_delete",
    "pending_clear",
)
MARKER_STATES = {"not_started", "intent", "done", "blocked"}
MARKER_TRANSITIONS = {
    "not_started": {"intent"},
    "intent": {"done", "blocked"},
    "blocked": {"intent"},
    "done": set(),
}

_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_HEX_OID = re.compile(r"[0-9a-f]{40,64}\Z")
_LODE_ID = re.compile(r"[a-z2-7]{8}\Z")
_REPAIR_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_STAGED_PATH = re.compile(r"completion-staging/([0-9a-f]{32})\.blob\Z")
_OWNERSHIP_PATH = re.compile(r"run-ownership-([0-9a-f]{32})\.json\Z")
_SPAWN_PATH = re.compile(r"spawn-([0-9a-f]{32})\.json\Z")
_STAGING_FILE = re.compile(r"[0-9a-f]{32}\.blob(?:\.[0-9a-f]{32}\.tmp)?\Z")


def lode_dir(lode_id: str) -> Path:
    """Return the persistent directory for one lode."""
    if not isinstance(lode_id, str) or _LODE_ID.fullmatch(lode_id) is None:
        raise ValueError("lode ID must be eight lowercase base32 characters")
    return config.hopper_dir() / "lodes" / lode_id


def pending_action_path(lode_id: str) -> Path:
    """Return the canonical pending-action path for a lode."""
    return lode_dir(lode_id) / PENDING_ACTION_FILENAME


def staging_dir(lode_id: str) -> Path:
    """Return the server-owned completion staging directory for a lode."""
    return lode_dir(lode_id) / COMPLETION_STAGING_DIRNAME


def run_ownership_path(lode_id: str, run_generation: str) -> Path:
    """Return the durable launch-ownership path for one generation."""
    if _HEX32.fullmatch(run_generation) is None:
        raise ValueError("run generation must be 32 lowercase hexadecimal characters")
    return lode_dir(lode_id) / f"{RUN_OWNERSHIP_PREFIX}{run_generation}.json"


def spawn_receipt_path(lode_id: str, action_id: str) -> Path:
    """Return the action-scoped pane bootstrap receipt path."""
    if _HEX32.fullmatch(action_id) is None:
        raise ValueError("action ID must be 32 lowercase hexadecimal characters")
    return lode_dir(lode_id) / f"spawn-{action_id}.json"


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _durable_replace(path: Path, writer) -> None:
    """Populate and durably replace a file using a writer-unique sibling."""
    parent_existed = path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not parent_existed:
        _fsync_directory(path.parent.parent)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    fd = None
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as output:
            fd = None
            writer(output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception:
        if fd is not None:
            os.close(fd)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _canonical_json_bytes(value: dict) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def write_durable_json(path: Path, value: dict) -> None:
    """Write canonical JSON with file and containing-directory durability."""
    payload = _canonical_json_bytes(value)
    _durable_replace(path, lambda output: output.write(payload))


def durable_json_sha256(path: Path) -> str:
    """Return the SHA-256 of exact durable JSON bytes."""
    return staged_output_sha256(path)


def _object(value, name: str, keys: set[str]) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    actual = set(value)
    if actual != keys:
        missing = sorted(keys - actual)
        unknown = sorted(actual - keys)
        raise ValueError(f"{name} has missing keys {missing} and unknown keys {unknown}")
    return value


def _string(value, name: str, *, choices=None, pattern=None, nullable=False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if choices is not None and value not in choices:
        raise ValueError(f"{name} has an unsupported value")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise ValueError(f"{name} has an invalid format")
    return value


def _integer(value, name: str, *, minimum=0, nullable=False) -> int | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _boolean(value, name: str) -> None:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")


def _filesystem_identity(value, name: str) -> None:
    value = _object(value, name, {"st_dev", "st_ino"})
    _integer(value["st_dev"], f"{name}.st_dev")
    _integer(value["st_ino"], f"{name}.st_ino", minimum=1)


def _path_identity(value, name: str) -> None:
    value = _object(value, name, {"realpath", "identity"})
    realpath = _string(value["realpath"], f"{name}.realpath")
    if not realpath.startswith("/"):
        raise ValueError(f"{name}.realpath must be absolute")
    _filesystem_identity(value["identity"], f"{name}.identity")


def _birth_identity(value, name: str) -> None:
    value = _object(value, name, {"kind", "boot_id", "value"})
    kind = _string(
        value["kind"],
        f"{name}.kind",
        choices={"linux-proc-starttime", "ps-lstart", "unavailable"},
    )
    _string(value["boot_id"], f"{name}.boot_id", nullable=True)
    _string(value["value"], f"{name}.value", nullable=True)
    if kind == "linux-proc-starttime":
        if not value["boot_id"] or not value["value"]:
            raise ValueError(f"{name} Linux birth identity is incomplete")
    elif kind == "ps-lstart":
        if value["boot_id"] is not None or not value["value"]:
            raise ValueError(f"{name} ps birth identity is incomplete")
    elif value["boot_id"] is not None or value["value"] is not None:
        raise ValueError(f"{name} unavailable birth identity must contain nulls")


def _process_identity(value, name: str) -> None:
    value = _object(value, name, {"pid", "ppid", "pgid", "birth"})
    _integer(value["pid"], f"{name}.pid", minimum=1)
    _integer(value["ppid"], f"{name}.ppid")
    _integer(value["pgid"], f"{name}.pgid", minimum=1)
    _birth_identity(value["birth"], f"{name}.birth")


def _marker(value, name: str) -> None:
    value = _object(value, name, {"state", "attempt_id", "detail"})
    _string(value["state"], f"{name}.state", choices=MARKER_STATES)
    attempt = _string(value["attempt_id"], f"{name}.attempt_id", nullable=True)
    if attempt is not None and _HEX32.fullmatch(attempt) is None:
        raise ValueError(f"{name}.attempt_id has an invalid format")
    _string(value["detail"], f"{name}.detail", nullable=True)


def _validate_output(value: dict, stage: str) -> None:
    keys = {
        "blob_id",
        "staged_relative_path",
        "staged_identity",
        "canonical_name",
        "byte_length",
        "digest_algorithm",
        "digest_hex",
        "repair_token",
        "published",
        "failure",
    }
    value = _object(value, "output", keys)
    blob_id = _string(value["blob_id"], "output.blob_id", pattern=_HEX32)
    staged_path = _string(
        value["staged_relative_path"], "output.staged_relative_path", pattern=_STAGED_PATH
    )
    if staged_path != f"{COMPLETION_STAGING_DIRNAME}/{blob_id}.blob":
        raise ValueError("output staged path does not match blob ID")
    _filesystem_identity(value["staged_identity"], "output.staged_identity")
    if value["canonical_name"] != f"{stage}_out.md":
        raise ValueError("output canonical name does not match stage")
    _integer(value["byte_length"], "output.byte_length", minimum=1)
    if value["digest_algorithm"] != DIGEST_ALGORITHM:
        raise ValueError("output digest algorithm must be sha256")
    _string(value["digest_hex"], "output.digest_hex", pattern=_HEX_DIGEST)
    _string(value["repair_token"], "output.repair_token", pattern=_REPAIR_TOKEN)
    _boolean(value["published"], "output.published")
    _string(value["failure"], "output.failure", nullable=True)


def _validate_ownership(value: dict, generation: str, record_boot_id: str) -> None:
    keys = {
        "source_record_relative_path",
        "source_record_sha256",
        "captured",
        "captured_at_ms",
        "platform",
        "proof_mode",
        "pane",
        "supervisor",
        "worker",
        "process_group",
        "descendants",
        "unit",
        "cgroup",
    }
    value = _object(value, "ownership", keys)
    source = _string(
        value["source_record_relative_path"],
        "ownership.source_record_relative_path",
        pattern=_OWNERSHIP_PATH,
    )
    if source != f"run-ownership-{generation}.json":
        raise ValueError("ownership source path does not match generation")
    _string(value["source_record_sha256"], "ownership.source_record_sha256", pattern=_HEX_DIGEST)
    _boolean(value["captured"], "ownership.captured")
    _integer(value["captured_at_ms"], "ownership.captured_at_ms", nullable=True)
    if value["captured"] != (value["captured_at_ms"] is not None):
        raise ValueError("ownership capture flag and timestamp disagree")
    platform = _string(
        value["platform"], "ownership.platform", choices={"linux", "darwin", "other"}
    )
    proof_mode = _string(
        value["proof_mode"],
        "ownership.proof_mode",
        choices={
            "linux-strict",
            "linux-degraded",
            "darwin-bounded",
            "other-bounded-no-birth",
        },
    )
    pane = _object(value["pane"], "ownership.pane", {"pane_id", "window_id", "root_process"})
    _string(pane["pane_id"], "ownership.pane.pane_id")
    _string(pane["window_id"], "ownership.pane.window_id")
    _process_identity(pane["root_process"], "ownership.pane.root_process")
    _process_identity(value["supervisor"], "ownership.supervisor")
    _process_identity(value["worker"], "ownership.worker")
    _integer(value["process_group"], "ownership.process_group", minimum=1)
    if not isinstance(value["descendants"], list):
        raise ValueError("ownership.descendants must be an array")
    for index, process in enumerate(value["descendants"]):
        _process_identity(process, f"ownership.descendants[{index}]")
    descendant_pids = [process["pid"] for process in value["descendants"]]
    if descendant_pids != sorted(set(descendant_pids)):
        raise ValueError("ownership descendants must have unique sorted PIDs")
    if value["supervisor"]["pgid"] != value["process_group"]:
        raise ValueError("ownership process group does not match supervisor")
    unit = value["unit"]
    if unit is not None:
        unit = _object(unit, "ownership.unit", {"name", "load_state", "control_group"})
        _string(unit["name"], "ownership.unit.name")
        _string(unit["load_state"], "ownership.unit.load_state")
        control_group = _string(unit["control_group"], "ownership.unit.control_group")
        if not control_group.startswith("/"):
            raise ValueError("ownership.unit.control_group must be absolute")
    cgroup = value["cgroup"]
    if cgroup is not None:
        cgroup = _object(
            cgroup, "ownership.cgroup", {"relative_path", "absolute_path", "identity", "boot_id"}
        )
        for key in ("relative_path", "absolute_path"):
            path = _string(cgroup[key], f"ownership.cgroup.{key}")
            if not path.startswith("/"):
                raise ValueError(f"ownership.cgroup.{key} must be absolute")
        _filesystem_identity(cgroup["identity"], "ownership.cgroup.identity")
        _string(cgroup["boot_id"], "ownership.cgroup.boot_id")
        if cgroup["boot_id"] != record_boot_id:
            raise ValueError("ownership cgroup boot identity does not match record")

    births = [pane["root_process"]["birth"], value["supervisor"]["birth"], value["worker"]["birth"]]
    births.extend(process["birth"] for process in value["descendants"])
    if proof_mode == "linux-strict":
        if platform != "linux" or unit is None or cgroup is None:
            raise ValueError("strict Linux ownership requires unit and cgroup")
        if any(birth["kind"] != "linux-proc-starttime" for birth in births):
            raise ValueError("strict Linux ownership requires proc start times")
        if any(birth["boot_id"] != record_boot_id for birth in births):
            raise ValueError("strict Linux process boot identity does not match record")
    if proof_mode == "linux-degraded" and (
        platform != "linux"
        or unit is not None
        or cgroup is not None
        or any(birth["kind"] != "linux-proc-starttime" for birth in births)
    ):
        raise ValueError("degraded Linux ownership requires proc start times and no cgroup")
    if proof_mode == "linux-degraded" and any(
        birth["boot_id"] != record_boot_id for birth in births
    ):
        raise ValueError("degraded Linux process boot identity does not match record")
    if proof_mode == "darwin-bounded" and (
        platform != "darwin"
        or unit is not None
        or cgroup is not None
        or any(birth["kind"] != "ps-lstart" for birth in births)
    ):
        raise ValueError("Darwin ownership requires ps start-time identities")
    if proof_mode == "other-bounded-no-birth" and (
        platform != "other"
        or unit is not None
        or cgroup is not None
        or any(birth["kind"] != "ps-lstart" for birth in births)
    ):
        raise ValueError("other bounded ownership requires opaque ps observation tokens")


def _validate_containment(value: dict) -> None:
    keys = {
        "state",
        "started_monotonic_ns",
        "deadline_monotonic_ns",
        "poll_interval_ms",
        "last_cgroup_observation",
        "last_supervisor_observation",
        "last_owned_process_count",
        "result",
        "proof_label",
        "last_error",
    }
    value = _object(value, "containment", keys)
    _string(
        value["state"],
        "containment.state",
        choices={
            "not_started",
            "pane_close_pending",
            "grace",
            "kill_pending",
            "verify_after_kill",
            "proven",
            "blocked",
        },
    )
    _integer(value["started_monotonic_ns"], "containment.started_monotonic_ns", nullable=True)
    _integer(value["deadline_monotonic_ns"], "containment.deadline_monotonic_ns", nullable=True)
    if value["poll_interval_ms"] != POLL_INTERVAL_MS:
        raise ValueError("containment.poll_interval_ms must be 50")
    _string(
        value["last_cgroup_observation"],
        "containment.last_cgroup_observation",
        choices={"empty", "populated", "cannot-tell"},
        nullable=True,
    )
    _string(
        value["last_supervisor_observation"],
        "containment.last_supervisor_observation",
        choices={"gone", "alive", "cannot-tell"},
        nullable=True,
    )
    _integer(
        value["last_owned_process_count"], "containment.last_owned_process_count", nullable=True
    )
    _string(
        value["result"],
        "containment.result",
        choices={
            "linux-strict-empty",
            "linux-strict-killed-empty",
            "linux-degraded-bounded-empty",
            "darwin-bounded-empty",
            "other-bounded-empty-no-birth",
            "no-owned-runner",
        },
        nullable=True,
    )
    _string(value["proof_label"], "containment.proof_label", nullable=True)
    _string(value["last_error"], "containment.last_error", nullable=True)


def _validate_spawn(value) -> None:
    if value is None:
        return
    value = _object(
        value,
        "spawn",
        {
            "target_lode_id",
            "target_generation",
            "receipt_relative_path",
            "pane_id",
            "supervisor_adopted",
            "worker_adopted",
        },
    )
    _string(value["target_lode_id"], "spawn.target_lode_id", pattern=_LODE_ID)
    _string(value["target_generation"], "spawn.target_generation", pattern=_HEX32)
    _string(value["receipt_relative_path"], "spawn.receipt_relative_path", pattern=_SPAWN_PATH)
    _string(value["pane_id"], "spawn.pane_id", nullable=True)
    _boolean(value["supervisor_adopted"], "spawn.supervisor_adopted")
    _boolean(value["worker_adopted"], "spawn.worker_adopted")


def _validate_ship(value) -> None:
    value = _object(
        value,
        "ship",
        {"provenance", "landing", "backlog", "archive_published", "quarantine", "cleanup_failure"},
    )
    provenance = _object(
        value["provenance"],
        "ship.provenance",
        {
            "project",
            "git_common_dir",
            "worktree",
            "worktree_git_dir",
            "branch_ref",
            "branch_oid",
            "head_oid",
        },
    )
    for key in ("project", "git_common_dir", "worktree", "worktree_git_dir"):
        _path_identity(provenance[key], f"ship.provenance.{key}")
    branch_ref = _string(provenance["branch_ref"], "ship.provenance.branch_ref")
    if not branch_ref.startswith("refs/heads/"):
        raise ValueError("ship.provenance.branch_ref must be a full branch ref")
    _string(provenance["branch_oid"], "ship.provenance.branch_oid", pattern=_HEX_OID)
    _string(provenance["head_oid"], "ship.provenance.head_oid", pattern=_HEX_OID)
    landing = _object(value["landing"], "ship.landing", {"cause", "base_ref", "detail", "accepted"})
    for key in ("cause", "base_ref", "detail"):
        _string(landing[key], f"ship.landing.{key}", nullable=True)
    _boolean(landing["accepted"], "ship.landing.accepted")
    backlog = _object(
        value["backlog"],
        "ship.backlog",
        {"planned", "selected_item_id", "promoted_lode_id", "remaining_item_ids", "applied"},
    )
    _boolean(backlog["planned"], "ship.backlog.planned")
    _string(backlog["selected_item_id"], "ship.backlog.selected_item_id", nullable=True)
    _string(backlog["promoted_lode_id"], "ship.backlog.promoted_lode_id", nullable=True)
    if not isinstance(backlog["remaining_item_ids"], list) or not all(
        isinstance(item, str) for item in backlog["remaining_item_ids"]
    ):
        raise ValueError("ship.backlog.remaining_item_ids must be an array of strings")
    _boolean(backlog["applied"], "ship.backlog.applied")
    _boolean(value["archive_published"], "ship.archive_published")
    quarantine = _object(
        value["quarantine"],
        "ship.quarantine",
        {
            "original_path",
            "quarantine_path",
            "expected_identity",
            "registration_repaired",
            "removal_outcome",
            "branch_outcome",
        },
    )
    for key in ("original_path", "quarantine_path"):
        path = _string(quarantine[key], f"ship.quarantine.{key}")
        if not path.startswith("/"):
            raise ValueError(f"ship.quarantine.{key} must be absolute")
    _filesystem_identity(quarantine["expected_identity"], "ship.quarantine.expected_identity")
    _boolean(quarantine["registration_repaired"], "ship.quarantine.registration_repaired")
    _string(
        quarantine["removal_outcome"],
        "ship.quarantine.removal_outcome",
        choices={"pending", "removed", "retained"},
    )
    _string(
        quarantine["branch_outcome"],
        "ship.quarantine.branch_outcome",
        choices={"pending", "deleted", "already_absent", "retained"},
    )
    _string(value["cleanup_failure"], "ship.cleanup_failure", nullable=True)


def _next_action(action_type: str, stage: str, target_disposition: str) -> dict:
    expected = {
        ("completion", "mill", "advance_refine"): {
            "kind": "advance",
            "target_stage": "refine",
        },
        ("completion", "refine", "advance_ship"): {
            "kind": "advance",
            "target_stage": "ship",
        },
        ("completion", "ship", "shipped_archived"): {
            "kind": "ship_archive",
            "target_stage": None,
        },
        ("pause", stage, "paused"): {"kind": "pause", "target_stage": None},
        ("restart", stage, "replacement_spawned"): {
            "kind": "restart",
            "target_stage": stage,
        },
        ("kill", stage, "killed_archived"): {
            "kind": "kill_archive",
            "target_stage": None,
        },
        ("archive", stage, "archived"): {
            "kind": "archive",
            "target_stage": None,
        },
    }.get((action_type, stage, target_disposition))
    if expected is None:
        raise ValueError("target disposition does not match action type and stage")
    return expected


def action_binding(value: dict) -> tuple[str, str | None, str, str, bool]:
    """Validate and project the immutable identity fields of an action."""
    value = _object(
        value,
        "action binding",
        {
            "lode_id",
            "expected_generation",
            "action_type",
            "target_disposition",
            "force_consent",
        },
    )
    lode_id = _string(value["lode_id"], "lode_id", pattern=_LODE_ID)
    generation = _string(
        value["expected_generation"],
        "expected_generation",
        pattern=_HEX32,
        nullable=True,
    )
    action_type = _string(value["action_type"], "action_type", choices=ACTION_TYPES)
    target = _string(
        value["target_disposition"],
        "target_disposition",
        choices=TARGET_DISPOSITIONS[action_type],
    )
    _boolean(value["force_consent"], "force_consent")
    return lode_id, generation, action_type, target, value["force_consent"]


def validate_action_id(value) -> str:
    """Validate one externally supplied action identity."""
    return _string(value, "action_id", pattern=_HEX32)


def record_binding(record: dict) -> tuple[str, str | None, str, str, bool]:
    """Return the validated bound tuple from a pending record or receipt."""
    return action_binding(
        {
            "lode_id": record["lode_id"],
            "expected_generation": record["expected_generation"],
            "action_type": record["action_type"],
            "target_disposition": record["target_disposition"],
            "force_consent": record["force_consent"],
        }
    )


def _validate_durability_observation(value, name: str) -> None:
    value = _object(value, name, {"outcome", "count", "basis", "error", "checked_at_ms"})
    _string(
        value["outcome"],
        f"{name}.outcome",
        choices={"safe", "unknown", "unpushed", "consent_override", "not_required"},
    )
    _integer(value["count"], f"{name}.count", nullable=True)
    _string(value["basis"], f"{name}.basis", nullable=True)
    _string(value["error"], f"{name}.error", nullable=True)
    _integer(value["checked_at_ms"], f"{name}.checked_at_ms", nullable=True)


def _validate_durability(value, action_type: str, force_consent: bool) -> None:
    value = _object(value, "durability", {"required", "preflight", "final"})
    _boolean(value["required"], "durability.required")
    _validate_durability_observation(value["preflight"], "durability.preflight")
    _validate_durability_observation(value["final"], "durability.final")
    observations = (value["preflight"], value["final"])
    if any(item["outcome"] == "consent_override" for item in observations) and not (
        action_type == "kill" and force_consent
    ):
        raise ValueError("only a forced kill may override durability")


def _validate_action_result(result: dict) -> dict:
    result = _object(
        result,
        "action result",
        {
            "schema_version",
            "action_id",
            "lode_id",
            "expected_generation",
            "action_type",
            "target_disposition",
            "force_consent",
            "terminal_disposition",
            "completed_at_ms",
            "containment_proof",
            "retained",
            "successor",
        },
    )
    if type(result["schema_version"]) is not int or result["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported action result schema")
    _string(result["action_id"], "action result action_id", pattern=_HEX32)
    record_binding(result)
    _string(
        result["terminal_disposition"],
        "action result terminal_disposition",
        choices=TARGET_DISPOSITIONS[result["action_type"]],
    )
    if result["terminal_disposition"] != result["target_disposition"]:
        raise ValueError("action result disposition does not match its binding")
    _integer(result["completed_at_ms"], "action result completed_at_ms")
    _string(result["containment_proof"], "action result containment_proof", nullable=True)
    retained = _object(
        result["retained"], "action result retained", {"worktree", "branch", "session"}
    )
    for name in retained:
        _boolean(retained[name], f"action result retained.{name}")
    if result["successor"] is not None:
        successor = _object(
            result["successor"],
            "action result successor",
            {"lode_id", "generation", "pane_id"},
        )
        _string(successor["lode_id"], "action result successor.lode_id", pattern=_LODE_ID)
        _string(successor["generation"], "action result successor.generation", pattern=_HEX32)
        _string(successor["pane_id"], "action result successor.pane_id")
    return result


def validate_pending_action(record: dict) -> dict:
    """Validate the complete v2 pending action record and return it unchanged."""
    keys = {
        "schema_version",
        "action_id",
        "lode_id",
        "action_type",
        "target_disposition",
        "force_consent",
        "stage",
        "expected_generation",
        "accepted_at_ms",
        "boot_id",
        "phase",
        "next_action",
        "output",
        "ownership",
        "containment",
        "durability",
        "spawn",
        "ship",
        "markers",
        "recovery",
        "result",
    }
    record = _object(record, "pending action", keys)
    if type(record["schema_version"]) is not int or record["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported pending action schema")
    _string(record["action_id"], "action_id", pattern=_HEX32)
    _, generation, action_type, target_disposition, force_consent = record_binding(record)
    stage = _string(record["stage"], "stage", choices=STAGES)
    if action_type == "completion" and generation is None:
        raise ValueError("completion requires an expected generation")
    _integer(record["accepted_at_ms"], "accepted_at_ms")
    boot_id = _string(record["boot_id"], "boot_id", nullable=True)
    if generation is None and boot_id is not None:
        raise ValueError("boot_id requires an expected generation")
    _string(record["phase"], "phase", choices=PHASES)
    next_action = _object(record["next_action"], "next_action", {"kind", "target_stage"})
    if next_action != _next_action(action_type, stage, target_disposition):
        raise ValueError("next action does not match action binding")
    if action_type == "completion":
        if record["output"] is None:
            raise ValueError("completion requires output facts")
        _validate_output(record["output"], stage)
    elif record["output"] is not None:
        raise ValueError("manual action cannot contain output facts")
    if record["ownership"] is not None:
        if generation is None or boot_id is None:
            raise ValueError("ownership requires an expected generation")
        _validate_ownership(record["ownership"], generation, boot_id)
    elif action_type == "completion":
        raise ValueError("completion requires ownership facts")
    elif boot_id is not None:
        raise ValueError("boot_id requires ownership facts")
    _validate_containment(record["containment"])
    _validate_durability(record["durability"], action_type, force_consent)
    _validate_spawn(record["spawn"])
    if action_type == "completion" and stage == "ship":
        if record["ship"] is None:
            raise ValueError("ship completion requires ship facts")
        _validate_ship(record["ship"])
    elif record["ship"] is not None:
        raise ValueError("only ship completion can contain ship facts")
    markers = _object(record["markers"], "markers", set(MARKER_NAMES))
    for name in MARKER_NAMES:
        _marker(markers[name], f"markers.{name}")
    irrelevant_markers = set()
    if action_type != "completion":
        irrelevant_markers.update(
            {
                "output_publish",
                "ship_landing",
                "quarantine_rename",
                "worktree_repair",
                "cleanup_authorization",
                "backlog",
                "worktree_remove",
                "branch_delete",
            }
        )
    elif stage != "ship":
        irrelevant_markers.update(
            {
                "ship_landing",
                "quarantine_rename",
                "worktree_repair",
                "cleanup_authorization",
                "archive",
                "backlog",
                "worktree_remove",
                "branch_delete",
            }
        )
    if action_type not in {"kill", "archive"}:
        irrelevant_markers.add("durability_recheck")
    if action_type not in {"completion", "restart"}:
        irrelevant_markers.add("spawn")
    if action_type in {"pause", "restart"}:
        irrelevant_markers.add("archive")
    if action_type in {"kill", "archive"}:
        irrelevant_markers.add("lode_mutation")
    for name in irrelevant_markers:
        if markers[name] != new_marker():
            raise ValueError(f"irrelevant marker {name} must not be started")
    recovery = _object(record["recovery"], "recovery", {"kind", "message", "command"})
    _string(
        recovery["kind"],
        "recovery.kind",
        choices={
            "output",
            "publication",
            "ownership",
            "containment",
            "landing",
            "durability",
            "spawn",
            "cleanup",
        },
        nullable=True,
    )
    _string(recovery["message"], "recovery.message", nullable=True)
    _string(recovery["command"], "recovery.command", nullable=True)
    if record["result"] is not None:
        _validate_action_result(record["result"])
        if record_binding(record["result"]) != record_binding(record):
            raise ValueError("action result binding does not match pending action")
    return record


def validate_run_ownership(record: dict, *, require_worker: bool = False) -> dict:
    """Validate the durable supervisor/worker launch identity record."""
    keys = {
        "schema_version",
        "lode_id",
        "run_generation",
        "registered_at_ms",
        "boot_id",
        "platform",
        "proof_mode",
        "degraded_reason",
        "pane",
        "supervisor",
        "worker",
        "process_group",
        "descendants",
        "unit",
        "cgroup",
        "unit_name",
    }
    record = _object(record, "run ownership", keys)
    if (
        type(record["schema_version"]) is not int
        or record["schema_version"] != RUN_OWNERSHIP_SCHEMA_VERSION
    ):
        raise ValueError("unsupported run ownership schema")
    _string(record["lode_id"], "run ownership lode_id", pattern=_LODE_ID)
    _string(record["run_generation"], "run ownership generation", pattern=_HEX32)
    _integer(record["registered_at_ms"], "run ownership registered_at_ms")
    boot_id = _string(record["boot_id"], "run ownership boot_id")
    if not boot_id:
        raise ValueError("run ownership boot identity is empty")
    platform = _string(
        record["platform"], "run ownership platform", choices={"linux", "darwin", "other"}
    )
    proof_mode = _string(
        record["proof_mode"],
        "run ownership proof_mode",
        choices={
            "linux-strict",
            "linux-degraded",
            "darwin-bounded",
            "other-bounded-no-birth",
        },
    )
    _string(record["degraded_reason"], "run ownership degraded_reason", nullable=True)
    pane = _object(record["pane"], "run ownership pane", {"pane_id", "window_id", "root_process"})
    _string(pane["pane_id"], "run ownership pane_id")
    _string(pane["window_id"], "run ownership window_id")
    _process_identity(pane["root_process"], "run ownership pane root")
    _process_identity(record["supervisor"], "run ownership supervisor")
    if record["worker"] is not None:
        _process_identity(record["worker"], "run ownership worker")
    elif require_worker:
        raise ValueError("run ownership worker is not registered")
    _integer(record["process_group"], "run ownership process_group", minimum=1)
    if record["supervisor"]["pgid"] != record["process_group"]:
        raise ValueError("run ownership process group does not match supervisor")
    _string(record["unit_name"], "run ownership unit_name", nullable=True)
    if not isinstance(record["descendants"], list):
        raise ValueError("run ownership descendants must be an array")
    for index, process in enumerate(record["descendants"]):
        _process_identity(process, f"run ownership descendants[{index}]")
    if record["worker"] is None:
        if record["descendants"] or record["unit"] is not None or record["cgroup"] is not None:
            raise ValueError("partial run ownership contains final capture facts")
    else:
        pending_shape = {
            "source_record_relative_path": f"run-ownership-{record['run_generation']}.json",
            "source_record_sha256": "0" * 64,
            "captured": True,
            "captured_at_ms": record["registered_at_ms"],
            "platform": platform,
            "proof_mode": proof_mode,
            "pane": record["pane"],
            "supervisor": record["supervisor"],
            "worker": record["worker"],
            "process_group": record["process_group"],
            "descendants": record["descendants"],
            "unit": record["unit"],
            "cgroup": record["cgroup"],
        }
        _validate_ownership(pending_shape, record["run_generation"], boot_id)
    if proof_mode == "linux-strict":
        if platform != "linux" or not record["unit_name"] or record["degraded_reason"] is not None:
            raise ValueError("strict Linux run ownership requires a unit and no degradation")
        if require_worker and (record["unit"] is None or record["cgroup"] is None):
            raise ValueError("strict Linux run ownership requires captured systemd facts")
    else:
        if record["unit_name"] is not None:
            raise ValueError("degraded run ownership cannot claim a systemd unit")
        if not record["degraded_reason"]:
            raise ValueError("degraded run ownership requires a reason")
    launch_births = [pane["root_process"]["birth"], record["supervisor"]["birth"]]
    if platform == "linux" and (
        any(birth["kind"] != "linux-proc-starttime" for birth in launch_births)
        or any(birth["boot_id"] != boot_id for birth in launch_births)
    ):
        raise ValueError("Linux run ownership requires matching proc birth identities")
    if platform != "linux" and any(birth["kind"] != "ps-lstart" for birth in launch_births):
        raise ValueError("bounded run ownership requires opaque ps birth identities")
    return record


def write_run_ownership(record: dict) -> Path:
    """Durably write one launch ownership record."""
    validate_run_ownership(record)
    path = run_ownership_path(record["lode_id"], record["run_generation"])
    write_durable_json(path, record)
    return path


def load_run_ownership(lode_id: str, run_generation: str, *, require_worker=False) -> dict | None:
    """Load one launch ownership record, preserving absence as distinct."""
    path = run_ownership_path(lode_id, run_generation)
    try:
        with open(path, encoding="utf-8") as source:
            record = json.load(source)
    except FileNotFoundError:
        return None
    validate_run_ownership(record, require_worker=require_worker)
    if record["lode_id"] != lode_id or record["run_generation"] != run_generation:
        raise ValueError("run ownership identity does not match its path")
    return record


def new_pending_action(
    *,
    lode_id: str,
    stage: str,
    expected_generation: str | None,
    action_type: str,
    target_disposition: str,
    force_consent: bool,
    output_facts: dict | None = None,
    ownership_record: dict | None = None,
    source_record_sha256: str | None = None,
    ship: dict | None = None,
    action_id: str | None = None,
    accepted_ms: int | None = None,
    durability: dict | None = None,
    already_empty: bool = False,
) -> dict:
    """Build a complete accepted record from validated action facts."""
    binding = {
        "lode_id": lode_id,
        "expected_generation": expected_generation,
        "action_type": action_type,
        "target_disposition": target_disposition,
        "force_consent": force_consent,
    }
    action_binding(binding)
    if ownership_record is not None:
        validate_run_ownership(ownership_record, require_worker=True)
        if ownership_record["run_generation"] != expected_generation:
            raise ValueError("run ownership generation does not match action binding")
        if source_record_sha256 is None:
            raise ValueError("run ownership digest is required")
    action_id = action_id or uuid.uuid4().hex
    next_action = _next_action(action_type, stage, target_disposition)
    facts = ownership_record
    accepted_ms = accepted_ms if accepted_ms is not None else int(time.time() * 1000)
    if durability is None:
        observation = {
            "outcome": "not_required",
            "count": 0,
            "basis": action_type,
            "error": None,
            "checked_at_ms": accepted_ms,
        }
        durability = {
            "required": False,
            "preflight": dict(observation),
            "final": dict(observation),
        }
    record = {
        "schema_version": SCHEMA_VERSION,
        "action_id": action_id,
        **binding,
        "stage": stage,
        "accepted_at_ms": accepted_ms,
        "boot_id": facts["boot_id"] if facts is not None else None,
        "phase": "accepted",
        "next_action": next_action,
        "output": (
            {
                **output_facts,
                "canonical_name": f"{stage}_out.md",
                "repair_token": secrets.token_urlsafe(32),
                "published": False,
                "failure": None,
            }
            if output_facts is not None
            else None
        ),
        "ownership": (
            {
                "source_record_relative_path": run_ownership_path(
                    lode_id, expected_generation
                ).name,
                "source_record_sha256": source_record_sha256,
                "captured": False,
                "captured_at_ms": None,
                "platform": facts["platform"],
                "proof_mode": facts["proof_mode"],
                "pane": facts["pane"],
                "supervisor": facts["supervisor"],
                "worker": facts["worker"],
                "process_group": facts["process_group"],
                "descendants": facts["descendants"],
                "unit": facts["unit"],
                "cgroup": facts["cgroup"],
            }
            if facts is not None
            else None
        ),
        "containment": {
            "state": "not_started",
            "started_monotonic_ns": None,
            "deadline_monotonic_ns": None,
            "poll_interval_ms": POLL_INTERVAL_MS,
            "last_cgroup_observation": None,
            "last_supervisor_observation": None,
            "last_owned_process_count": None,
            "result": None,
            "proof_label": None,
            "last_error": None,
        },
        "durability": durability,
        "spawn": None,
        "ship": ship,
        "markers": new_markers(),
        "recovery": {"kind": None, "message": None, "command": None},
        "result": None,
    }
    if already_empty:
        if ownership_record is not None:
            raise ValueError("already-empty action cannot contain ownership facts")
        record["containment"].update(
            state="proven",
            result="no-owned-runner",
            proof_label="no active runner identity was recorded at acceptance",
        )
        for marker_name in ("ownership_capture", "pane_close", "containment"):
            transition_marker(record, marker_name, "intent")
            marker = record["markers"][marker_name]
            transition_marker(
                record,
                marker_name,
                "done",
                attempt_id=marker["attempt_id"],
                detail="no owned runner existed at acceptance",
            )
    validate_pending_action(record)
    return record


def write_pending_action(record: dict) -> Path:
    """Validate and durably publish the sole pending record for its lode."""
    validate_pending_action(record)
    path = pending_action_path(record["lode_id"])
    write_durable_json(path, record)
    return path


class LegacyPendingActionError(ValueError):
    """A v1 completion record was found during a clean-break upgrade."""


def load_pending_action(lode_id: str) -> dict | None:
    """Load and validate a lode's pending record, or return None when absent."""
    path = pending_action_path(lode_id)
    try:
        with open(path, encoding="utf-8") as source:
            record = json.load(source)
    except FileNotFoundError:
        return None
    if isinstance(record, dict) and record.get("schema_version") == 1:
        raise LegacyPendingActionError(
            "schema-v1 pending action must be drained before this host is upgraded"
        )
    validate_pending_action(record)
    if record["lode_id"] != lode_id:
        raise ValueError("pending action belongs to a different lode")
    return record


def new_action_result(
    record: dict,
    *,
    completed_ms: int | None = None,
    retained: dict | None = None,
    successor: dict | None = None,
) -> dict:
    """Build the immutable terminal receipt for a validated pending action."""
    validate_pending_action(record)
    result = {
        "schema_version": SCHEMA_VERSION,
        "action_id": record["action_id"],
        "lode_id": record["lode_id"],
        "expected_generation": record["expected_generation"],
        "action_type": record["action_type"],
        "target_disposition": record["target_disposition"],
        "force_consent": record["force_consent"],
        "terminal_disposition": record["target_disposition"],
        "completed_at_ms": completed_ms if completed_ms is not None else accepted_at_ms(),
        "containment_proof": record["containment"]["proof_label"],
        "retained": retained or {"worktree": True, "branch": True, "session": False},
        "successor": successor,
    }
    return _validate_action_result(result)


def find_action_result(lode: dict, action_id: str) -> dict | None:
    """Return one validated retained result receipt by action identity."""
    _string(action_id, "action result lookup ID", pattern=_HEX32)
    results = lode.get("action_results", [])
    if not isinstance(results, list):
        raise ValueError("lode action_results must be an array")
    found = None
    for result in results:
        _validate_action_result(result)
        if result["action_id"] == action_id:
            if found is not None:
                raise ValueError("lode contains duplicate action result IDs")
            found = result
    return found


def append_action_result(lode: dict, result: dict) -> None:
    """Append one receipt and retain the eight newest in publication order."""
    _validate_action_result(result)
    if result["lode_id"] != lode.get("id"):
        raise ValueError("action result belongs to a different lode")
    results = lode.setdefault("action_results", [])
    if not isinstance(results, list):
        raise ValueError("lode action_results must be an array")
    for existing in results:
        _validate_action_result(existing)
        if existing["action_id"] == result["action_id"]:
            if existing != result:
                raise ValueError("action result identity already has different facts")
            return
    results.append(result)
    del results[:-ACTION_RESULT_LIMIT]


def pending_action_projection(record: dict) -> dict:
    """Return the public, capability-free projection of a pending action."""
    validate_pending_action(record)
    containment = record["containment"]
    return {
        "action_id": record["action_id"],
        "action_type": record["action_type"],
        "expected_generation": record["expected_generation"],
        "target_disposition": record["target_disposition"],
        "force_consent": record["force_consent"],
        "stage": record["stage"],
        "phase": record["phase"],
        "containment": {
            "state": containment["state"],
            "result": containment["result"],
            "proof_label": containment["proof_label"],
            "last_error": containment["last_error"],
        },
        "preserved": _preserved_artifacts(record["action_type"], record=record),
        "recovery": dict(record["recovery"]),
        "status": action_status(record),
    }


def _validate_spawn_receipt(receipt: dict) -> dict:
    keys = {
        "schema_version",
        "action_id",
        "source_lode_id",
        "target_lode_id",
        "target_generation",
        "pane_id",
    }
    receipt = _object(receipt, "spawn receipt", keys)
    if (
        type(receipt["schema_version"]) is not int
        or receipt["schema_version"] != SPAWN_RECEIPT_SCHEMA_VERSION
    ):
        raise ValueError("unsupported spawn receipt schema")
    _string(receipt["action_id"], "spawn receipt action_id", pattern=_HEX32)
    _string(receipt["source_lode_id"], "spawn receipt source_lode_id", pattern=_LODE_ID)
    _string(receipt["target_lode_id"], "spawn receipt target_lode_id", pattern=_LODE_ID)
    _string(receipt["target_generation"], "spawn receipt generation", pattern=_HEX32)
    _string(receipt["pane_id"], "spawn receipt pane_id")
    return receipt


def write_spawn_receipt(receipt: dict) -> Path:
    """Durably publish the pane identity before its supervisor starts."""
    receipt = _validate_spawn_receipt(receipt)
    path = spawn_receipt_path(receipt["source_lode_id"], receipt["action_id"])
    write_durable_json(path, receipt)
    return path


def load_spawn_receipt(lode_id: str, action_id: str) -> dict | None:
    """Load an action-scoped pane bootstrap receipt, preserving absence."""
    path = spawn_receipt_path(lode_id, action_id)
    try:
        with open(path, encoding="utf-8") as source:
            receipt = json.load(source)
    except FileNotFoundError:
        return None
    receipt = _validate_spawn_receipt(receipt)
    if receipt["source_lode_id"] != lode_id or receipt["action_id"] != action_id:
        raise ValueError("spawn receipt identity does not match its path")
    return receipt


def clear_pending_action(record: dict) -> None:
    """Durably remove a completed action's staged blob and pending fence."""
    validate_pending_action(record)
    if record["markers"]["pending_clear"]["state"] != "done":
        raise ValueError("pending action cannot clear before pending_clear is done")
    directory = lode_dir(record["lode_id"])
    output = record["output"]
    if record["action_type"] == "completion" and output is not None:
        staged = directory / output["staged_relative_path"]
        try:
            staged.unlink()
        except FileNotFoundError:
            pass
        else:
            _fsync_directory(staged.parent)
    pending = pending_action_path(record["lode_id"])
    try:
        pending.unlink()
    except FileNotFoundError:
        return
    _fsync_directory(directory)


def stage_output(
    lode_id: str,
    data: bytes,
    *,
    blob_id: str | None = None,
    expected_length: int | None = None,
    expected_sha256: str | None = None,
) -> dict:
    """Durably stage and reverify exact completion bytes."""
    if not isinstance(data, bytes) or not data:
        raise ValueError("completion output must be nonempty bytes")
    digest = hashlib.sha256(data).hexdigest()
    if expected_length is not None and expected_length != len(data):
        raise ValueError("completion output length does not match message")
    if expected_sha256 is not None and not secrets.compare_digest(expected_sha256, digest):
        raise ValueError("completion output SHA-256 does not match message")
    blob_id = blob_id or uuid.uuid4().hex
    if _HEX32.fullmatch(blob_id) is None:
        raise ValueError("blob ID must be 32 lowercase hexadecimal characters")
    directory = staging_dir(lode_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{blob_id}.blob"
    if path.exists():
        raise FileExistsError(path)
    _durable_replace(path, lambda output: output.write(data))
    identity = verify_output_file(path, len(data), digest)
    return {
        "blob_id": blob_id,
        "staged_relative_path": f"{COMPLETION_STAGING_DIRNAME}/{blob_id}.blob",
        "staged_identity": identity,
        "byte_length": len(data),
        "digest_algorithm": DIGEST_ALGORITHM,
        "digest_hex": digest,
    }


def repair_staged_output(record: dict, data: bytes) -> dict:
    """Durably replace only the staged blob with the immutable accepted bytes."""
    validate_pending_action(record)
    if record["action_type"] != "completion" or record["output"] is None:
        raise ValueError("only a completion action has repairable output")
    if not isinstance(data, bytes):
        raise ValueError("repair output must be bytes")
    output = record["output"]
    if len(data) != output["byte_length"]:
        raise ValueError("repair output length does not match accepted output")
    digest = hashlib.sha256(data).hexdigest()
    if not secrets.compare_digest(digest, output["digest_hex"]):
        raise ValueError("repair output SHA-256 does not match accepted output")
    path = lode_dir(record["lode_id"]) / output["staged_relative_path"]
    _durable_replace(path, lambda destination: destination.write(data))
    return verify_output_file(path, output["byte_length"], output["digest_hex"])


def pending_output_recovery(record: dict) -> dict | None:
    """Return the capability-bearing status projection only for blocked output."""
    validate_pending_action(record)
    output = record["output"]
    if (
        record["action_type"] != "completion"
        or output is None
        or record["phase"] != "output_blocked"
        or output["published"]
        or record["recovery"]["kind"] != "output"
    ):
        return None
    return {
        "stage": record["stage"],
        "action_id": record["action_id"],
        "sha256": output["digest_hex"],
        "byte_length": output["byte_length"],
        "repair_token": output["repair_token"],
        "failure": output["failure"] or record["recovery"]["message"],
        "command": record["recovery"]["command"],
    }


def verify_output_file(path: Path, length: int, digest_hex: str) -> dict:
    """Verify exact bytes and return their filesystem identity."""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as source:
        before = os.fstat(source.fileno())
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
        after = os.fstat(source.fileno())
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise OSError("completion output identity changed while reading")
    if size != length or not secrets.compare_digest(digest.hexdigest(), digest_hex):
        raise ValueError("completion output bytes do not match accepted digest and length")
    return {"st_dev": before.st_dev, "st_ino": before.st_ino}


def verify_staged_output(record: dict) -> Path:
    """Verify that the server-owned staged blob still matches its accepted identity."""
    validate_pending_action(record)
    if record["action_type"] != "completion" or record["output"] is None:
        raise ValueError("only a completion action has staged output")
    output = record["output"]
    staged = lode_dir(record["lode_id"]) / output["staged_relative_path"]
    identity = verify_output_file(staged, output["byte_length"], output["digest_hex"])
    if identity != output["staged_identity"]:
        raise OSError("staged completion output identity changed")
    return staged


def publish_output(record: dict) -> Path:
    """Publish a verified staged blob to its canonical stage output path."""
    output = record["output"]
    directory = lode_dir(record["lode_id"])
    staged = verify_staged_output(record)

    canonical = directory / output["canonical_name"]

    def copy_and_verify(destination) -> None:
        digest = hashlib.sha256()
        length = 0
        with open(staged, "rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                destination.write(chunk)
                digest.update(chunk)
                length += len(chunk)
        if length != output["byte_length"] or not secrets.compare_digest(
            digest.hexdigest(), output["digest_hex"]
        ):
            raise ValueError("staged completion output changed while publishing")

    _durable_replace(canonical, copy_and_verify)
    verify_output_file(canonical, output["byte_length"], output["digest_hex"])
    return canonical


def collect_orphaned_staging(lode_id: str, record: dict | None = None) -> list[Path]:
    """Delete writer temporaries and blobs not referenced by a valid record."""
    if record is None:
        record = load_pending_action(lode_id)
    elif record["lode_id"] != lode_id:
        raise ValueError("pending action belongs to a different lode")
    if record is not None:
        validate_pending_action(record)
    output = record["output"] if record and record["action_type"] == "completion" else None
    keep = output["staged_relative_path"].split("/", 1)[1] if output else None
    directory = staging_dir(lode_id)
    if not directory.is_dir():
        return []
    removed = []
    for path in directory.iterdir():
        if path.is_file() and _STAGING_FILE.fullmatch(path.name) and path.name != keep:
            path.unlink()
            removed.append(path)
    if removed:
        _fsync_directory(directory)
    return removed


def new_marker() -> dict:
    """Return an untouched durable phase marker."""
    return {"state": "not_started", "attempt_id": None, "detail": None}


def new_markers() -> dict:
    """Return the complete marker map for a new action."""
    return {name: new_marker() for name in MARKER_NAMES}


def transition_marker(
    record: dict,
    name: str,
    state: str,
    *,
    attempt_id: str | None = None,
    detail: str | None = None,
) -> dict:
    """Apply one legal marker transition in memory and return the record."""
    if name not in MARKER_NAMES or state not in MARKER_STATES:
        raise ValueError("unknown action marker or state")
    marker = record["markers"][name]
    if state not in MARKER_TRANSITIONS[marker["state"]]:
        raise ValueError(f"illegal marker transition {marker['state']} -> {state}")
    if state == "intent":
        attempt_id = attempt_id or uuid.uuid4().hex
        if _HEX32.fullmatch(attempt_id) is None:
            raise ValueError("marker attempt ID must be 32 lowercase hexadecimal characters")
    elif attempt_id is None:
        attempt_id = marker["attempt_id"]
    if state in {"done", "blocked"} and attempt_id != marker["attempt_id"]:
        raise ValueError("marker result does not match its durable attempt")
    marker.update(state=state, attempt_id=attempt_id, detail=detail)
    return record


def recovery_command(record: dict, kind: str) -> str:
    """Return the single operator command for a blocked action phase."""
    if kind == "output":
        return (
            f"hop lode repair-output {record['lode_id']} - "
            f"--token {record['output']['repair_token']}"
        )
    if record["action_type"] == "completion":
        return f"hop lode restart {record['lode_id']}"
    if record["action_type"] == "archive":
        return f"hop lode archive {record['lode_id']}"
    suffix = " --force" if record["force_consent"] else ""
    return f"hop lode {record['action_type']} {record['lode_id']}{suffix}"


def _preserved_artifacts(action_type: str, *, record: dict | None = None) -> dict:
    """Describe user-owned artifacts retained while an action is blocked."""
    if action_type == "completion" and record is not None and record["stage"] == "ship":
        quarantine = record["ship"]["quarantine"]
        return {
            "worktree": quarantine["removal_outcome"] != "removed",
            "branch": quarantine["branch_outcome"] != "deleted",
            "stage_session": False,
        }
    return {
        "worktree": True,
        "branch": True,
        "stage_session": action_type != "restart",
    }


def _preserved_text(preserved: dict) -> str:
    names = [name.replace("_", " ") for name, kept in preserved.items() if kept]
    return ", ".join(names) if names else "none"


def _blocked_facts(record: dict) -> str:
    containment = record["containment"]
    truth = containment["proof_label"] or containment["result"] or containment["state"]
    preserved = _preserved_text(_preserved_artifacts(record["action_type"], record=record))
    return (
        f"Action {record['action_id']} owns generation "
        f"{record['expected_generation'] or 'none'} for "
        f"{record['target_disposition'].replace('_', ' ')}; containment: {truth}. "
        f"Preserved: {preserved}"
    )


def action_retry_command(
    action_type: str | None,
    lode_id: str | None,
    *,
    force_consent: bool = False,
) -> str:
    """Return the authoritative safe retry instruction for an action request."""
    if not lode_id:
        return "hop lode list"
    if action_type == "archive":
        return f"hop lode archive {lode_id}"
    if action_type == "completion":
        return "hop processed"
    if action_type in {"pause", "restart", "kill"}:
        suffix = " --force" if force_consent else ""
        return f"hop lode {action_type} {lode_id}{suffix}"
    return f"hop lode status {lode_id}"


def action_ack_projection(
    *,
    outcome: str,
    reason: str,
    action_id: str | None = None,
    lode_id: str | None = None,
    expected_generation: str | None = None,
    action_type: str | None = None,
    target_disposition: str | None = None,
    force_consent: bool = False,
    disposition: str | None = None,
    detail: str | None = None,
    record: dict | None = None,
    receipt: dict | None = None,
    owner: dict | None = None,
) -> dict:
    """Build the single wire/status projection for an action response."""
    if record is not None:
        validate_pending_action(record)
        action_id = record["action_id"]
        lode_id = record["lode_id"]
        expected_generation = record["expected_generation"]
        action_type = record["action_type"]
        target_disposition = record["target_disposition"]
        force_consent = record["force_consent"]
    if receipt is not None:
        _validate_action_result(receipt)
        action_id = receipt["action_id"]
        lode_id = receipt["lode_id"]
        expected_generation = receipt["expected_generation"]
        action_type = receipt["action_type"]
        target_disposition = receipt["target_disposition"]
        force_consent = receipt["force_consent"]
        disposition = receipt["terminal_disposition"]

    response = {
        "accepted": outcome != "refused",
        "outcome": outcome,
        "reason": reason,
    }
    bound_fields = {
        "action_id": action_id,
        "lode_id": lode_id,
        "expected_generation": expected_generation,
        "action_type": action_type,
        "target_disposition": target_disposition,
        "force_consent": force_consent,
    }
    response.update({key: value for key, value in bound_fields.items() if value is not None})
    if action_id is not None or lode_id is not None:
        response["expected_generation"] = expected_generation
    if disposition is not None:
        response["disposition"] = disposition

    if record is not None:
        projection = pending_action_projection(record)
        for key in ("phase", "containment", "preserved", "recovery"):
            response[key] = projection[key]
    elif receipt is not None:
        response["containment"] = {
            "state": "proven",
            "result": receipt["containment_proof"],
            "proof_label": receipt["containment_proof"],
            "last_error": None,
        }
        response["preserved"] = {
            "worktree": receipt["retained"]["worktree"],
            "branch": receipt["retained"]["branch"],
            "stage_session": receipt["retained"]["session"],
        }

    retry_force = force_consent or reason in {
        "registered_runner_requires_force",
        "started_stage_requires_force",
    }
    retry = action_retry_command(action_type, lode_id, force_consent=retry_force)
    inspect = f"hop lode status {lode_id}" if lode_id else "hop lode list"
    if outcome == "refused":
        owner_text = (
            f"Action {owner['action_id']} ({owner['action_type']}) owns generation "
            f"{owner['expected_generation'] or 'none'}."
            if isinstance(owner, dict)
            else (
                f"Action {action_id or 'unbound'} did not acquire generation "
                f"{expected_generation or 'none'}."
            )
        )
        preserved = (
            dict(owner["preserved"])
            if isinstance(owner, dict) and isinstance(owner.get("preserved"), dict)
            else _preserved_artifacts(action_type or "")
        )
        explanation = detail or (
            "This request uses a retired mixed-version control message; upgrade the "
            "calling hop CLI before retrying"
            if reason == "protocol_upgrade_required"
            else reason.replace("_", " ")
        )
        response["preserved"] = preserved
        response["recovery"] = {
            "kind": reason,
            "message": explanation,
            "command": retry,
        }
        response["detail"] = explanation
        response["recovery_command"] = retry
        response["status"] = (
            f"{(action_type or 'Action').capitalize()} refused ({reason}): {explanation}. "
            f"{owner_text} Preserved: {_preserved_text(preserved)}. "
            f"Inspect with: {inspect}. Retry with: {retry}"
        )
    elif outcome == "blocked" and record is not None:
        response["detail"] = record["recovery"]["message"]
        response["recovery_command"] = record["recovery"]["command"]
        response["status"] = action_status(record)
    elif outcome in {"completed", "idempotent"} and disposition is not None:
        preserved = response.get("preserved", _preserved_artifacts(action_type or ""))
        response["status"] = (
            f"{(action_type or 'Action').capitalize()} completed: "
            f"{disposition.replace('_', ' ')}. Preserved: {_preserved_text(preserved)}."
        )
    elif outcome == "accepted" and action_type == "completion":
        stage = record["stage"] if record is not None else "stage"
        response["status"] = (
            f"Accepted {stage} output for durable teardown (action {action_id or 'unknown'}). "
            "The server will close and prove containment independently."
        )
    elif record is not None:
        response["status"] = action_status(record)
    else:
        response["status"] = f"{(action_type or 'Action').capitalize()} {outcome}."
    return response


def action_status(record: dict) -> str:
    """Project one pending record into the exact operator-facing status text."""
    phase = record["phase"]
    stage = record["stage"]
    recovery = record["recovery"]
    if record["action_type"] != "completion":
        label = record["action_type"].capitalize()
        if phase.endswith("_blocked"):
            detail = recovery["message"] or f"{record['action_type']} action is blocked"
            command = recovery["command"] or f"hop lode status {record['lode_id']}"
            containment = record["containment"]
            truth = containment["proof_label"] or containment["result"] or containment["state"]
            preserved = _preserved_text(_preserved_artifacts(record["action_type"], record=record))
            retry_label = "Retry" if record["action_type"] == "archive" else "Retry with"
            return (
                f"{label} blocked: {detail}. Action {record['action_id']} owns generation "
                f"{record['expected_generation'] or 'none'} for "
                f"{record['target_disposition'].replace('_', ' ')}; containment: {truth}. "
                f"Preserved: {preserved}. {retry_label}: {command}"
            )
        if phase == "complete":
            return f"{label}: {record['target_disposition'].replace('_', ' ')}"
        return f"{label}: teardown in progress"
    if phase == "output_blocked":
        detail = recovery["message"] or "completion output publication failed"
        command = recovery["command"]
        if recovery["kind"] == "publication":
            return f"Teardown blocked: {detail}. Retry with: {command}. {_blocked_facts(record)}"
        output = record["output"]
        return (
            f"Teardown blocked: accepted {stage} output is unavailable "
            f"(sha256 {output['digest_hex']}, {output['byte_length']} bytes). Repair with: "
            f"{command}. {_blocked_facts(record)}"
        )
    if phase in {"containment_blocked", "ship_blocked", "cleanup_blocked"}:
        detail = recovery["message"] or "unknown teardown failure"
        ship = record.get("ship")
        if phase == "cleanup_blocked" and ship is not None and ship["archive_published"]:
            return (
                f"Shipped; cleanup retained: {detail}. Retry with: {recovery['command']}. "
                f"{_blocked_facts(record)}"
            )
        return (
            f"Teardown blocked: {detail}. Retry with: {recovery['command']}. "
            f"{_blocked_facts(record)}"
        )
    if phase in {"accepted", "publishing_output"}:
        return f"Teardown: publishing accepted {stage} output"
    if phase == "capturing_ownership":
        return "Teardown: capturing generation ownership"
    if phase == "closing_pane":
        return f"Teardown: closing pane {record['ownership']['pane']['pane_id']}"
    if phase in {"observing_containment", "force_killing"}:
        mode = record["ownership"]["proof_mode"]
        if mode == "linux-strict":
            return "Teardown: waiting for Linux containment"
        if mode == "darwin-bounded":
            return (
                "Teardown: waiting for bounded Darwin ownership "
                "(degraded; leak-free cleanup unproven)"
            )
        if mode == "linux-degraded":
            return (
                "Teardown: waiting for bounded Linux ownership "
                "(degraded; leak-free cleanup unproven)"
            )
        return "Teardown: waiting for bounded ownership (degraded; birth identity unavailable)"
    if phase == "proving_ship_landing":
        return "Teardown: proving ship landing"
    if phase == "quarantining":
        ship = record["ship"]
        if ship["archive_published"]:
            return "Teardown: cleaning quarantined worktree"
        return "Teardown: quarantining shipped worktree"
    if phase == "publishing_terminal":
        if record["ownership"]["proof_mode"] == "linux-degraded":
            return (
                "Teardown: bounded Linux containment observed "
                "(degraded; leak-free cleanup unproven); awaiting next action"
            )
        target = record["next_action"]["target_stage"]
        return f"Teardown: advancing to {target}" if target else "Teardown: archiving shipped lode"
    if phase == "spawning":
        return f"Teardown: starting {record['next_action']['target_stage']}"
    if phase == "complete" and record["ownership"]["proof_mode"] == "darwin-bounded":
        return "Shipped (bounded Darwin teardown; leak-free cleanup unproven)"
    if phase == "complete" and record["ownership"]["proof_mode"] == "linux-degraded":
        return "Shipped (bounded Linux teardown; systemd proof and leak-free cleanup unproven)"
    if phase == "complete" and record["ownership"]["proof_mode"] != "linux-strict":
        return "Shipped (degraded teardown; birth identity and leak-free cleanup unproven)"
    if phase == "complete":
        return "Shipped"
    return "Teardown: archiving shipped lode"


def staged_output_sha256(path: Path) -> str:
    """Return SHA-256 for a file without loading it all into memory."""
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def accepted_at_ms() -> int:
    """Return a wall-clock timestamp for a newly accepted record."""
    return int(time.time() * 1000)
