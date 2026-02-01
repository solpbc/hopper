import sys
from pathlib import Path

from blessed import Terminal
from platformdirs import user_data_dir

DATA_DIR = Path(user_data_dir("hopper"))
SOCKET_PATH = DATA_DIR / "server.sock"


def cmd_up() -> int:
    """Start the server."""
    from hopper.server import start_server
    from hopper.tmux import get_tmux_sessions, is_inside_tmux

    if not is_inside_tmux():
        print("hopper up must run inside tmux.")
        print()
        sessions = get_tmux_sessions()
        if sessions:
            print("You have active tmux sessions. Attach to one and run hopper:")
            print()
            for session in sessions:
                print(f"    tmux attach -t {session}")
            print()
            print("Or start a new session:")
        else:
            print("Start a new tmux session:")
        print()
        print("    tmux new 'hopper up'")
        return 1

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    start_server(SOCKET_PATH)
    return 0


def cmd_ping() -> int:
    """Ping the server."""
    from hopper.client import ping

    if ping(SOCKET_PATH):
        print("pong")
        return 0
    else:
        print("failed to connect")
        return 1


def cmd_tui() -> int:
    """Run the TUI (default command)."""
    from hopper.tui import run_tui

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    term = Terminal()
    return run_tui(term)


def main() -> int:
    """Main entry point with command dispatch."""
    args = sys.argv[1:]

    if not args:
        return cmd_tui()

    command = args[0]

    if command == "up":
        return cmd_up()
    elif command == "ping":
        return cmd_ping()
    else:
        print(f"unknown command: {command}")
        return 1
