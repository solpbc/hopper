# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Shared configuration for hopper."""

import json
from pathlib import Path

from platformdirs import user_data_dir


def hopper_dir() -> Path:
    """Return the hopper data directory for this user/OS."""
    return Path(user_data_dir("hopper"))


def load_config() -> dict[str, str]:
    """Load user config from config.json.

    Returns:
        Config dict, empty if file doesn't exist.
    """
    config_file = hopper_dir() / "config.json"
    if not config_file.exists():
        return {}
    try:
        return json.loads(config_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_config(config: dict[str, str]) -> None:
    """Save user config to config.json.

    Args:
        config: Config dict to save.
    """
    data_dir = hopper_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    config_file = data_dir / "config.json"
    tmp = config_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(config, indent=2) + "\n")
    tmp.replace(config_file)
