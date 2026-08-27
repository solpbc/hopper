# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Lode management for hopper.

Lodes are plain dicts with these fields:
- id: str - 8-character base32 ID
- stage: str - "mill", "refine", "ship", or "shipped"
- created_at: int - milliseconds since epoch
- project: str - project name (default "")
- scope: str - user's task scope description (default "")
- originating_extro_sid: str | None - submitting client's Extro session ID (default None)
- updated_at: int - milliseconds since epoch (default 0, meaning use created_at)
- state: str - server-validated lifecycle state (default "new")
- status: str - human-readable status text (default "")
- title: str - short human-readable label (default "")
- branch: str - git branch name for this lode's worktree (default "")
- worktree_path: str | None - durably published managed worktree path (default None)
- active: bool - whether a runner client is connected (default False)
- tmux_pane: str | None - tmux pane ID (default None)
- pid: int | None - process ID of active runner (default None)
- run_generation: str | None - generation owning runner mutations (default None)
- oom_scope: str | None - guarded systemd scope unit name (default None)
- failure_kind: str | None - durable terminal runner failure discriminator (default None)
- protocol_error: str | None - durable current-generation stage-protocol refusal (default None)
- gate_body: str | None - durable operator-visible current gate body (default None)
- gate_kind: str | None - durable current gate kind (default None)
- gate_epoch: int - monotonic current/most-recent gate identity (default 0)
- gate_delivery_epoch: int - monotonic delivery attempt identity (default 0)
- archive_action_id: str | None - action that published this archive (default None)
- codex_thread_id: str | None - Codex thread ID for stage resumption (default None)
- coder: optional dict - non-Codex refine-stage provider and resumable session:
    {"provider": "grok", "session_id": str | None}; presence means its session
    lives in coder.session_id; absence means Codex, whose session lives in
    codex_thread_id
- last_progress_at: int | None - timestamp of most recent progress heartbeat
- last_progress_summary: str - short progress summary for UI display
- last_pane_activity_at: int | None - timestamp of most recent real pane change (default None)
- pane_title_observation: dict | None - cross-attempt processing-title observation (default None)
- backlog: dict | None - original backlog item data if promoted (default None)
- archived_at: int | None - milliseconds since epoch when archived
  (default None, set by archive_lode)
- runs: dict - per-stage runtime tracking {"stage": {"started_at": ms, "stopped_at": ms}}
- driver: str - immutable interactive-stage provider ("claude", "codex", or "grok")
- stage_sessions: dict - canonical per-stage launch/session tracking:
    {"mill": {"launch_id": "<uuid>", "provider_session_id": "<uuid>",
     "transcript_path": None, "started": false, "start_attempt": None}, ...}
- claude: dict - retained legacy projection of canonical per-stage session tracking:
    {"mill": {"session_id": "<uuid>", "started": false},
     "refine": {"session_id": "<uuid>", "started": false},
     "ship": {"session_id": "<uuid>", "started": false}}. This is a bounded
  rolling-version compatibility field.
"""

import copy
import json
import os
import secrets
import time
import uuid
from pathlib import Path

from hopper import config, oom
from hopper.coder import DEFAULT_CODER_PROVIDER, validate_coder_provider
from hopper.tmux import Liveness, pane_liveness

ID_LEN = 8  # Lode ID length (8 base32 chars)
ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"  # lowercase base32
REFUSAL_STATUS_PREFIXES = (
    "spawn refused: ",
    "spawn failed: ",
    "action refused: ",
    "protocol error: ",
)
_REFUSAL_STATUS_INDEX = {"spawn": 0, "spawn_failed": 1, "action": 2, "protocol": 3}
_STAGE_NAMES = ("mill", "refine", "ship")
# These are valid durable record values, not necessarily runnable adapters.
VALID_DURABLE_STAGE_DRIVERS = frozenset({"claude", "codex", "grok"})
GATE_KINDS = frozenset({"explicit", "native_question", "idle_park"})
# Frozen launch-ID namespace; it must never change or stable durable identities would rewrite.
_LAUNCH_ID_NAMESPACE = uuid.UUID("bd3a8a6f-6d4f-4d1c-bce3-9d27fc61864c")


def format_refusal_status(kind: str, message: str) -> str:
    """Format a refusal using the single prefix vocabulary rendered by the TUI."""
    try:
        prefix = REFUSAL_STATUS_PREFIXES[_REFUSAL_STATUS_INDEX[kind]]
    except KeyError as error:
        raise ValueError(f"unknown refusal status kind: {kind}") from error
    return f"{prefix}{message}"


def is_canonical_lode_id(value: object) -> bool:
    """Return whether a value is a canonical Hopper lode ID."""
    return (
        isinstance(value, str)
        and len(value) == ID_LEN
        and all(character in ID_ALPHABET for character in value)
    )


def current_time_ms() -> int:
    """Return current time in milliseconds since epoch."""
    return int(time.time() * 1000)


def format_age(timestamp_ms: int) -> str:
    """Format a timestamp as a friendly age string.

    Args:
        timestamp_ms: Timestamp in milliseconds since epoch

    Returns:
        Friendly string like "now", "3m", "4h", "2d", "1w"
    """
    now = current_time_ms()
    diff_ms = now - timestamp_ms

    # Handle future timestamps or very recent
    if diff_ms < 60_000:  # < 1 minute
        return "now"

    minutes = diff_ms // 60_000
    if minutes < 60:
        return f"{minutes}m"

    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"

    days = hours // 24
    if days < 7:
        return f"{days}d"

    weeks = days // 7
    return f"{weeks}w"


def format_uptime(started_at_ms: int) -> str:
    """Format uptime as a friendly duration string.

    Args:
        started_at_ms: Start timestamp in milliseconds since epoch

    Returns:
        Friendly string like "5m", "2h 15m", "3d 4h"
    """
    now = current_time_ms()
    diff_ms = now - started_at_ms

    if diff_ms < 60_000:  # < 1 minute
        return "0m"

    minutes = diff_ms // 60_000
    hours = minutes // 60
    days = hours // 24

    minutes = minutes % 60
    hours = hours % 24

    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0 and days == 0:  # Only show minutes if less than a day
        parts.append(f"{minutes}m")

    return " ".join(parts) if parts else "0m"


def format_duration_ms(duration_ms: int) -> str:
    """Format a duration in milliseconds as a friendly string.

    Args:
        duration_ms: Duration in milliseconds

    Returns:
        Friendly string like "5s", "1m", "2m", "1h"
    """
    if duration_ms < 1000:
        return "0s"

    seconds = duration_ms // 1000
    if seconds < 60:
        return f"{seconds}s"

    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"

    hours = minutes // 60
    return f"{hours}h"


def compute_runtime_ms(lode: dict, now: int | None = None) -> int:
    """Sum runtime across all stages in a lode's runs dict."""
    if now is None:
        now = current_time_ms()
    total = 0
    for stage_run in lode.get("runs", {}).values():
        started = stage_run.get("started_at")
        if started is None:
            continue
        stopped = stage_run.get("stopped_at")
        if stopped is not None:
            total += stopped - started
        else:
            total += now - started
    return max(0, total)


def touch(lode: dict) -> None:
    """Update a lode's updated_at timestamp to now."""
    lode["updated_at"] = current_time_ms()


def get_lode_dir(lode_id: str) -> Path:
    """Get the directory for a lode."""
    return config.hopper_dir() / "lodes" / lode_id


def get_worktree_dir(lode_id: str) -> Path:
    """Return the on-disk git worktree location for a lode.

    New worktrees live under config.worktree_root() (whitespace-free)
    so downstream project tooling that mishandles spaces in paths does
    not break. A worktree already created at the legacy location
    (get_lode_dir(lode_id)/"worktree") is returned in place so existing
    lodes stay resumable, inspectable, and cleanable with no filesystem
    surgery.
    """
    legacy = get_lode_dir(lode_id) / "worktree"
    if legacy.is_dir():
        return legacy
    return config.worktree_root() / lode_id


def resolve_worktree_path(lode: dict) -> dict:
    """Resolve durable or discoverable worktree provenance for a lode."""
    lode_id = lode.get("id")
    if not isinstance(lode_id, str) or not lode_id:
        return {"path": None, "basis": "unavailable", "reason": "invalid_lode_id"}

    try:
        root = config.worktree_root().resolve(strict=False)
        managed_candidate = root / lode_id
        managed = managed_candidate.resolve(strict=False)
        legacy = (get_lode_dir(lode_id) / "worktree").resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return {"path": None, "basis": "unavailable", "reason": "discovery_error"}

    recorded = lode.get("worktree_path")
    if recorded is not None:
        if not isinstance(recorded, str) or not recorded or "\x00" in recorded:
            return {"path": None, "basis": "unavailable", "reason": "invalid_recorded_path"}
        recorded_path = Path(recorded)
        if not recorded_path.is_absolute():
            return {"path": None, "basis": "unavailable", "reason": "recorded_not_absolute"}
        try:
            canonical = recorded_path.resolve(strict=False)
            canonical.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            return {"path": None, "basis": "unavailable", "reason": "recorded_outside_root"}
        if canonical != managed_candidate:
            return {"path": None, "basis": "unavailable", "reason": "recorded_identity_mismatch"}
        return {"path": canonical, "basis": "recorded", "reason": None}

    try:
        legacy_exists = legacy.is_dir()
        managed_exists = managed.is_dir()
    except OSError:
        return {"path": None, "basis": "unavailable", "reason": "discovery_error"}
    if legacy_exists and managed_exists:
        return {"path": None, "basis": "unavailable", "reason": "ambiguous_candidates"}
    if managed_exists:
        if managed != managed_candidate:
            return {
                "path": None,
                "basis": "unavailable",
                "reason": "managed_identity_mismatch",
            }
        return {"path": managed, "basis": "existing", "reason": None}
    if legacy_exists:
        return {"path": legacy, "basis": "unavailable", "reason": "legacy_outside_root"}
    return {"path": None, "basis": "unavailable", "reason": "no_existing_candidate"}


def parse_diff_numstat_totals(text: str) -> tuple[int, int]:
    """Parse git numstat output and return (total_additions, total_deletions)."""
    total_additions = 0
    total_deletions = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        additions, deletions, _filename = parts
        if not additions.isdigit() or not deletions.isdigit():
            continue
        total_additions += int(additions)
        total_deletions += int(deletions)
    return (total_additions, total_deletions)


def parse_diff_numstat(text: str) -> str:
    """Parse git numstat output and return a compact summary."""
    additions, deletions = parse_diff_numstat_totals(text)
    if additions == 0 and deletions == 0:
        return ""
    return f"+{additions} -{deletions}"


def read_diff_totals(lode_id: str) -> tuple[int, int]:
    """Read a lode's diff.txt and return (additions, deletions) totals."""
    diff_path = get_lode_dir(lode_id) / "diff.txt"
    try:
        return parse_diff_numstat_totals(diff_path.read_text())
    except Exception:
        return (0, 0)


def _lode_record_error(lode: dict, stage: str, field: str, detail: str) -> ValueError:
    """Build one prescriptive durable-stage validation error."""
    lode_id = lode.get("id")
    return ValueError(f"lode {lode_id!r} stage {stage!r} has invalid {field}: {detail}")


def _validate_uuid(value: object, lode: dict, stage: str, field: str) -> str:
    """Validate and return a durable UUID string without changing its spelling."""
    if not isinstance(value, str) or not value:
        raise _lode_record_error(lode, stage, field, "must be a non-empty UUID string")
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError) as error:
        raise _lode_record_error(lode, stage, field, "must be a UUID string") from error
    return value


def validate_lode_driver(driver: object) -> str:
    """Validate an immutable interactive-stage provider."""
    if not isinstance(driver, str) or driver not in VALID_DURABLE_STAGE_DRIVERS:
        choices = ", ".join(sorted(VALID_DURABLE_STAGE_DRIVERS))
        raise ValueError(f"lode driver must be one of {choices}")
    return str(driver)


def lode_driver(lode: dict) -> str:
    """Return a lode's immutable interactive-stage provider.

    Older records predate the field and therefore remain Claude records. New
    records always write it explicitly.
    """
    if "driver" not in lode:
        return "claude"
    try:
        return validate_lode_driver(lode["driver"])
    except ValueError as error:
        raise ValueError(f"lode {lode.get('id')!r} has invalid driver: {error}") from error


def set_lode_driver(lode: dict, driver: str) -> None:
    """Refuse any attempt to change the immutable lode driver."""
    current = lode_driver(lode)
    requested = validate_lode_driver(driver)
    if requested != current:
        raise ValueError(
            f"lode {lode.get('id')!r} driver is immutable: {current!r} cannot become {requested!r}"
        )


def _expected_launch_id(lode_id: str, stage: str, provider_session_id: str) -> str:
    """Derive the stable launch identity for one provider session."""
    return str(uuid.uuid5(_LAUNCH_ID_NAMESPACE, f"{lode_id}:{stage}:{provider_session_id}"))


def stage_launch_id(lode_id: str, stage: str, provider_session_id: str) -> str:
    """Return the durable launch UUID paired with one provider session UUID."""
    return _expected_launch_id(lode_id, stage, provider_session_id)


def _normalize_lode_gate(lode: dict) -> None:
    """Validate one durable gate or materialize an old status-only gate once."""
    epoch = lode.get("gate_epoch", 0)
    delivery_epoch = lode.get("gate_delivery_epoch", 0)
    if (
        not isinstance(epoch, int)
        or isinstance(epoch, bool)
        or epoch < 0
        or not isinstance(delivery_epoch, int)
        or isinstance(delivery_epoch, bool)
        or delivery_epoch < 0
    ):
        raise ValueError(f"lode {lode.get('id')!r} has invalid gate epoch")

    body = lode.get("gate_body")
    kind = lode.get("gate_kind")
    if body is None and kind is None:
        # Older status-only gates have one bounded, deterministic conversion so
        # every future reader has the same durable authority.
        if lode.get("state") == "gated":
            body = lode.get("status") or "Gate"
            kind = "idle_park" if str(body).startswith("Parked (idle)") else "explicit"
            epoch = max(epoch, 1)
        lode["gate_body"] = body
        lode["gate_kind"] = kind
        lode["gate_epoch"] = epoch
        lode["gate_delivery_epoch"] = delivery_epoch
        return
    if not isinstance(body, str) or not body or kind not in GATE_KINDS or epoch < 1:
        raise ValueError(f"lode {lode.get('id')!r} has malformed durable gate")
    lode["gate_epoch"] = epoch
    lode["gate_delivery_epoch"] = delivery_epoch


def lode_gate(lode: dict) -> dict | None:
    """Return the validated durable gate, or None when this lode is ungated."""
    _normalize_lode_gate(lode)
    body = lode["gate_body"]
    if body is None:
        return None
    return {
        "body": body,
        "kind": lode["gate_kind"],
        "epoch": lode["gate_epoch"],
        "delivery_epoch": lode["gate_delivery_epoch"],
    }


def _validate_stage_session(lode: dict, stage: str, session: object) -> dict:
    """Validate one canonical stage-session record and return it unchanged."""
    if not isinstance(session, dict):
        raise _lode_record_error(lode, stage, "stage session", "must be an object")
    expected_fields = {
        "launch_id",
        "provider_session_id",
        "transcript_path",
        "started",
        "start_attempt",
    }
    missing = expected_fields - session.keys()
    if missing:
        raise _lode_record_error(lode, stage, next(iter(sorted(missing))), "is required")
    extra = session.keys() - expected_fields
    if extra:
        raise _lode_record_error(lode, stage, next(iter(sorted(extra))), "is not supported")

    provider_session_id = _validate_uuid(
        session["provider_session_id"], lode, stage, "provider_session_id"
    )
    launch_id = _validate_uuid(session["launch_id"], lode, stage, "launch_id")
    expected_launch_id = _expected_launch_id(str(lode.get("id")), stage, provider_session_id)
    if launch_id != expected_launch_id:
        raise _lode_record_error(
            lode, stage, "launch_id", "does not match the pinned launch identity"
        )
    if not isinstance(session["started"], bool):
        raise _lode_record_error(lode, stage, "started", "must be a boolean")
    if session["transcript_path"] is not None and not isinstance(session["transcript_path"], str):
        raise _lode_record_error(lode, stage, "transcript_path", "must be a string or null")
    if session["start_attempt"] is not None:
        _validate_stage_start_attempt(lode, stage, session, session["start_attempt"])
    return session


def _validate_stage_start_attempt(lode: dict, stage: str, session: dict, attempt: object) -> dict:
    """Validate the lode-local committed binding attempt for one stage."""
    if not isinstance(attempt, dict):
        raise _lode_record_error(lode, stage, "start_attempt", "must be an object or null")
    expected_fields = {
        "driver",
        "stage",
        "launch_id",
        "provider_session_id",
        "run_generation",
        "outcome",
    }
    missing = expected_fields - attempt.keys()
    if missing:
        raise _lode_record_error(lode, stage, next(iter(sorted(missing))), "is required")
    extra = attempt.keys() - expected_fields
    if extra:
        raise _lode_record_error(lode, stage, next(iter(sorted(extra))), "is not supported")
    if attempt["driver"] != lode_driver(lode):
        raise _lode_record_error(lode, stage, "driver", "does not match the lode driver")
    if attempt["stage"] != stage:
        raise _lode_record_error(lode, stage, "stage", "does not match the stage session")
    if attempt["launch_id"] != session["launch_id"]:
        raise _lode_record_error(lode, stage, "launch_id", "does not match the stage session")
    if attempt["provider_session_id"] != session["provider_session_id"]:
        raise _lode_record_error(
            lode, stage, "provider_session_id", "does not match the stage session"
        )
    if not isinstance(attempt["run_generation"], str) or not attempt["run_generation"]:
        raise _lode_record_error(lode, stage, "run_generation", "must be a non-empty string")
    if attempt["outcome"] != "committed":
        raise _lode_record_error(lode, stage, "outcome", "must be committed")
    return attempt


def lode_stage_session(lode: dict, stage: str) -> dict:
    """Return one validated canonical stage-session record."""
    if stage not in _STAGE_NAMES:
        raise _lode_record_error(lode, stage, "stage", "is not a managed interactive stage")
    # A new runner can receive an untouched pre-foundation lode from an old
    # server process.  Normalize that legacy-only wire payload at the accessor
    # boundary before requiring canonical state.  An explicitly present but
    # malformed canonical field must still fail instead of falling back.
    if "stage_sessions" not in lode and "claude" in lode:
        normalize_lode_stage_sessions(lode)
    sessions = lode.get("stage_sessions")
    if not isinstance(sessions, dict):
        raise _lode_record_error(lode, stage, "stage_sessions", "is required")
    if stage not in sessions:
        raise _lode_record_error(lode, stage, "stage session", "is required")
    return _validate_stage_session(lode, stage, sessions[stage])


def project_lode_claude_state(lode: dict) -> None:
    """Project canonical stage sessions into the retained Claude compatibility map."""
    lode["claude"] = {
        stage: {
            "session_id": lode_stage_session(lode, stage)["provider_session_id"],
            "started": lode_stage_session(lode, stage)["started"],
        }
        for stage in _STAGE_NAMES
    }


def _validate_legacy_claude_stage(lode: dict, stage: str, session: object) -> dict:
    """Validate one legacy compatibility projection stage."""
    if not isinstance(session, dict):
        raise _lode_record_error(lode, stage, "claude stage", "must be an object")
    expected_fields = {"session_id", "started"}
    missing = expected_fields - session.keys()
    if missing:
        raise _lode_record_error(lode, stage, next(iter(sorted(missing))), "is required")
    extra = session.keys() - expected_fields
    if extra:
        raise _lode_record_error(lode, stage, next(iter(sorted(extra))), "is not supported")
    _validate_uuid(session["session_id"], lode, stage, "session_id")
    if not isinstance(session["started"], bool):
        raise _lode_record_error(lode, stage, "started", "must be a boolean")
    return session


def _legacy_stage_sessions(lode: dict) -> dict:
    """Materialize canonical session records from a validated legacy projection."""
    claude = lode.get("claude")
    if not isinstance(claude, dict):
        raise _lode_record_error(lode, "mill", "claude", "must be an object")
    expected_stages = set(_STAGE_NAMES)
    missing = expected_stages - claude.keys()
    if missing:
        raise _lode_record_error(lode, next(iter(sorted(missing))), "claude stage", "is required")
    extra = claude.keys() - expected_stages
    if extra:
        raise _lode_record_error(
            lode, next(iter(sorted(extra))), "claude stage", "is not supported"
        )
    sessions = {}
    for stage in _STAGE_NAMES:
        legacy = _validate_legacy_claude_stage(lode, stage, claude[stage])
        provider_session_id = legacy["session_id"]
        sessions[stage] = {
            "launch_id": _expected_launch_id(str(lode.get("id")), stage, provider_session_id),
            "provider_session_id": provider_session_id,
            "transcript_path": None,
            "started": legacy["started"],
            "start_attempt": None,
        }
    return sessions


def normalize_lode_stage_sessions(lode: dict) -> dict:
    """Validate one durable lode record and materialize legacy-only canonical state.

    The normalizer never mints a replacement identity. Legacy-only records get
    their deterministic canonical projection; hybrid records must agree; and
    canonical-only records remain canonical-only after validation.
    """
    if not isinstance(lode, dict):
        raise ValueError("lode record must be an object")
    lode_id = lode.get("id")
    if not isinstance(lode_id, str) or not lode_id:
        raise _lode_record_error(lode, "mill", "id", "must be a non-empty string")

    has_canonical = "stage_sessions" in lode
    has_legacy = "claude" in lode
    if not has_canonical and not has_legacy:
        raise _lode_record_error(lode, "mill", "stage_sessions", "or claude is required")

    if has_canonical:
        if "driver" not in lode:
            raise _lode_record_error(lode, "mill", "driver", "is required with stage_sessions")
        try:
            lode_driver(lode)
        except ValueError as error:
            raise _lode_record_error(lode, "mill", "driver", str(error)) from error
        sessions = lode["stage_sessions"]
        if not isinstance(sessions, dict):
            raise _lode_record_error(lode, "mill", "stage_sessions", "must be an object")
        expected_stages = set(_STAGE_NAMES)
        missing = expected_stages - sessions.keys()
        if missing:
            raise _lode_record_error(
                lode, next(iter(sorted(missing))), "stage session", "is required"
            )
        extra = sessions.keys() - expected_stages
        if extra:
            raise _lode_record_error(
                lode, next(iter(sorted(extra))), "stage session", "is not supported"
            )
        if has_legacy:
            _legacy_stage_sessions(lode)
        for stage in _STAGE_NAMES:
            canonical = _validate_stage_session(lode, stage, sessions[stage])
            if has_legacy:
                legacy = lode["claude"][stage]
                if legacy["session_id"] != canonical["provider_session_id"]:
                    raise _lode_record_error(
                        lode, stage, "provider_session_id", "conflicts with legacy session_id"
                    )
                if legacy["started"] != canonical["started"]:
                    raise _lode_record_error(
                        lode, stage, "started", "conflicts with legacy projection"
                    )
        _normalize_lode_gate(lode)
        return lode

    # The only absence-means-Claude case is a legacy-only durable record.
    if "driver" in lode:
        try:
            driver = lode_driver(lode)
        except ValueError as error:
            raise _lode_record_error(lode, "mill", "driver", str(error)) from error
        if driver != "claude":
            raise _lode_record_error(
                lode, "mill", "driver", "legacy claude projection requires claude"
            )
    lode["driver"] = "claude"
    lode["stage_sessions"] = _legacy_stage_sessions(lode)
    _normalize_lode_gate(lode)
    return lode


def _load_lodes_file(path: Path) -> list[dict]:
    """Load and normalize one JSONL lode snapshot."""
    if not path.exists():
        return []
    lodes = []
    with open(path) as source:
        for line in source:
            line = line.strip()
            if line:
                lodes.append(normalize_lode_stage_sessions(json.loads(line)))
    return lodes


def load_lodes() -> list[dict]:
    """Load active lodes from JSONL file through the shared normalizer."""
    return _load_lodes_file(config.hopper_dir() / "active.jsonl")


def load_archived_lodes() -> list[dict]:
    """Load archived lodes through the shared normalizer."""
    return _load_lodes_file(config.hopper_dir() / "archived.jsonl")


def _write_jsonl_atomic(path: Path, items: list[dict]) -> None:
    """Atomically write a complete JSONL snapshot using a writer-unique temp file."""
    # Unique temps prevent concurrent writers from corrupting each other's
    # snapshots, but writes remain last-writer-wins. A live-server
    # `hop project rename` can still lose updates; this is sound only because
    # the flock singleton removes server-vs-server concurrency.
    tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp_path, "w") as f:
            for item in items:
                f.write(json.dumps(item) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def save_archived_lodes(lodes: list[dict]) -> None:
    """Atomically save archived lodes to JSONL file."""
    _write_jsonl_atomic(config.hopper_dir() / "archived.jsonl", lodes)


def save_lodes(lodes: list[dict]) -> None:
    """Atomically save lodes to JSONL file."""
    _write_jsonl_atomic(config.hopper_dir() / "active.jsonl", lodes)


def _generate_lode_id(lodes: list[dict]) -> str:
    """Generate a unique 8-character base32 lode ID.

    Checks for collisions against active lodes, archived lodes, and existing
    lode directories.
    """
    # Load archived IDs for collision check
    archived_ids: set[str] = set()
    archived_file = config.hopper_dir() / "archived.jsonl"
    if archived_file.exists():
        with open(archived_file) as f:
            for line in f:
                line = line.strip()
                if line:
                    data = json.loads(line)
                    archived_ids.add(data["id"])

    # Get existing lode directories
    lodes_dir = config.hopper_dir() / "lodes"
    existing_dirs = set(lodes_dir.iterdir()) if lodes_dir.exists() else set()
    existing_dir_names = {d.name for d in existing_dirs}

    # Active lode IDs
    active_ids = {lode["id"] for lode in lodes}

    # Generate until unique
    for _ in range(100):  # Safety limit
        new_id = "".join(secrets.choice(ID_ALPHABET) for _ in range(ID_LEN))
        if (
            new_id not in active_ids
            and new_id not in archived_ids
            and new_id not in existing_dir_names
        ):
            return new_id

    raise RuntimeError("Failed to generate unique lode ID after 100 attempts")


def make_lode_stage_sessions(
    lode_id: str, provider_session_ids: dict[str, str] | None = None
) -> dict:
    """Build canonical stage sessions with one provider identity per stage.

    This is deliberately small and deterministic for callers that construct a
    complete durable lode in tests. Production creation supplies no IDs and
    receives fresh provider sessions.
    """
    provider_session_ids = provider_session_ids or {
        stage: str(uuid.uuid4()) for stage in _STAGE_NAMES
    }
    if set(provider_session_ids) != set(_STAGE_NAMES):
        raise ValueError("provider session IDs must contain mill, refine, and ship")
    lode = {"id": lode_id}
    sessions = {}
    for stage in _STAGE_NAMES:
        provider_session_id = _validate_uuid(
            provider_session_ids[stage], lode, stage, "provider_session_id"
        )
        sessions[stage] = {
            "launch_id": _expected_launch_id(lode_id, stage, provider_session_id),
            "provider_session_id": provider_session_id,
            "transcript_path": None,
            "started": False,
            "start_attempt": None,
        }
    return sessions


def create_lode(
    lodes: list[dict],
    project: str,
    scope: str = "",
    *,
    lode_id: str | None = None,
    originating_extro_sid: str | None = None,
    coder_provider: str = DEFAULT_CODER_PROVIDER,
    driver: str = "claude",
) -> dict:
    """Create a new lode, add to list, and create its directory.

    Args:
        lodes: List of lodes to add to.
        project: Project name for this lode.
        scope: User's task scope description.

    Returns:
        The newly created lode dict.
    """
    coder_provider = validate_coder_provider(coder_provider)
    driver = validate_lode_driver(driver)
    if lode_id is not None:
        if len(lode_id) != ID_LEN or any(character not in ID_ALPHABET for character in lode_id):
            raise ValueError("reserved lode ID has an invalid format")
        if any(existing.get("id") == lode_id for existing in lodes):
            raise ValueError("reserved lode ID is already active")
        if get_lode_dir(lode_id).exists():
            raise ValueError("reserved lode ID directory already exists")
        archived_path = config.hopper_dir() / "archived.jsonl"
        if archived_path.exists():
            with open(archived_path) as source:
                if any(json.loads(line).get("id") == lode_id for line in source if line.strip()):
                    raise ValueError("reserved lode ID is already archived")
    new_lode_id = lode_id or _generate_lode_id(lodes)
    stage_sessions = make_lode_stage_sessions(new_lode_id)
    now = current_time_ms()
    lode = {
        "id": new_lode_id,
        "stage": "mill",
        "shipped_at": None,
        "created_at": now,
        "project": project,
        "scope": scope,
        "originating_extro_sid": originating_extro_sid,
        "updated_at": now,
        "state": "new",
        "status": "Ready to start",
        "title": "",
        "branch": "",
        "worktree_path": None,
        "worktree_reap": None,
        "active": False,
        "tmux_pane": None,
        "pid": None,
        "run_generation": None,
        "oom_scope": None,
        "failure_kind": None,
        "errored_at": None,
        "protocol_error": None,
        "gate_body": None,
        "gate_kind": None,
        "gate_epoch": 0,
        "gate_delivery_epoch": 0,
        "spawn_disposition": None,
        "archive_action_id": None,
        "codex_thread_id": None,
        "last_progress_at": None,
        "last_progress_summary": "",
        "last_pane_activity_at": None,
        "pane_title_observation": None,
        "backlog": None,
        "pending_action": None,
        "action_results": [],
        "runs": {},
        "driver": driver,
        "stage_sessions": stage_sessions,
        "claude": {
            stage: {
                "session_id": stage_sessions[stage]["provider_session_id"],
                "started": stage_sessions[stage]["started"],
            }
            for stage in _STAGE_NAMES
        },
    }
    if coder_provider != "codex":
        lode["coder"] = {"provider": coder_provider, "session_id": None}
    lodes.append(lode)
    get_lode_dir(lode["id"]).mkdir(parents=True, exist_ok=True)
    save_lodes(lodes)
    return lode


def reserve_lode_id(lodes: list[dict]) -> str:
    """Return a collision-checked lode ID without publishing a lode."""
    return _generate_lode_id(lodes)


def _update_lode_field(lodes: list[dict], lode_id: str, field: str, value) -> dict | None:
    """Find a lode by ID, set a single field, touch, and save."""
    for lode in lodes:
        if lode["id"] == lode_id:
            lode[field] = value
            touch(lode)
            save_lodes(lodes)
            return lode
    return None


def update_lode_stage(lodes: list[dict], lode_id: str, stage: str) -> dict | None:
    """Update a lode's stage. Returns the updated lode or None if not found."""
    return _update_lode_field(lodes, lode_id, "stage", stage)


def archive_lode(lodes: list[dict], lode_id: str) -> dict | None:
    """Archive a lode: append to archived.jsonl and remove from active list.

    The lode directory is left intact; git worktree and branch cleanup
    is handled by the caller.
    Returns the archived lode or None if not found.
    """
    for i, lode in enumerate(lodes):
        if lode["id"] == lode_id:
            archived = lodes.pop(i)
            archived["archived_at"] = current_time_ms()

            # Append to archive file
            archived_file = config.hopper_dir() / "archived.jsonl"
            archived_file.parent.mkdir(parents=True, exist_ok=True)
            with open(archived_file, "a") as f:
                f.write(json.dumps(archived) + "\n")

            save_lodes(lodes)
            return archived
    return None


def archive_lode_for_action(
    active_lodes: list[dict], archived_lodes: list[dict], lode_id: str, action_id: str
) -> dict:
    """Publish exactly one archive record for a durable lode action.

    The archive snapshot is persisted before the active twin is removed. A
    retry therefore converges after a crash on either side of the active-file
    rewrite.
    """
    archived_matches = [lode for lode in archived_lodes if lode.get("id") == lode_id]
    if any(lode.get("archive_action_id") != action_id for lode in archived_matches):
        raise ValueError("lode is archived by a different action")
    active_matches = [lode for lode in active_lodes if lode.get("id") == lode_id]
    if len(active_matches) > 1:
        raise ValueError("active lode identity is duplicated")
    if active_matches and active_matches[0].get("archive_action_id") not in {None, action_id}:
        raise ValueError("active lode belongs to a different archive action")
    if not archived_matches and not active_matches:
        raise ValueError("action lode is absent from active and archived storage")

    if archived_matches:
        archived = archived_matches[0]
    else:
        archived = dict(active_matches[0])
        archived["archive_action_id"] = action_id
        archived["archived_at"] = current_time_ms()

    prior_archived = list(archived_lodes)
    archived_lodes[:] = [lode for lode in archived_lodes if lode.get("id") != lode_id] + [archived]
    try:
        save_archived_lodes(archived_lodes)
    except Exception:
        archived_lodes[:] = prior_archived
        raise

    prior_active = list(active_lodes)
    active_lodes[:] = [lode for lode in active_lodes if lode.get("id") != lode_id]
    try:
        save_lodes(active_lodes)
    except Exception:
        active_lodes[:] = prior_active
        raise
    return archived


def unarchive_lode(
    archived_lodes: list[dict], active_lodes: list[dict], lode_id: str
) -> dict | None:
    """Unarchive a lode: move from archived list back to active list.

    Returns the unarchived lode or None if not found.
    """
    for i, lode in enumerate(archived_lodes):
        if lode["id"] == lode_id:
            restored = archived_lodes.pop(i)
            restored.pop("archived_at", None)
            active_lodes.append(restored)

            save_archived_lodes(archived_lodes)
            save_lodes(active_lodes)
            return restored
    return None


def update_lode_state(lodes: list[dict], lode_id: str, state: str, status: str) -> dict | None:
    """Update a lode's state and status. Returns the updated lode or None if not found."""
    for lode in lodes:
        if lode["id"] == lode_id:
            _set_lode_state_fields(lode, state, status)
            touch(lode)
            save_lodes(lodes)
            return lode
    return None


def _set_lode_state_fields(lode: dict, state: str, status: str) -> None:
    """Apply state fields and their existing runtime bookkeeping without saving."""
    previous_state = lode.get("state")
    lode["state"] = state
    lode["status"] = status
    lode["spawn_disposition"] = None
    if state == "error":
        if previous_state != "error" or type(lode.get("errored_at")) is not int:
            lode["errored_at"] = current_time_ms()
    else:
        lode["errored_at"] = None
    stage = lode.get("stage", "")
    if stage in _STAGE_NAMES:
        runs = lode.setdefault("runs", {})
        now = current_time_ms()
        if state == "running":
            stage_run = runs.get(stage, {})
            if "started_at" not in stage_run or "stopped_at" in stage_run:
                runs[stage] = {"started_at": now}
        elif state in ("error", "ready"):
            stage_run = runs.get(stage, {})
            if "started_at" in stage_run:
                stage_run["stopped_at"] = now
                runs[stage] = stage_run


def publish_lode_gate(
    lodes: list[dict],
    lode_id: str,
    *,
    body: str,
    kind: str,
    status: str,
) -> tuple[dict | None, bool]:
    """Atomically publish a coherent durable gate and return whether it is new."""
    for lode in lodes:
        if lode["id"] != lode_id:
            continue
        prior = copy.deepcopy(lode)
        if not set_lode_gate_fields(lode, body=body, kind=kind, status=status):
            return lode, False
        touch(lode)
        try:
            save_lodes(lodes)
        except Exception:
            lode.clear()
            lode.update(prior)
            raise
        return lode, True
    return None, False


def set_lode_gate_fields(lode: dict, *, body: str, kind: str, status: str) -> bool:
    """Apply one coherent gate publication without saving; return whether it changed."""
    if not isinstance(body, str) or not body:
        raise ValueError("gate body must be a non-empty string")
    if kind not in GATE_KINDS:
        raise ValueError("gate kind is not supported")
    gate = lode_gate(lode)
    if gate is not None and gate["kind"] == kind and gate["body"] == body:
        return False
    lode["gate_body"] = body
    lode["gate_kind"] = kind
    lode["gate_epoch"] += 1
    _set_lode_state_fields(lode, "gated", status)
    return True


def begin_lode_gate_delivery(
    lodes: list[dict], lode_id: str
) -> tuple[dict | None, tuple[int, int] | None]:
    """Durably fence one operator delivery against the current gate instance."""
    for lode in lodes:
        if lode["id"] != lode_id:
            continue
        prior = copy.deepcopy(lode)
        gate = lode_gate(lode)
        if gate is None:
            return lode, None
        lode["gate_epoch"] += 1
        lode["gate_delivery_epoch"] += 1
        touch(lode)
        try:
            save_lodes(lodes)
        except Exception:
            lode.clear()
            lode.update(prior)
            raise
        return lode, (lode["gate_epoch"], lode["gate_delivery_epoch"])
    return None, None


def clear_lode_gate(
    lodes: list[dict],
    lode_id: str,
    *,
    gate_epoch: int,
    kind: str,
    state: str,
    status: str,
    delivery_epoch: int | None = None,
) -> dict | None:
    """Atomically clear one exact gate instance, refusing stale/mixed authority."""
    for lode in lodes:
        if lode["id"] != lode_id:
            continue
        prior = copy.deepcopy(lode)
        gate = lode_gate(lode)
        if (
            gate is None
            or gate["epoch"] != gate_epoch
            or gate["kind"] != kind
            or (delivery_epoch is not None and gate["delivery_epoch"] != delivery_epoch)
        ):
            return None
        lode["gate_body"] = None
        lode["gate_kind"] = None
        _set_lode_state_fields(lode, state, status)
        touch(lode)
        try:
            save_lodes(lodes)
        except Exception:
            lode.clear()
            lode.update(prior)
            raise
        return lode
    return None


def write_lode_gate_artifact(lode: dict) -> None:
    """Write derived gate.md after durable publication; it is never read as authority."""
    gate = lode_gate(lode)
    path = get_lode_dir(lode["id"]) / "gate.md"
    if gate is None:
        path.unlink(missing_ok=True)
        return
    tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp_path, "w") as artifact:
            artifact.write(gate["body"])
            artifact.flush()
            os.fsync(artifact.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def stop_lode_runtime(lode: dict, *, stopped_at: int | None = None) -> bool:
    """Close the current stage's open runtime interval exactly once."""
    stage = lode.get("stage")
    if stage not in ("mill", "refine", "ship"):
        return False
    stage_run = lode.setdefault("runs", {}).get(stage)
    if not isinstance(stage_run, dict) or "started_at" not in stage_run:
        return False
    if "stopped_at" in stage_run:
        return False
    stage_run["stopped_at"] = stopped_at if stopped_at is not None else current_time_ms()
    return True


def update_lode_status(lodes: list[dict], lode_id: str, status: str) -> dict | None:
    """Update a lode's status text only. Returns the updated lode or None if not found."""
    return _update_lode_field(lodes, lode_id, "status", status)


def update_lode_title(lodes: list[dict], lode_id: str, title: str) -> dict | None:
    """Update a lode's title only. Returns the updated lode or None if not found."""
    return _update_lode_field(lodes, lode_id, "title", title)


def update_lode_branch(lodes: list[dict], lode_id: str, branch: str) -> dict | None:
    """Update a lode's branch only. Returns the updated lode or None if not found."""
    return _update_lode_field(lodes, lode_id, "branch", branch)


def update_lode_worktree_path(lodes: list[dict], lode_id: str, worktree_path: str) -> dict | None:
    """Update durable worktree provenance. Returns the updated lode or None."""
    return _update_lode_field(lodes, lode_id, "worktree_path", worktree_path)


def lode_coder(lode: dict) -> tuple[str, str | None]:
    """Return the selected coder and session; absence means Codex."""
    if "coder" not in lode:
        return "codex", lode.get("codex_thread_id")
    coder = lode["coder"]
    if not isinstance(coder, dict):
        raise ValueError("lode contains invalid coder data")
    provider = validate_coder_provider(coder.get("provider"))
    if provider == "codex":
        raise ValueError("Codex lodes must use codex_thread_id")
    session_id = coder.get("session_id")
    if session_id is not None and not isinstance(session_id, str):
        raise ValueError("lode contains invalid coder session_id")
    return provider, session_id


def update_lode_codex_thread(lodes: list[dict], lode_id: str, codex_thread_id: str) -> dict | None:
    """Update the Codex thread ID on a lode."""
    for lode in lodes:
        if lode["id"] != lode_id:
            continue
        provider, _session_id = lode_coder(lode)
        if provider != "codex":
            raise ValueError("Codex thread IDs can only be stored on Codex lodes")
        return _update_lode_field(lodes, lode_id, "codex_thread_id", codex_thread_id)
    return None


def update_lode_coder_session(
    lodes: list[dict], lode_id: str, provider: str, session_id: str
) -> dict | None:
    """Store a session only when it belongs to the lode's selected coder."""
    provider = validate_coder_provider(provider)
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("coder session_id must be a non-empty string")
    if provider == "codex":
        return None
    for lode in lodes:
        if lode["id"] != lode_id:
            continue
        selected_provider, _selected_session = lode_coder(lode)
        if selected_provider != provider:
            return None
        lode["coder"]["session_id"] = session_id
        touch(lode)
        save_lodes(lodes)
        return lode
    return None


def validate_lode_coder_data(lodes: list[dict], source: str) -> None:
    """Validate non-Codex coder data without touching absent-coder Codex rows."""
    for lode in lodes:
        if "coder" not in lode:
            continue
        try:
            lode_coder(lode)
        except ValueError as error:
            raise ValueError(f"{source} contains invalid coder data: {error}") from error


def validate_lode_driver_data(lodes: list[dict], source: str) -> None:
    """Validate interactive-stage data without rewriting a loaded snapshot."""
    for lode in lodes:
        try:
            normalize_lode_stage_sessions(lode)
        except ValueError as error:
            raise ValueError(f"{source} contains invalid lode driver data: {error}") from error


def set_lode_stage_session_started(lodes: list[dict], lode_id: str, stage: str) -> dict | None:
    """Mark one canonical stage session as started and save both projections."""
    for lode in lodes:
        if lode["id"] != lode_id:
            continue
        normalize_lode_stage_sessions(lode)
        if stage not in _STAGE_NAMES:
            return None
        lode_stage_session(lode, stage)["started"] = True
        project_lode_claude_state(lode)
        touch(lode)
        save_lodes(lodes)
        return lode
    return None


def bind_lode_stage_session(
    lodes: list[dict],
    lode_id: str,
    *,
    driver: str,
    stage: str,
    launch_id: str,
    provider_session_id: str,
    run_generation: str,
) -> tuple[dict | None, str]:
    """Commit one fenced provider start attempt and both durable projections.

    The stage-local attempt is the idempotency record. An exact replay reports
    its committed outcome; a differing replay is an identity conflict.
    """
    for lode in lodes:
        if lode["id"] != lode_id:
            continue
        normalize_lode_stage_sessions(lode)
        if lode_driver(lode) != driver:
            raise ValueError("driver does not match the durable lode")
        session = lode_stage_session(lode, stage)
        attempt = {
            "driver": driver,
            "stage": stage,
            "launch_id": launch_id,
            "provider_session_id": provider_session_id,
            "run_generation": run_generation,
            "outcome": "committed",
        }
        existing = session["start_attempt"]
        if existing is not None:
            if existing == attempt:
                return lode, "committed"
            raise ValueError("start attempt conflicts with the committed durable attempt")
        expected_launch_id = _expected_launch_id(lode_id, stage, provider_session_id)
        if launch_id != expected_launch_id:
            raise ValueError("launch_id does not match the provider session")
        if (
            session["launch_id"] != launch_id
            or session["provider_session_id"] != provider_session_id
        ):
            if driver != "codex" or session["started"]:
                raise ValueError("stage identity does not match the durable stage session")
            # Codex chooses its thread UUID. Replace the provisional pair only
            # as part of this first, generation-fenced binding commit.
            session["launch_id"] = launch_id
            session["provider_session_id"] = provider_session_id
        session["started"] = True
        session["start_attempt"] = attempt
        project_lode_claude_state(lode)
        touch(lode)
        save_lodes(lodes)
        return lode, "committed"
    return None, "lode_not_found"


def reset_lode_stage_session(
    lodes: list[dict],
    lode_id: str,
    stage: str,
    *,
    persist: bool = True,
    session_id: str | None = None,
) -> dict | None:
    """Replace one stage's provider identity and reset its canonical state."""
    for lode in lodes:
        if lode["id"] != lode_id:
            continue
        normalize_lode_stage_sessions(lode)
        if stage not in _STAGE_NAMES:
            return None
        provider_session_id = session_id or str(uuid.uuid4())
        _validate_uuid(provider_session_id, lode, stage, "provider_session_id")
        lode["stage_sessions"][stage] = {
            "launch_id": _expected_launch_id(lode["id"], stage, provider_session_id),
            "provider_session_id": provider_session_id,
            "transcript_path": None,
            "started": False,
            "start_attempt": None,
        }
        project_lode_claude_state(lode)
        lode["last_progress_at"] = None
        lode["last_progress_summary"] = ""
        lode["last_pane_activity_at"] = None
        lode["pane_title_observation"] = None
        if persist:
            touch(lode)
            save_lodes(lodes)
        return lode
    return None


def set_lode_claude_started(lodes: list[dict], lode_id: str, claude_stage: str) -> dict | None:
    """Mark a Claude compatibility stage as started through canonical state."""
    return set_lode_stage_session_started(lodes, lode_id, claude_stage)


def reset_lode_claude_stage(
    lodes: list[dict],
    lode_id: str,
    claude_stage: str,
    *,
    persist: bool = True,
    session_id: str | None = None,
) -> dict | None:
    """Reset a Claude compatibility stage through canonical session state."""
    return reset_lode_stage_session(
        lodes,
        lode_id,
        claude_stage,
        persist=persist,
        session_id=session_id,
    )


def find_lodes_by_prefix(lodes: list[dict], prefix: str) -> list[dict]:
    """Find all lodes matching an ID prefix."""
    return [lode for lode in lodes if lode["id"].startswith(prefix)]


def find_lode_by_prefix(lodes: list[dict], prefix: str) -> dict | None:
    """Find a lode by ID prefix. Returns None if not found or ambiguous."""
    matches = find_lodes_by_prefix(lodes, prefix)
    if len(matches) == 1:
        return matches[0]
    return None


# --- Status rendering ---

PARK_STATUS_TEMPLATE = """Parked (idle): {reason}. The agent is ALIVE and was NOT terminated. Inspect: hop lode peek {lode_id} | Resume: hop lode nudge {lode_id} (or hop lode answer {lode_id} 1)"""  # noqa: E501

PARK_PANE_GONE_STATUS = """Parked (idle), but the pane is GONE: {reason}. The agent did NOT survive — nudge and answer cannot reach a dead pane. Recover: hop lode restart {lode_id} (check first that the work did not already land: git cherry origin/main {branch})"""  # noqa: E501

OOM_KILLED_STATUS = """OOM-KILLED: the operating system killed this Hopper lode's process group after an out-of-memory event. Automatic restart is suppressed. Inspect the worktree and branch, then recover explicitly: hop lode resume {lode_id} (preserve the stage session) or hop lode restart {lode_id} (fresh stage session)."""  # noqa: E501

RUNNER_EXIT_UNVERIFIED_STATUS = """Runner exit UNVERIFIED: Hopper lost the guarded lode runner before it could classify the scope result. Automatic restart is suppressed. Inspect the worktree and branch, then recover explicitly: hop lode resume {lode_id} (preserve the stage session) or hop lode restart {lode_id} (fresh stage session)."""  # noqa: E501

WORKTREE_REAPED_STATUS = """Worktree auto-reaped: Hopper removed this lode's worktree and branch {age} ago, after {policy}. Recover from a retained remote branch or start a new lode; inspect: hop lode status {lode_id}."""  # noqa: E501

TERMINAL_FAILURE_KINDS = frozenset({"oom", "runner_exit_unverified"})

# The branch advice is the final parenthetical in the constant.
_PARK_PANE_GONE_WITHOUT_BRANCH = PARK_PANE_GONE_STATUS.rsplit(" (", 1)[0]

PARK_LIVENESS_UNVERIFIED_SUFFIX = (
    """ (pane liveness UNVERIFIED — could not probe tmux; treat ALIVE as unconfirmed)"""
)

PANE_LIVENESS_NOT_PROBED = "not_probed"


def format_park_status(reason: str, lode_id: str) -> str:
    """Format the durable status written when an idle lode is parked."""
    return PARK_STATUS_TEMPLATE.format(reason=reason, lode_id=lode_id)


def is_terminal_failure_kind(failure_kind: str | None) -> bool:
    """Return whether failure_kind latches automatic runner launch."""
    return failure_kind in TERMINAL_FAILURE_KINDS


def is_terminal_oom_scope_archive_candidate(lode: dict) -> bool:
    """Return whether an OOM-terminal lode has only a potentially stale scope handle."""
    lode_id = lode.get("id")
    generation = lode.get("run_generation")
    scope = lode.get("oom_scope")
    if (
        not is_canonical_lode_id(lode_id)
        or not isinstance(generation, str)
        or not isinstance(scope, str)
        or lode.get("state") != "error"
        or lode.get("failure_kind") != "oom"
        or lode.get("active")
        or any(lode.get(field) is not None for field in ("tmux_pane", "pid"))
    ):
        return False
    try:
        return scope == oom.scope_unit_name(lode_id, generation)
    except ValueError:
        return False


def format_terminal_failure_status(failure_kind: str, lode_id: str) -> str:
    """Format the canonical durable status for a terminal runner failure."""
    if failure_kind == "oom":
        template = OOM_KILLED_STATUS
    elif failure_kind == "runner_exit_unverified":
        template = RUNNER_EXIT_UNVERIFIED_STATUS
    else:
        raise ValueError(f"unknown terminal failure kind: {failure_kind}")
    return template.format(lode_id=lode_id)


def format_worktree_reaped_status(lode_id: str, worktree_reap: dict) -> str:
    """Format the recovery guidance for a worktree Hopper auto-reaped."""
    policy = {
        "shipped": "the 6-hour shipped retention period",
        "error": "the 48-hour terminal-error retention period",
        "killed": "kill confirmation and a zero-unpushed-commit proof",
    }.get(worktree_reap.get("trigger"), "the configured retention policy")
    return WORKTREE_REAPED_STATUS.format(
        lode_id=lode_id,
        age=format_age(worktree_reap.get("reaped_at")),
        policy=policy,
    )


def _lode_status_and_liveness(
    lode: dict,
    *,
    pane_timeout: float | None = None,
) -> tuple[str, Liveness | None]:
    """Return display status and current local pane evidence, if probed."""
    status = lode.get("status", "")
    if not status:
        return status, None

    lode_id = lode.get("id", "")
    reason_prefix, suffix_template = PARK_STATUS_TEMPLATE.split("{reason}", 1)
    status_suffix = suffix_template.format(lode_id=lode_id)
    if not status.startswith(reason_prefix) or not status.endswith(status_suffix):
        return status, None

    reason = status[len(reason_prefix) : -len(status_suffix)]
    if format_park_status(reason, lode_id) != status:
        return status, None

    if lode.get("host") not in (None, "", "local"):
        return status, None

    pane = lode.get("tmux_pane")
    if not pane:
        liveness = Liveness.GONE
    else:
        try:
            liveness = (
                pane_liveness(pane)
                if pane_timeout is None
                else pane_liveness(pane, timeout=pane_timeout)
            )
        except Exception:
            liveness = Liveness.UNKNOWN

    if liveness is Liveness.ALIVE:
        return status, liveness
    elif liveness is Liveness.GONE:
        branch = lode.get("branch")
        template = PARK_PANE_GONE_STATUS if branch else _PARK_PANE_GONE_WITHOUT_BRANCH
        return template.format(reason=reason, lode_id=lode_id, branch=branch), liveness
    else:
        return status + PARK_LIVENESS_UNVERIFIED_SUFFIX, liveness


def lode_status_for_display(lode: dict, *, pane_timeout: float | None = None) -> str:
    """Return a lode's human-readable status with current local pane evidence."""
    status, _liveness = _lode_status_and_liveness(lode, pane_timeout=pane_timeout)
    return status


def lode_with_status_annotations(lode: dict, *, pane_timeout: float | None = None) -> dict:
    """Return a copied lode with additive display status and pane liveness."""
    status, liveness = _lode_status_and_liveness(lode, pane_timeout=pane_timeout)
    annotated = dict(lode)
    annotated["status_display"] = status
    annotated["pane_liveness"] = (
        liveness.value if liveness is not None else PANE_LIVENESS_NOT_PROBED
    )
    return annotated


# --- Status icon constants ---

STATUS_RUNNING = "●"  # filled circle
STATUS_STUCK = "◐"  # half-filled circle
STATUS_NEW = "○"  # empty circle
STATUS_ERROR = "✗"  # x mark
STATUS_SHIPPED = "✓"
STATUS_GATED = "◇"  # open diamond — paused at gate, awaiting user review
STATUS_TEARDOWN = "◌"  # dotted circle — accepted action is proving containment
STATUS_DISCONNECTED = "⊘"  # circled division slash — runner not connected


def lode_icon(lode: dict) -> str:
    """Derive the status icon for a lode based on its state, stage, and active flag."""
    stage = lode.get("stage", "mill")
    state = lode.get("state", "new")
    if state == "teardown":
        icon = STATUS_TEARDOWN
    elif stage == "shipped":
        icon = STATUS_SHIPPED
    elif state in {"new", "reconnecting"}:
        icon = STATUS_NEW
    elif state == "error":
        icon = STATUS_ERROR
    elif state == "gated":
        icon = STATUS_GATED
    elif state == "stuck":
        icon = STATUS_STUCK
    else:
        icon = STATUS_RUNNING
    if (
        not lode.get("active", False)
        and stage != "shipped"
        and state
        not in {
            "gated",
            "reconnecting",
            "teardown",
        }
    ):
        icon = STATUS_DISCONNECTED
    return icon
