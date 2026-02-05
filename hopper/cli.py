import argparse
import os
import sys
from collections.abc import Callable
from pathlib import Path

import setproctitle

from hopper import __version__, config


def _socket() -> Path:
    """Return the server socket path (late-binding, safe for tests)."""
    return config.hopper_dir() / "server.sock"


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

    if not ping(_socket()):
        print("Server not running. Start it with: hop up")
        return 1
    return None


def require_no_server() -> int | None:
    """Check that the server is NOT running. Returns exit code on failure, None on success."""
    from hopper.client import ping

    if ping(_socket()):
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


def validate_hopper_lid() -> int | None:
    """Validate HOPPER_LID if set. Returns exit code on failure, None on success."""
    from hopper.client import lode_exists

    lode_id = os.environ.get("HOPPER_LID")
    if not lode_id:
        return None

    if not lode_exists(_socket(), lode_id):
        print(f"Lode {lode_id} not found or archived.")
        print("Unset HOPPER_LID to continue: unset HOPPER_LID")
        return 1
    return None


def get_hopper_lid() -> str | None:
    """Get HOPPER_LID from environment if set."""
    return os.environ.get("HOPPER_LID")


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

    config.hopper_dir().mkdir(parents=True, exist_ok=True)
    tmux_location = get_current_tmux_location()
    return start_server_with_tui(_socket(), tmux_location=tmux_location)


@command("ore", "Run Claude for a lode")
def cmd_ore(args: list[str]) -> int:
    """Run Claude for a lode, managing active/inactive state."""
    from hopper.ore import run_ore

    parser = make_parser("ore", "Run Claude for a lode (internal command).")
    parser.add_argument("lode_id", help="Lode ID to run")
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

    return run_ore(parsed.lode_id, _socket())


@command("refine", "Run refine workflow for a lode")
def cmd_refine(args: list[str]) -> int:
    """Run Claude with refine prompt in a git worktree."""
    from hopper.refine import run_refine

    parser = make_parser("refine", "Run refine workflow for a processing-stage lode.")
    parser.add_argument("lode_id", help="Lode ID to run")
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

    return run_refine(parsed.lode_id, _socket())


@command("ship", "Run ship workflow for a lode")
def cmd_ship(args: list[str]) -> int:
    """Run Claude to merge feature branch back to main."""
    from hopper.ship import run_ship

    parser = make_parser("ship", "Run ship workflow for a ship-stage lode.")
    parser.add_argument("lode_id", help="Lode ID to run")
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

    return run_ship(parsed.lode_id, _socket())


@command("status", "Show or update lode status")
def cmd_status(args: list[str]) -> int:
    """Show or update the current lode's status text."""
    from hopper.client import get_lode, set_lode_status

    parser = make_parser(
        "status",
        "Show or update lode status. "
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

    lode_id = get_hopper_lid()
    if not lode_id:
        print("HOPPER_LID not set. Run this from within a hopper lode.")
        return 1

    if err := validate_hopper_lid():
        return err

    if not parsed.text:
        # Show current status
        lode = get_lode(_socket(), lode_id)
        if not lode:
            print(f"Lode {lode_id} not found.")
            return 1
        status = lode.get("status", "")
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
    lode = get_lode(_socket(), lode_id)
    old_status = lode.get("status", "") if lode else ""

    set_lode_status(_socket(), lode_id, new_status)

    if old_status:
        print(f"Updated from '{old_status}' to '{new_status}'")
    else:
        print(f"Updated to '{new_status}'")

    return 0


@command("project", "Manage projects")
def cmd_project(args: list[str]) -> int:
    """Manage projects (git directories for lodes)."""
    from hopper.projects import add_project, load_projects, remove_project

    parser = make_parser(
        "project",
        "Manage projects. Projects are git directories where lodes run.",
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

    response = connect(_socket())
    if not response:
        print("Failed to connect to server.")
        return 1

    tmux = response.get("tmux")
    if not tmux:
        print("Server was not started inside tmux.")
        return 1

    content = capture_pane(tmux["pane"])
    if content is None:
        print(f"Failed to capture tmux pane {tmux['pane']}.")
        return 1

    print(content, end="")
    return 0


@command("shovel", "Save a shovel-ready prompt for a lode")
def cmd_shovel(args: list[str]) -> int:
    """Read a shovel-ready prompt from stdin and save it to the lode directory."""
    from hopper.client import set_lode_state
    from hopper.lodes import get_lode_dir

    parser = make_parser(
        "shovel",
        "Read a shovel-ready prompt from stdin and save it to the lode directory. "
        "Usage: hop shovel <<'EOF'\n<prompt>\nEOF",
    )
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

    lode_id = get_hopper_lid()
    if not lode_id:
        print("HOPPER_LID not set. Run this from within a hopper lode.")
        return 1

    if err := validate_hopper_lid():
        return err

    # Read prompt from stdin
    prompt = sys.stdin.read()
    if not prompt.strip():
        print("No input received. Pipe a shovel-ready prompt via stdin.")
        return 1

    # Write to lode directory
    lode_dir = get_lode_dir(lode_id)
    lode_dir.mkdir(parents=True, exist_ok=True)
    shovel_path = lode_dir / "shovel.md"
    tmp_path = shovel_path.with_suffix(".md.tmp")
    tmp_path.write_text(prompt)
    os.replace(tmp_path, shovel_path)

    # Update lode status
    set_lode_state(_socket(), lode_id, "completed", "Shovel complete")

    print(f"Saved to {shovel_path}")
    return 0


@command("refined", "Signal that refine workflow is complete")
def cmd_refined(args: list[str]) -> int:
    """Signal that the refine workflow is complete for this lode."""
    from hopper.client import set_lode_state

    parser = make_parser(
        "refined",
        "Signal that the refine workflow is complete. "
        "Called by Claude from within a refine lode.",
    )
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

    lode_id = get_hopper_lid()
    if not lode_id:
        print("HOPPER_LID not set. Run this from within a hopper lode.")
        return 1

    if err := validate_hopper_lid():
        return err

    set_lode_state(_socket(), lode_id, "completed", "Refine complete")

    print("Refine complete.")
    return 0


@command("shipped", "Signal that ship workflow is complete")
def cmd_shipped(args: list[str]) -> int:
    """Signal that the ship workflow is complete for this lode."""
    from hopper.client import set_lode_state

    parser = make_parser(
        "shipped",
        "Signal that the ship workflow is complete. " "Called by Claude from within a ship lode.",
    )
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

    lode_id = get_hopper_lid()
    if not lode_id:
        print("HOPPER_LID not set. Run this from within a hopper lode.")
        return 1

    if err := validate_hopper_lid():
        return err

    set_lode_state(_socket(), lode_id, "completed", "Ship complete")

    print("Ship complete.")
    return 0


@command("code", "Run a stage prompt via Codex")
def cmd_code(args: list[str]) -> int:
    """Run a stage prompt via Codex, resuming the lode's Codex thread."""
    from hopper.code import run_code

    parser = make_parser("code", "Run a prompts/<stage>.md file via Codex for a lode.")
    parser.add_argument("stage", help="Stage name (matches prompts/<stage>.md)")
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

    lode_id = get_hopper_lid()
    if not lode_id:
        print("HOPPER_LID not set. Run this from within a hopper lode.")
        return 1

    if err := validate_hopper_lid():
        return err

    # Read directions from stdin (heredoc)
    request = sys.stdin.read().strip()
    if not request:
        print("No directions provided. Use: hop code <stage> <<'EOF'\\n<directions>\\nEOF")
        return 1

    return run_code(lode_id, _socket(), parsed.stage, request)


@command("backlog", "Manage backlog items")
def cmd_backlog(args: list[str]) -> int:
    """Manage backlog items (list, add, remove)."""
    from hopper.backlog import (
        add_backlog_item,
        find_by_prefix,
        load_backlog,
        remove_backlog_item,
    )
    from hopper.client import add_backlog, get_lode, ping, remove_backlog
    from hopper.lodes import format_age

    parser = make_parser(
        "backlog",
        "Manage backlog items. Items track future work for projects.",
    )
    parser.add_argument(
        "action",
        nargs="?",
        choices=["add", "remove", "list"],
        default="list",
        help="Action to perform (default: list)",
    )
    parser.add_argument("text", nargs="*", help="Description (for add) or ID prefix (for remove)")
    parser.add_argument("--project", "-p", help="Project name (required if no active lode)")
    try:
        parsed = parse_args(parser, args)
    except SystemExit:
        return 0
    except ArgumentError as e:
        print(f"error: {e}")
        parser.print_usage()
        return 1

    if parsed.action == "list":
        items = load_backlog()
        if not items:
            print("No backlog items. Use: hop backlog add <description>")
            return 0
        for item in items:
            age = format_age(item.created_at)
            print(f"  {item.id}  {item.project:<16} {item.description}  ({age})")
        return 0

    if parsed.action == "add":
        if not parsed.text:
            print("error: description required for add")
            parser.print_usage()
            return 1

        description = " ".join(parsed.text)
        project = parsed.project
        lode_id = get_hopper_lid()

        # Resolve project from lode if not provided
        if not project and lode_id:
            if err := require_server():
                return err
            lode = get_lode(_socket(), lode_id)
            if lode:
                project = lode.get("project", "")

        if not project:
            print("error: --project required (no active lode to resolve from)")
            return 1

        # Route through server if running, otherwise write directly
        server_running = ping(_socket())
        if server_running:
            add_backlog(_socket(), project, description, lode_id=lode_id)
        else:
            items = load_backlog()
            add_backlog_item(items, project, description, lode_id=lode_id)

        print(f"Added: [{project}] {description}")
        return 0

    if parsed.action == "remove":
        if not parsed.text:
            print("error: ID prefix required for remove")
            parser.print_usage()
            return 1

        prefix = parsed.text[0]
        items = load_backlog()
        item = find_by_prefix(items, prefix)
        if not item:
            print(f"No unique backlog item matching '{prefix}'")
            return 1

        # Route through server if running, otherwise write directly
        server_running = ping(_socket())
        if server_running:
            remove_backlog(_socket(), item.id)
        else:
            remove_backlog_item(items, item.id)

        print(f"Removed: {item.id} [{item.project}] {item.description}")
        return 0

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

    lode_id = get_hopper_lid()
    response = connect(_socket(), lode_id=lode_id)
    if not response:
        require_server()
        return 1

    # Check lode validity if HOPPER_LID was set
    if lode_id and not response.get("lode_found", False):
        print(f"Lode {lode_id} not found or archived.")
        print("Unset HOPPER_LID to continue: unset HOPPER_LID")
        return 1

    # Build output
    parts = ["pong"]
    tmux = response.get("tmux")
    if tmux:
        parts.append(f"tmux:{tmux['session']}:{tmux['pane']}")
    if lode_id:
        parts.append(f"lode:{lode_id}")
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
