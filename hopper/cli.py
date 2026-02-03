import argparse
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


class ArgumentError(Exception):
    """Raised when argument parsing fails."""

    pass


def make_parser(cmd: str, description: str) -> argparse.ArgumentParser:
    """Create an argument parser for a subcommand.

    Returns a parser configured with:
    - prog set to 'hop <cmd>' for proper usage lines
    - exit_on_error=False so we can handle errors gracefully
    """
    return argparse.ArgumentParser(
        prog=f"hop {cmd}",
        description=description,
        exit_on_error=False,
    )


def parse_args(parser: argparse.ArgumentParser, args: list[str]) -> argparse.Namespace:
    """Parse arguments, raising ArgumentError on failure."""
    try:
        return parser.parse_args(args)
    except argparse.ArgumentError as e:
        raise ArgumentError(str(e)) from e
    except SystemExit:
        # Raised by argparse for --help (exits with 0)
        raise


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


def require_config_name() -> int | None:
    """Check that 'name' is configured. Returns exit code on failure, None on success."""
    from hopper.config import load_config

    config = load_config()
    if "name" not in config:
        print("Please set your name first:")
        print()
        print("    hop config name <your-name>")
        return 1
    return None


def require_projects() -> int | None:
    """Check that at least one project is configured.

    Returns exit code on failure, None on success.
    """
    from hopper.projects import get_active_projects

    projects = get_active_projects()
    if not projects:
        print("No projects configured. Add a project first:")
        print()
        print("    hop project add <path>")
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
    from hopper.tmux import get_current_tmux_location, get_tmux_sessions, is_inside_tmux

    parser = make_parser("up", "Start the hopper server and TUI (must run inside tmux).")
    try:
        parse_args(parser, args)
    except SystemExit:
        return 0
    except ArgumentError as e:
        print(f"error: {e}")
        parser.print_usage()
        return 1

    if err := require_no_server():
        return err

    if err := require_config_name():
        return err

    if err := require_projects():
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
    tmux_location = get_current_tmux_location()
    return start_server_with_tui(SOCKET_PATH, tmux_location=tmux_location)


@command("ore", "Run Claude for a session")
def cmd_ore(args: list[str]) -> int:
    """Run Claude for a session, managing active/inactive state."""
    from hopper.client import get_session
    from hopper.ore import run_ore

    parser = make_parser("ore", "Run Claude for a session (internal command).")
    parser.add_argument("session_id", help="Session ID to run")
    parser.add_argument(
        "-f", "--force", action="store_true", help="Force connection even if already running"
    )
    try:
        parsed = parse_args(parser, args)
    except SystemExit:
        return 0
    except ArgumentError as e:
        print(f"error: {e}")
        parser.print_usage()
        return 1

    session_id = parsed.session_id

    # Server must be running (but we proceed gracefully if it dies mid-session)
    if err := require_server():
        return err

    # Validate session exists and check state
    session = get_session(SOCKET_PATH, session_id)
    if not session:
        print(f"Session {session_id} not found.")
        return 1

    if session.get("state") == "running" and not parsed.force:
        print(f"Session {session_id[:8]} is already running.")
        print("Use --force to take over the session.")
        return 1

    return run_ore(session_id, SOCKET_PATH)


@command("status", "Show or update session status")
def cmd_status(args: list[str]) -> int:
    """Show or update the current session's status text."""
    from hopper.client import get_session, set_session_status

    parser = make_parser(
        "status",
        "Show or update session status. "
        "Without arguments, displays the current status. "
        "With arguments, sets the status to the provided text.",
    )
    parser.add_argument("text", nargs="*", help="New status text (optional)")
    try:
        parsed = parse_args(parser, args)
    except SystemExit:
        return 0
    except ArgumentError as e:
        print(f"error: {e}")
        parser.print_usage()
        return 1

    if err := require_server():
        return err

    session_id = get_hopper_sid()
    if not session_id:
        print("HOPPER_SID not set. Run this from within a hopper session.")
        return 1

    if err := validate_hopper_sid():
        return err

    if not parsed.text:
        # Show current status
        session = get_session(SOCKET_PATH, session_id)
        if not session:
            print(f"Session {session_id} not found.")
            return 1
        status = session.get("status", "")
        if status:
            print(status)
        else:
            print("(no status)")
        return 0

    # Update status - join all args as the text
    new_status = " ".join(parsed.text)
    if not new_status.strip():
        print("Status text required.")
        return 1

    # Get current status for friendly output
    session = get_session(SOCKET_PATH, session_id)
    old_status = session.get("status", "") if session else ""

    set_session_status(SOCKET_PATH, session_id, new_status)

    if old_status:
        print(f"Updated from '{old_status}' to '{new_status}'")
    else:
        print(f"Updated to '{new_status}'")

    return 0


@command("project", "Manage projects")
def cmd_project(args: list[str]) -> int:
    """Manage projects (git directories for sessions)."""
    from hopper.projects import add_project, load_projects, remove_project

    parser = make_parser(
        "project",
        "Manage projects. Projects are git directories where sessions run.",
    )
    parser.add_argument(
        "action",
        nargs="?",
        choices=["add", "remove", "list"],
        default="list",
        help="Action to perform (default: list)",
    )
    parser.add_argument("path", nargs="?", help="Path (for add) or name (for remove)")
    try:
        parsed = parse_args(parser, args)
    except SystemExit:
        return 0
    except ArgumentError as e:
        print(f"error: {e}")
        parser.print_usage()
        return 1

    if parsed.action == "list":
        projects = load_projects()
        if not projects:
            print("No projects configured. Use: hop project add <path>")
            return 0
        for p in projects:
            status = " (disabled)" if p.disabled else ""
            print(f"{p.name}{status}")
            print(f"  {p.path}")
        return 0

    if parsed.action == "add":
        if not parsed.path:
            print("error: path required for add")
            parser.print_usage()
            return 1
        try:
            project = add_project(parsed.path)
            print(f"Added project: {project.name}")
            print(f"  {project.path}")
            return 0
        except ValueError as e:
            print(f"error: {e}")
            return 1

    if parsed.action == "remove":
        if not parsed.path:
            print("error: name required for remove")
            parser.print_usage()
            return 1
        if remove_project(parsed.path):
            print(f"Disabled project: {parsed.path}")
            return 0
        else:
            print(f"Project not found: {parsed.path}")
            return 1

    return 0


@command("config", "Get or set config values")
def cmd_config(args: list[str]) -> int:
    """Get or set config values used as prompt template variables."""
    from hopper.config import load_config, save_config

    parser = make_parser(
        "config",
        "Get or set config values. Config values are available as $variables in prompts.",
    )
    parser.add_argument("name", nargs="?", help="Config key name")
    parser.add_argument("value", nargs="?", help="Value to set")
    try:
        parsed = parse_args(parser, args)
    except SystemExit:
        return 0
    except ArgumentError as e:
        print(f"error: {e}")
        parser.print_usage()
        return 1

    config = load_config()

    # No args: list all config
    if not parsed.name:
        if not config:
            print("No config set. Use: hop config <name> <value>")
            return 0
        for key, value in sorted(config.items()):
            print(f"{key}={value}")
        return 0

    # One arg: get value
    if not parsed.value:
        if parsed.name in config:
            print(config[parsed.name])
        else:
            print(f"Config '{parsed.name}' not set.")
            return 1
        return 0

    # Two args: set value
    config[parsed.name] = parsed.value
    save_config(config)
    print(f"{parsed.name}={parsed.value}")
    return 0


@command("screenshot", "Capture TUI window as ANSI text")
def cmd_screenshot(args: list[str]) -> int:
    """Capture the TUI window content with ANSI styling."""
    from hopper.client import connect
    from hopper.tmux import capture_pane

    parser = make_parser("screenshot", "Capture the TUI window as ANSI text.")
    try:
        parse_args(parser, args)
    except SystemExit:
        return 0
    except ArgumentError as e:
        print(f"error: {e}")
        parser.print_usage()
        return 1

    if err := require_server():
        return err

    response = connect(SOCKET_PATH)
    if not response:
        print("Failed to connect to server.")
        return 1

    tmux = response.get("tmux")
    if not tmux:
        print("Server was not started inside tmux.")
        return 1

    content = capture_pane(tmux["window"])
    if content is None:
        print(f"Failed to capture tmux window {tmux['window']}.")
        return 1

    print(content, end="")
    return 0


@command("ping", "Check if server is running")
def cmd_ping(args: list[str]) -> int:
    """Ping the server."""
    from hopper.client import connect

    parser = make_parser("ping", "Check if the hopper server is running.")
    try:
        parse_args(parser, args)
    except SystemExit:
        return 0
    except ArgumentError as e:
        print(f"error: {e}")
        parser.print_usage()
        return 1

    session_id = get_hopper_sid()
    response = connect(SOCKET_PATH, session_id=session_id)
    if not response:
        require_server()
        return 1

    # Check session validity if HOPPER_SID was set
    if session_id and not response.get("session_found", False):
        print(f"Session {session_id} not found or archived.")
        print("Unset HOPPER_SID to continue: unset HOPPER_SID")
        return 1

    # Build output
    parts = ["pong"]
    tmux = response.get("tmux")
    if tmux:
        parts.append(f"tmux:{tmux['session']}:{tmux['window']}")
    if session_id:
        parts.append(f"session:{session_id}")
    print(" ".join(parts))
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
