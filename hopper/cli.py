import os
import sys
from collections.abc import Callable

import setproctitle

from hopper import __version__
from hopper.config import DATA_DIR, SOCKET_PATH

# Command registry: name -> (handler, description)
# Handler signature: (args: list[str]) -> int
COMMANDS: dict[str, tuple[Callable[[list[str]], int], str]] = {}


def command(name: str, description: str):
    """Decorator to register a command."""

    def decorator(func):
        COMMANDS[name] = (func, description)
        return func

    return decorator


def print_help() -> None:
    """Print help text."""
    print(f"hop v{__version__} - TUI for managing coding agents")
    print()
    print("Usage: hop <command> [options]")
    print()
    print("Commands:")
    for name, (_, desc) in COMMANDS.items():
        print(f"  {name:<12} {desc}")
    print()
    print("Options:")
    print("  -h, --help   Show this help message")
    print("  --version    Show version number")


def require_server() -> int | None:
    """Check that the server is running. Returns exit code on failure, None on success."""
    from hopper.client import ping

    if not ping(SOCKET_PATH):
        print("Server not running. Start it with: hop up")
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


@command("up", "Start the server and TUI")
def cmd_up(args: list[str]) -> int:
    """Start the server and TUI."""
    from hopper.server import start_server_with_tui
    from hopper.tmux import get_tmux_sessions, is_inside_tmux

    if err := require_no_server():
        return err

    if not is_inside_tmux():
        print("hop up must run inside tmux.")
        print()
        sessions = get_tmux_sessions()
        if sessions:
            print("You have active tmux sessions. Attach to one and run hop:")
            print()
            for session in sessions:
                print(f"    tmux attach -t {session}")
            print()
            print("Or start a new session:")
        else:
            print("Start a new tmux session:")
        print()
        print("    tmux new 'hop up'")
        return 1

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return start_server_with_tui(SOCKET_PATH)


@command("ore", "Run Claude for a session")
def cmd_ore(args: list[str]) -> int:
    """Run Claude for a session, managing active/inactive state."""
    from hopper.client import session_exists
    from hopper.ore import run_ore

    if not args:
        print("Usage: hop ore <session-id>")
        return 1

    session_id = args[0]

    # Server must be running (but we proceed gracefully if it dies mid-session)
    if err := require_server():
        return err

    # Validate session exists
    if not session_exists(SOCKET_PATH, session_id):
        print(f"Session {session_id} not found.")
        return 1

    return run_ore(session_id, SOCKET_PATH)


@command("status", "Show or update session status message")
def cmd_status(args: list[str]) -> int:
    """Show or update the current session's status message."""
    from hopper.client import get_session, set_session_message

    if err := require_server():
        return err

    session_id = get_hopper_sid()
    if not session_id:
        print("HOPPER_SID not set. Run this from within a hopper session.")
        return 1

    if err := validate_hopper_sid():
        return err

    if not args:
        # Show current status
        session = get_session(SOCKET_PATH, session_id)
        if not session:
            print(f"Session {session_id} not found.")
            return 1
        message = session.get("message", "")
        if message:
            print(message)
        else:
            print("(no status message)")
        return 0

    # Update status - join all args as the message
    new_message = " ".join(args)
    if not new_message.strip():
        print("Status message required.")
        return 1

    # Get current message for friendly output
    session = get_session(SOCKET_PATH, session_id)
    old_message = session.get("message", "") if session else ""

    set_session_message(SOCKET_PATH, session_id, new_message)

    if old_message:
        print(f"Updated from '{old_message}' to '{new_message}'")
    else:
        print(f"Updated to '{new_message}'")

    return 0


@command("ping", "Check if server is running")
def cmd_ping(args: list[str]) -> int:
    """Ping the server."""
    from hopper.client import ping

    if not ping(SOCKET_PATH):
        require_server()
        return 1

    if err := validate_hopper_sid():
        return err

    session_id = get_hopper_sid()
    if session_id:
        print(f"pong (session: {session_id})")
    else:
        print("pong")
    return 0


def main() -> int:
    """Main entry point with command dispatch."""
    args = sys.argv[1:]

    # No args or help flags -> show help
    if not args or args[0] in ("-h", "--help", "help"):
        print_help()
        return 0

    # Version flag
    if args[0] == "--version":
        print(f"hop {__version__}")
        return 0

    cmd = args[0]
    cmd_args = args[1:]

    # Check for unknown commands
    if cmd not in COMMANDS:
        print(f"unknown command: {cmd}")
        print()
        print_help()
        return 1

    # Set process title
    setproctitle.setproctitle(f"hop:{cmd}")

    # Dispatch to command handler
    handler, _ = COMMANDS[cmd]
    return handler(cmd_args)
