# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Bounded, immutable lifecycle notification transport for the server broadcast path.

Lode lifecycle events leave the ordinary FIFO and reduce, per lode, into at most the two
complete snapshots an absent -> active -> archived sequence needs. Each snapshot is frozen
into publisher-owned wire text when the publisher gains ownership, so nothing a caller does
to its dict afterwards can change what reaches a socket. One writer thread alternates
between the lifecycle reducer and the ordinary FIFO so neither class starves the other.

Membership is reported by the owning mutation, never inferred: ``active-root`` means the
lode lives in the active root, ``archive-root`` means it lives in the archive root. The
emitted event type is derived from the baseline -> target transition, not from the type the
caller declared.
"""

import json
import logging
import math
import threading
from collections import deque
from collections.abc import Callable
from typing import NamedTuple

logger = logging.getLogger(__name__)

ACTIVE_ROOT = "active-root"
ARCHIVE_ROOT = "archive-root"
ROOTS = (ACTIVE_ROOT, ARCHIVE_ROOT)

LODE_CREATED = "lode_created"
LODE_UPDATED = "lode_updated"
LODE_ARCHIVED = "lode_archived"
LODE_UNARCHIVED = "lode_unarchived"
LIFECYCLE_TYPES = frozenset({LODE_CREATED, LODE_UPDATED, LODE_ARCHIVED, LODE_UNARCHIVED})

# Declared type / reported membership pairs that cannot both be true. `lode_updated` is
# deliberately absent: it is valid against either root.
_CONTRADICTIONS = frozenset(
    {
        (LODE_CREATED, ARCHIVE_ROOT),
        (LODE_UNARCHIVED, ARCHIVE_ROOT),
        (LODE_ARCHIVED, ACTIVE_ROOT),
    }
)

LIFECYCLE = "lifecycle"
ORDINARY = "ordinary"


class Selection(NamedTuple):
    """One unit of writer work: either frozen lifecycle bytes or an ordinary FIFO turn."""

    kind: str
    data: bytes | None
    cohort: tuple | None


def lifecycle_refusal(message: dict, root: str | None) -> str | None:
    """Return why this lifecycle message cannot be owned, or None when it is publishable."""
    declared = message.get("type")
    if not isinstance(declared, str) or declared not in LIFECYCLE_TYPES:
        return f"lifecycle event type must be one of {sorted(LIFECYCLE_TYPES)}, got {declared!r}"
    if root not in ROOTS:
        return f"{declared} requires authoritative root membership, got {root!r}"
    if (declared, root) in _CONTRADICTIONS:
        return f"{declared} contradicts reported {root} membership"
    lode = message.get("lode")
    if not isinstance(lode, dict):
        return f"{declared} requires a lode mapping, got {type(lode).__name__}"
    lode_id = lode.get("id")
    if not isinstance(lode_id, str) or not lode_id:
        return f"{declared} requires a non-empty string lode id, got {lode_id!r}"
    for field in ("project", "stage", "state", "status"):
        value = lode.get(field)
        if not isinstance(value, str):
            return f"{declared} requires string lode {field}, got {type(value).__name__}"
    if not isinstance(lode.get("active"), bool):
        return f"{declared} requires boolean lode active, got {type(lode.get('active')).__name__}"
    return None


def freeze_lode(lode: dict) -> str:
    """Freeze one strict JSON value tree into publisher-owned wire text."""

    def strict_json(value) -> bool:
        if value is None or isinstance(value, (str, bool, int)):
            return True
        if isinstance(value, float):
            return math.isfinite(value)
        if isinstance(value, list):
            return all(strict_json(item) for item in value)
        if isinstance(value, dict):
            return all(isinstance(key, str) and strict_json(item) for key, item in value.items())
        return False

    if not strict_json(lode):
        raise TypeError("lode is not a strict JSON value tree")
    return json.dumps(lode, allow_nan=False)


def compose_envelope(event_type: str, payload: str, ts: int) -> bytes:
    """Build the wire frame from an already-frozen payload. Infallible by construction."""
    return f'{{"type": "{event_type}", "lode": {payload}, "ts": {ts}}}\n'.encode()


def build_baseline(active_ids, archived_ids) -> dict[str, str]:
    """Map every known lode id to its root. Raises on duplicate or cross-root ids."""
    mapping: dict[str, str] = {}
    for root, ids in ((ACTIVE_ROOT, active_ids), (ARCHIVE_ROOT, archived_ids)):
        for lode_id in ids:
            if lode_id in mapping:
                raise ValueError(
                    f"lode {lode_id} is not unique: already seeded as {mapping[lode_id]}, "
                    f"also present in {root}"
                )
            mapping[lode_id] = root
    return mapping


def emitted_type(baseline: str | None, target: str, declared: str) -> str:
    """Derive the wire event type from the membership transition the consumer must learn."""
    if baseline is None:
        return LODE_CREATED
    if baseline == target:
        # Membership did not move, so only an already-archived lode may still say "archived".
        return LODE_ARCHIVED if declared == LODE_ARCHIVED else LODE_UPDATED
    return LODE_UNARCHIVED if target == ACTIVE_ROOT else LODE_ARCHIVED


class LifecycleTransport:
    """Per-lode reduced lifecycle state plus the bilateral writer schedule.

    Stateful lifecycle by nature: it owns the pending envelopes, the scheduling claims, the
    baseline membership map, and the condition the writer thread parks on, all of which live
    for as long as the server does.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._baseline: dict[str, str] = {}
        self._pending: dict[str, dict] = {}
        self._claims: deque[str] = deque()
        self._last_kind: str | None = None
        self._closed = False
        self._refusal: str | None = None

    # -- baseline -------------------------------------------------------------------

    def seed(self, active_ids, archived_ids) -> None:
        """Adopt complete root membership as the baseline. No synthetic events are emitted."""
        mapping = build_baseline(active_ids, archived_ids)
        with self._cond:
            if self._closed:
                raise RuntimeError("lifecycle transport is closed")
            if mapping != self._baseline:
                self._discard_pending_locked("baseline reseed")
                self._baseline = mapping
            self._refusal = None

    def disable(self, reason: str) -> None:
        """Refuse every further lifecycle publish until a valid baseline is seeded."""
        with self._cond:
            if self._closed:
                return
            self._discard_pending_locked(f"baseline refusal: {reason}")
            self._refusal = reason

    @property
    def refusal(self) -> str | None:
        """Why lifecycle publishing is currently refused, or None."""
        with self._cond:
            return self._refusal

    def baseline_root(self, lode_id: str) -> str | None:
        """The root a consumer currently believes this lode lives in, or None if unknown."""
        with self._cond:
            return self._baseline.get(lode_id)

    # -- publish --------------------------------------------------------------------

    def publish(
        self,
        message: dict,
        root: str | None,
        ts: int,
        cohort_factory: Callable[[], tuple],
    ) -> str | None:
        """Atomically validate, freeze, and own one snapshot, or return a refusal."""
        with self._cond:
            if self._closed:
                return "lifecycle transport is closed"
            if self._refusal is not None:
                return f"lifecycle baseline unusable: {self._refusal}"
            refusal = lifecycle_refusal(message, root)
            if refusal is not None:
                return refusal
            lode = message["lode"]
            try:
                payload = freeze_lode(lode)
            except Exception as error:
                return f"{message['type']} lode serialization refused: {error}"

            lode_id = lode["id"]
            declared = message["type"]
            entry = self._pending.get(lode_id)
            if entry is None:
                try:
                    cohort = cohort_factory()
                except Exception as error:
                    return f"{declared} cohort capture refused: {error}"
                entry = {
                    "baseline": self._baseline.get(lode_id),
                    "cohort": cohort,
                    "active_payload": None,
                    "active_ts": None,
                }
                self._pending[lode_id] = entry
                self._claims.append(lode_id)
            entry["target"] = root
            entry["payload"] = payload
            entry["ts"] = ts
            entry["declared"] = declared
            if entry["baseline"] is None and root == ACTIVE_ROOT:
                # The only second snapshot worth keeping: what "created" must carry when the
                # lode reaches the archive root before the consumer has heard of it at all.
                entry["active_payload"] = payload
                entry["active_ts"] = ts
            self._cond.notify()
            return None

    # -- schedule -------------------------------------------------------------------

    def select(self, ordinary_ready: Callable[[], bool], timeout: float) -> Selection | None:
        """Park until work exists, then alternate classes and take one unit of it."""
        with self._cond:
            self._cond.wait_for(
                lambda: self._closed or bool(self._claims) or ordinary_ready(), timeout
            )
            if self._closed:
                return None
            lifecycle_ready = bool(self._claims)
            fifo_ready = ordinary_ready()
            if lifecycle_ready and fifo_ready:
                kind = ORDINARY if self._last_kind == LIFECYCLE else LIFECYCLE
            elif lifecycle_ready:
                kind = LIFECYCLE
            elif fifo_ready:
                kind = ORDINARY
            else:
                return None
            self._last_kind = kind
            if kind == ORDINARY:
                return Selection(ORDINARY, None, None)
            return self._take_locked()

    def _take_locked(self) -> Selection:
        """Reduce the head lode's pending state into exactly one immutable envelope."""
        lode_id = self._claims.popleft()
        entry = self._pending[lode_id]
        baseline = entry["baseline"]
        target = entry["target"]
        cohort = entry["cohort"]

        if baseline is None and target == ARCHIVE_ROOT:
            # A consumer cannot learn "archived" about a lode it has never heard of, so the
            # sequence spends one envelope announcing the lode, keeping its original cohort.
            payload = entry["active_payload"] or entry["payload"]
            ts = entry["active_ts"] if entry["active_payload"] else entry["ts"]
            entry["baseline"] = ACTIVE_ROOT
            entry["active_payload"] = None
            entry["active_ts"] = None
            self._baseline[lode_id] = ACTIVE_ROOT
            self._claims.append(lode_id)
            return Selection(LIFECYCLE, compose_envelope(LODE_CREATED, payload, ts), cohort)

        event = emitted_type(baseline, target, entry["declared"])
        self._baseline[lode_id] = target
        del self._pending[lode_id]
        return Selection(LIFECYCLE, compose_envelope(event, entry["payload"], entry["ts"]), cohort)

    def wake(self) -> None:
        """Signal the writer that ordinary work arrived."""
        with self._cond:
            self._cond.notify()

    def close(self) -> None:
        """Discard pending envelopes and stale claims, and release the writer."""
        with self._cond:
            self._closed = True
            self._refusal = "lifecycle transport is closed"
            self._discard_pending_locked("transport close")
            self._last_kind = None
            self._cond.notify_all()

    def _discard_pending_locked(self, reason: str) -> None:
        """Discard pending lifecycle work while holding the transport condition."""
        if not self._pending and not self._claims:
            return
        lode_ids = list(self._pending)
        logger.warning(
            "Discarding %d pending lifecycle lodes during %s: %s",
            len(lode_ids),
            reason,
            lode_ids,
        )
        self._pending.clear()
        self._claims.clear()

    # -- observation ----------------------------------------------------------------

    def pending_ids(self) -> list[str]:
        """Lode ids still holding a pending envelope."""
        with self._cond:
            return list(self._pending)

    def claims(self) -> list[str]:
        """Outstanding scheduling claims, in selection order."""
        with self._cond:
            return list(self._claims)
