#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Migrate Hopper lodes from codex_thread_id to the per-lode coder schema."""

import argparse
import json
import os
import shutil
import sys
import uuid
from pathlib import Path

from hopper import config
from hopper.coder import validate_coder_provider


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"{path}:{line_number}: invalid JSON: {error}") from error
        if not isinstance(row, dict):
            raise ValueError(f"{path}:{line_number}: lode must be a JSON object")
        rows.append(row)
    return rows


def migrate_lode(row: dict) -> tuple[dict, bool]:
    """Return one validated coder-schema lode and whether it changed."""
    migrated = dict(row)
    coder = migrated.get("coder")
    has_legacy = "codex_thread_id" in migrated
    if coder is not None and has_legacy:
        raise ValueError(f"lode {row.get('id', '<unknown>')} has both coder schemas")
    if coder is None:
        if not has_legacy:
            raise ValueError(f"lode {row.get('id', '<unknown>')} has no coder session field")
        session_id = migrated.pop("codex_thread_id")
        if session_id is not None and (not isinstance(session_id, str) or not session_id):
            raise ValueError(f"lode {row.get('id', '<unknown>')} has invalid codex_thread_id")
        migrated["coder"] = {"provider": "codex", "session_id": session_id}
        return migrated, True
    if not isinstance(coder, dict):
        raise ValueError(f"lode {row.get('id', '<unknown>')} has invalid coder data")
    validate_coder_provider(coder.get("provider"))
    session_id = coder.get("session_id")
    if session_id is not None and (not isinstance(session_id, str) or not session_id):
        raise ValueError(f"lode {row.get('id', '<unknown>')} has invalid coder session_id")
    return migrated, False


def _write_jsonl_atomic(path: Path, rows: list[dict]) -> None:
    temp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp_path.open("w") as destination:
            for row in rows:
                destination.write(json.dumps(row) + "\n")
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temp_path, path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def migrate_directory(data_dir: Path, *, apply: bool) -> tuple[int, int]:
    """Validate both stores, then optionally back up and atomically rewrite each."""
    paths = [data_dir / "active.jsonl", data_dir / "archived.jsonl"]
    prepared: dict[Path, list[dict]] = {}
    changed_rows = 0
    for path in paths:
        rows = _read_jsonl(path)
        migrated_rows = []
        for row in rows:
            migrated, changed = migrate_lode(row)
            migrated_rows.append(migrated)
            changed_rows += int(changed)
        prepared[path] = migrated_rows

    changed_files = sum(
        path.exists() and _read_jsonl(path) != rows for path, rows in prepared.items()
    )
    if apply:
        changes = [
            (path, rows)
            for path, rows in prepared.items()
            if path.exists() and _read_jsonl(path) != rows
        ]
        for path, _rows in changes:
            backup = path.with_name(f"{path.name}.pre-coder-schema")
            if backup.exists():
                raise ValueError(f"backup already exists: {backup}")
        for path, rows in changes:
            backup = path.with_name(f"{path.name}.pre-coder-schema")
            shutil.copy2(path, backup)
            _write_jsonl_atomic(path, rows)
    return changed_files, changed_rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=config.hopper_dir())
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the migration after validation (default is a dry run)",
    )
    args = parser.parse_args(argv)
    try:
        changed_files, changed_rows = migrate_directory(args.data_dir, apply=args.apply)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    action = "migrated" if args.apply else "would migrate"
    print(f"{action} {changed_rows} lodes across {changed_files} files")
    if not args.apply and changed_files:
        print("re-run with --apply while the Hopper server is stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
