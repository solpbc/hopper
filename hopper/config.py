"""Shared configuration for hopper."""

import json
from pathlib import Path

from platformdirs import user_data_dir

DATA_DIR = Path(user_data_dir("hopper"))
SOCKET_PATH = DATA_DIR / "server.sock"
SESSIONS_FILE = DATA_DIR / "sessions.jsonl"
ARCHIVED_FILE = DATA_DIR / "archived.jsonl"
SESSIONS_DIR = DATA_DIR / "sessions"
CONFIG_FILE = DATA_DIR / "config.json"


def load_config() -> dict[str, str]:
    """Load user config from config.json.

    Returns:
        Config dict, empty if file doesn't exist.
    """
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_config(config: dict[str, str]) -> None:
    """Save user config to config.json.

    Args:
        config: Config dict to save.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(config, indent=2) + "\n")
    tmp.replace(CONFIG_FILE)
