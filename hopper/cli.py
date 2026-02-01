import os
import sys

from hopper.config import DATA_DIR, SOCKET_PATH

COMMANDS = {"up", "ping"}


def require_server() -> int | None:
    """Check that the server is running. Returns exit code on failure, None on success."""
    from hopper.client import ping

    if not ping(SOCKET_PATH):
        print("Server not running. Start it with: hopper up")
        return 1
    return None


def require_no_server() -> int | None:
    """Check that the server is NOT running. Returns exit code on failure, None on success."""
    from hopper.client import ping

    if ping(SOCKET_PATH):
        print("Server already running.")
        return 1
    return None


def validate_hopper_sid() -> int | None:
    """Validate HOPPER_SID if set. Returns exit code on failure, None on success."""
    from hopper.client import session_exists

    session_id = os.environ.get("HOPPER_SID")
    if not session_id:
        return None

    if not session_exists(SOCKET_PATH, session_id):
        print(f"Session {session_id} not found or archived.")
        print("Unset HOPPER_SID to continue: unset HOPPER_SID")
        return 1
    return None


def get_hopper_sid() -> str | None:
    """Get HOPPER_SID from environment if set."""
    return os.environ.get("HOPPER_SID")


def cmd_up() -> int:
    """Start the server."""
    from hopper.server import start_server
    from hopper.tmux import get_tmux_sessions, is_inside_tmux

    if err := require_no_server():
        return err

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
        session_id = get_hopper_sid()
        if session_id:
            print(f"pong (session: {session_id})")
        else:
            print("pong")
        return 0
    else:
        # Use require_server for consistent error message
        require_server()
        return 1


def cmd_tui() -> int:
    """Run the TUI (default command)."""
    from blessed import Terminal

    from hopper.tui import run_tui

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    term = Terminal()
    return run_tui(term)


def main() -> int:
    """Main entry point with command dispatch."""
    args = sys.argv[1:]

    if not args:
        # Default: TUI - requires server and valid session
        if err := require_server():
            return err
        if err := validate_hopper_sid():
            return err
        return cmd_tui()

    command = args[0]

    # Check for unknown commands first, before health checks
    if command not in COMMANDS:
        print(f"unknown command: {command}")
        return 1

    if command == "up":
        # up checks for no server internally
        return cmd_up()
    elif command == "ping":
        # ping handles its own connectivity check
        return cmd_ping()

    return 1  # unreachable, but satisfies type checker
