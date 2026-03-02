# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import argparse
import os
import sys
import threading
from collections.abc import Callable
from pathlib import Path

import setproctitle

from hopper import __version__, config
from hopper.lodes import find_lode_by_prefix, find_lodes_by_prefix, format_age, lode_icon


def _socket() -> Path:
    """Return the server socket path (late-binding, safe for tests)."""
    return config.hopper_dir() / "server.sock"


# Command registry: name -> (handler, description, group)
# Handler signature: (args: list[str]) -> int
COMMANDS: dict[str, tuple[Callable[[list[str]], int], str, str]] = {}

HELP_GROUPS = [
    ("commands", "Commands"),
    ("aliases", "Aliases"),
    ("lode", "Inside a lode"),
]


def command(name: str, description: str, group: str = "commands"):
    """Decorator to register a command."""

    def decorator(func):
        COMMANDS[name] = (func, description, group)
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
    for group_key, group_label in HELP_GROUPS:
        cmds = [(n, d) for n, (_, d, g) in COMMANDS.items() if g == group_key]
        if cmds:
            print(f"\n{group_label}:")
            for name, desc in cmds:
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
        print("    hop config set name <your-name>")
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


_CODING_AGENTS = {
    "CLAUDECODE": "Claude Code",
    "GEMINI_CLI": "Gemini CLI",
    "CODEX_CI": "Codex",
}


def detect_coding_agent() -> str | None:
    """Return the name of a detected coding agent, or None."""
    for var, name in _CODING_AGENTS.items():
        if os.environ.get(var) == "1":
            return name
    return None


def require_not_coding_agent() -> int | None:
    """Check that we're not inside a coding agent. Returns exit code on failure, None on success."""
    agent = detect_coding_agent()
    if agent:
        var = next(v for v, n in _CODING_AGENTS.items() if n == agent)
        print(f"hop up cannot run inside {agent} (detected {var}=1).")
        print("hop is a TUI that needs its own terminal.")
        return 1
    return None


def require_not_inside_lode() -> int | None:
    lid = get_hopper_lid()
    if lid is not None:
        print(f"Cannot run this command inside lode {lid}.")
        print("Use hop backlog add to queue work instead.")
        return 1
    return None


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

    if err := require_not_coding_agent():
        return err

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


@command("process", "Run Claude for a lode's current stage", group="internal")
def cmd_process(args: list[str]) -> int:
    """Run Claude for a lode, dispatching to the correct stage runner."""
    from hopper.process import run_process

    parser = make_parser("process", "Run Claude for a lode's current stage (internal command).")
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

    return run_process(parsed.lode_id, _socket())


@command("status", "Show or update lode status", group="lode")
def cmd_status(args: list[str]) -> int:
    """Show or update the current lode's status text and title."""
    from hopper.client import get_lode, set_lode_status, set_lode_title

    parser = make_parser(
        "status",
        "Show or update lode status. "
        "Without arguments, displays the current status and title. "
        "With arguments, sets the status text. Use -t to set the title.",
    )
    parser.add_argument("text", nargs="*", help="New status text (optional)")
    parser.add_argument("-t", "--title", default=None, help="Set a short title for this lode")
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
        # Outside a lode — look up a specific lode by ID/prefix
        if parsed.title is not None:
            print("Cannot set title from outside a lode.")
            return 1
        if not parsed.text:
            print("HOPPER_LID not set. Run this from within a hopper lode.")
            return 1
        # First arg is the lode ID/prefix
        lookup_id = parsed.text[0]
        if len(parsed.text) > 1:
            print("Too many arguments. Usage: hop status <lode-id>")
            return 1
        lode, error = _lookup_lode(_socket(), lookup_id)
        if error:
            print(error)
            return 1
        print(format_lode_detail(lode))
        return 0

    if err := validate_hopper_lid():
        return err

    if not parsed.text and parsed.title is None:
        # Show current status
        lode = get_lode(_socket(), lode_id)
        if not lode:
            print(f"Lode {lode_id} not found.")
            return 1
        title = lode.get("title", "")
        status = lode.get("status", "")
        if title:
            print(f"Title: {title}")
        if status:
            print(status)
        else:
            print("(no status)")
        return 0

    if parsed.title is not None:
        set_lode_title(_socket(), lode_id, parsed.title)
        print(f"Title set to '{parsed.title}'")

    if parsed.text:
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
    from hopper.client import reload_projects
    from hopper.projects import (
        add_project,
        load_projects,
        remove_project,
        rename_project,
        rename_project_in_data,
    )

    parser = make_parser(
        "project",
        "Manage projects. Projects are git directories where lodes run.",
    )
    parser.add_argument(
        "action",
        nargs="?",
        choices=["add", "remove", "rename", "list"],
        default="list",
        help="Action to perform (default: list)",
    )
    parser.add_argument("path", nargs="?", help="Path (for add) or name (for remove/rename)")
    parser.add_argument("new_name", nargs="?", help="New name (for rename)")
    try:
        parsed = parse_args(parser, args)
    except SystemExit:
        return 0
    except ArgumentError as e:
        print(f"error: {e}")
        parser.print_usage()
        return 1

    if parsed.action != "rename" and parsed.new_name is not None:
        print(f"error: unexpected argument: {parsed.new_name}")
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

    if parsed.action == "rename":
        if not parsed.path:
            print("error: current name required for rename")
            parser.print_usage()
            return 1
        if not parsed.new_name:
            print("error: new name required for rename")
            parser.print_usage()
            return 1
        try:
            rename_project(parsed.path, parsed.new_name)
            rename_project_in_data(parsed.path, parsed.new_name)
            print(f"Renamed project: {parsed.path} -> {parsed.new_name}")
            try:
                reload_projects(_socket())
            except Exception:
                pass
            return 0
        except ValueError as e:
            print(f"error: {e}")
            return 1

    if parsed.action == "add":
        if not parsed.path:
            print("error: path required for add")
            parser.print_usage()
            return 1
        try:
            project = add_project(parsed.path)
            print(f"Added project: {project.name}")
            print(f"  {project.path}")
            try:
                reload_projects(_socket())
            except Exception:
                pass
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
            try:
                reload_projects(_socket())
            except Exception:
                pass
            return 0
        else:
            print(f"Project not found: {parsed.path}")
            return 1

    return 0


def _is_simple_value(value: object) -> bool:
    """Check if a config value is simple (str, int, float, bool)."""
    return isinstance(value, (str, int, float, bool))


@command("config", "Get or set config values")
def cmd_config(args: list[str]) -> int:
    """Get or set config values used as prompt template variables."""
    from hopper.config import load_config, save_config

    parser = make_parser(
        "config",
        "Get or set config values. Config values are available as $variables in prompts.",
    )
    parser.add_argument(
        "action",
        nargs="?",
        choices=["list", "get", "set", "delete", "json", "path"],
        default="list",
        help="Action to perform (default: list)",
    )
    parser.add_argument("key", nargs="?", help="Config key name")
    parser.add_argument("value", nargs="?", help="Value to set")
    try:
        parsed = parse_args(parser, args)
    except SystemExit:
        return 0
    except ArgumentError as e:
        print(f"error: {e}")
        parser.print_usage()
        return 1

    import json

    if parsed.action == "path":
        print(config.hopper_dir())
        return 0

    cfg = load_config()

    if parsed.action == "json":
        print(json.dumps(cfg, indent=2))
        return 0

    if parsed.action == "delete":
        if not parsed.key:
            print("error: key required for delete")
            parser.print_usage()
            return 1
        if parsed.key not in cfg:
            print(f"Config '{parsed.key}' not set.")
            return 1
        if not _is_simple_value(cfg[parsed.key]):
            print(f"Cannot delete complex key '{parsed.key}'. Use its own command.")
            return 1
        del cfg[parsed.key]
        save_config(cfg)
        print(f"Deleted '{parsed.key}'.")
        return 0

    if parsed.action == "get":
        if not parsed.key:
            print("error: key required for get")
            parser.print_usage()
            return 1
        if parsed.key in cfg:
            print(cfg[parsed.key])
        else:
            print(f"Config '{parsed.key}' not set.")
            return 1
        return 0

    if parsed.action == "set":
        if not parsed.key or not parsed.value:
            print("error: key and value required for set")
            parser.print_usage()
            return 1
        cfg[parsed.key] = parsed.value
        save_config(cfg)
        print(f"{parsed.key}={parsed.value}")
        return 0

    # list (default)
    print(f"config: {config.hopper_dir()}")
    simple = {k: v for k, v in cfg.items() if _is_simple_value(v)}
    if not simple:
        print("No config set. Use: hop config set <key> <value>")
        return 0
    for key, value in sorted(simple.items()):
        print(f"{key}={value}")
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


@command("processed", "Signal stage completion with output", group="lode")
def cmd_processed(args: list[str]) -> int:
    """Read stage output from stdin and signal stage completion."""
    from hopper.client import get_lode, set_lode_state
    from hopper.lodes import get_lode_dir

    parser = make_parser(
        "processed",
        "Read stage output from stdin, save it, and signal completion. "
        "Usage: hop processed <<'EOF'\n<output>\nEOF",
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

    # Get lode's current stage from server
    lode = get_lode(_socket(), lode_id)
    if not lode:
        print(f"Lode {lode_id} not found.")
        return 1

    stage = lode.get("stage", "")
    if not stage:
        print(f"Lode {lode_id} has no stage.")
        return 1

    # Read output from stdin
    output = sys.stdin.read()
    if not output.strip():
        print("No input received. Use: hop processed <<'EOF'\\n<output>\\nEOF")
        return 1

    # Write to lode directory as <stage>_out.md
    lode_dir = get_lode_dir(lode_id)
    lode_dir.mkdir(parents=True, exist_ok=True)
    output_path = lode_dir / f"{stage}_out.md"
    tmp_path = output_path.with_suffix(".md.tmp")
    tmp_path.write_text(output)
    os.replace(tmp_path, output_path)

    # Signal completion
    status = f"{stage.capitalize()} complete"
    set_lode_state(_socket(), lode_id, "completed", status)

    print(f"Saved to {output_path}")
    return 0


@command("gate", "Pause lode at a review gate", group="lode")
def cmd_gate(args: list[str]) -> int:
    """Save gate review doc and pause lode for user review."""
    from hopper.client import get_lode, set_lode_state
    from hopper.lodes import get_lode_dir

    parser = make_parser(
        "gate",
        "Pause at a review gate. Saves review doc from stdin and pauses lode. "
        "Usage: hop gate <<'EOF'\n<review doc>\nEOF",
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

    # Validate lode is in refine stage
    lode = get_lode(_socket(), lode_id)
    if not lode:
        print(f"Lode {lode_id} not found.")
        return 1

    stage = lode.get("stage", "")
    if stage != "refine":
        print(f"Lode {lode_id} is not in refine stage.")
        return 1

    # Read review doc from stdin
    output = sys.stdin.read()
    if not output.strip():
        print("No input received. Use: hop gate <<'EOF'\\n<review doc>\\nEOF")
        return 1

    # Save to lode directory as gate.md
    lode_dir = get_lode_dir(lode_id)
    lode_dir.mkdir(parents=True, exist_ok=True)
    gate_path = lode_dir / "gate.md"
    tmp_path = gate_path.with_suffix(".md.tmp")
    tmp_path.write_text(output)
    os.replace(tmp_path, gate_path)

    # Set lode state to gated
    set_lode_state(_socket(), lode_id, "gated", "Gate")

    print(f"Gate set. Review saved to {gate_path}")
    print("Session will be resumed after review.")
    return 0


@command("code", "Run a stage prompt via Codex", group="lode")
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
    """Manage backlog items (list, add, remove, promote, queue)."""
    from hopper.backlog import (
        add_backlog_item,
        find_by_prefix,
        load_backlog,
        remove_backlog_item,
    )
    from hopper.client import (
        add_backlog,
        get_lode,
        ping,
        promote_backlog,
        remove_backlog,
        set_backlog_queued,
    )
    from hopper.lodes import format_age

    # Normalize 'ls' alias to 'list'
    if args and args[0] == "ls":
        args = ["list"] + args[1:]

    parser = make_parser(
        "backlog",
        "Manage backlog items. Items track future work for projects.",
    )
    parser.add_argument(
        "action",
        nargs="?",
        choices=["list", "add", "remove", "promote", "queue"],
        default="list",
        help="Action to perform (default: list)",
    )
    parser.add_argument(
        "text", nargs="*", help="Description (add) or ID prefix (remove/promote/queue)"
    )
    parser.add_argument("--project", "-p", help="Project name (required if no active lode)")
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Clear queued assignment (for queue action)",
    )
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
        if parsed.text:
            description = " ".join(parsed.text)
        else:
            description = sys.stdin.read().strip()
            if not description:
                print(
                    "Error: no description provided\n"
                    "Use: hop backlog add [-p project] <text...>\n"
                    " or: hop backlog add [-p project] <<'EOF'\n"
                    "<description>\nEOF"
                )
                return 1

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

    if parsed.action == "promote":
        if not parsed.text:
            print("error: ID prefix required for promote")
            parser.print_usage()
            return 1

        if err := require_server():
            return err

        prefix = parsed.text[0]
        items = load_backlog()
        item = find_by_prefix(items, prefix)
        if not item:
            print(f"No unique backlog item matching '{prefix}'")
            return 1

        scope = " ".join(parsed.text[1:]) if len(parsed.text) > 1 else ""
        lode = promote_backlog(_socket(), item.id, scope=scope)
        if lode:
            print(f"Promoted: {lode['id']} [{item.project}] {scope or item.description}")
            return 0

        print("error: promote failed")
        return 1

    if parsed.action == "queue":
        if not parsed.text:
            print("error: ID prefix required for queue")
            parser.print_usage()
            return 1

        prefix = parsed.text[0]

        if err := require_server():
            return err

        items = load_backlog()
        item = find_by_prefix(items, prefix)
        if not item:
            print(f"No unique backlog item matching '{prefix}'")
            return 1

        if parsed.clear:
            set_backlog_queued(_socket(), item.id, None)
            print(f"Cleared queue for: {item.id} [{item.project}] {item.description}")
            return 0

        if len(parsed.text) < 2:
            print("error: lode ID required for queue (or use --clear)")
            return 1

        lode_id = parsed.text[1]
        set_backlog_queued(_socket(), item.id, lode_id)
        print(f"Queued: {item.id} [{item.project}] {item.description} → {lode_id}")
        return 0

    return 0


def format_lode_line(lode: dict) -> str:
    icon = lode_icon(lode)
    stage = lode.get("stage", "mill")
    lid = lode["id"]
    project = lode.get("project", "")
    title = lode.get("title", "")
    status_text = lode.get("status", "")
    return f"  {icon} {stage:<7} {lid}  {project:<16} {title:<28} {status_text}"


def format_lode_detail(lode: dict) -> str:
    """Format a lode as a multi-line detailed view."""
    lines = [format_lode_line(lode)]
    lines.append(f"  id:       {lode.get('id', '')}")
    lines.append(f"  project:  {lode.get('project', '')}")
    lines.append(f"  stage:    {lode.get('stage', '')}")
    lines.append(f"  state:    {lode.get('state', '')}")

    status_text = lode.get("status", "")
    if status_text:
        lines.append(f"  status:   {status_text}")

    title = lode.get("title", "")
    if title:
        lines.append(f"  title:    {title}")

    scope_text = (lode.get("scope", "") or "").strip()
    if scope_text:
        lines.append(f"  scope:    {scope_text.splitlines()[0]}")

    branch = lode.get("branch", "")
    if branch:
        lines.append(f"  branch:   {branch}")

    created_age = format_age(lode.get("created_at", 0))
    updated_at = lode.get("updated_at", 0) or lode.get("created_at", 0)
    updated_age = format_age(updated_at)
    lines.append(f"  created:  {created_age} ago")
    lines.append(f"  updated:  {updated_age} ago")
    lines.append(f"  active:   {'yes' if lode.get('active') else 'no'}")
    return "\n".join(lines)


def _lookup_lode(socket_path, prefix: str) -> tuple[dict | None, str | None]:
    """Look up a lode by ID prefix across active and archived lodes."""
    import hopper.client as client

    active_lodes = client.list_lodes(socket_path)
    archived_lodes = client.list_archived_lodes(socket_path)
    all_lodes = active_lodes + archived_lodes

    lode = find_lode_by_prefix(all_lodes, prefix)
    if lode:
        return lode, None

    matches = find_lodes_by_prefix(all_lodes, prefix)
    if len(matches) > 1:
        ids = ", ".join(match["id"] for match in matches)
        return None, f"Ambiguous prefix '{prefix}', matches: {ids}"
    return None, f"Lode '{prefix}' not found."


def _add_create_args(parser):
    """Add lode create arguments to a parser."""
    parser.add_argument("project", help="Project name")
    parser.add_argument("scope", nargs="*", help="Task scope description")
    parser.formatter_class = argparse.RawDescriptionHelpFormatter
    prog = parser.prog
    parser.epilog = (
        "input methods:\n"
        f'  {prog} <project> "scope text here"       inline string\n'
        f"  {prog} <project> scope text here          multi-word (joined)\n"
        f'  echo "scope" | {prog} <project> -         pipe with stdin marker\n'
        f"  {prog} <project> - <<'EOF'                heredoc with stdin marker\n"
        "    scope text here\n"
        "  EOF\n"
        "\n"
        "scope must be at least 42 characters."
    )


def _create_alias_help(cmd_name: str, description: str, args: list[str]) -> int | None:
    """Show help or handle parse errors for a create alias."""
    p = make_parser(cmd_name, description)
    _add_create_args(p)
    try:
        parse_args(p, args)
    except ArgumentError as e:
        print(f"error: {e}\n")
        p.print_help()
        return 1
    except SystemExit:
        return 0
    return None


@command("lode", "Manage lodes")
def cmd_lode(args: list[str]) -> int:
    """Manage lodes — list, create, restart, watch, wait."""
    import hopper.client as client
    from hopper.projects import find_project

    STAGE_ORDER = {"mill": 0, "refine": 1, "ship": 2, "shipped": 3}

    def format_watch_line(lode: dict) -> str:
        icon = lode_icon(lode)
        lode_id = lode.get("id", "")
        stage = lode.get("stage", "")
        status = lode.get("status", "")
        return f"{icon} {lode_id} {stage}  {status}"

    parser = make_parser("lode", "Manage lodes")
    subs = parser.add_subparsers(dest="subcommand")

    list_p = subs.add_parser(
        "list", aliases=["ls"], help="List lodes (default)", exit_on_error=False
    )
    list_p.add_argument("-a", "--archived", action="store_true", help="Show archived lodes")
    list_p.add_argument("-p", "--project", help="Filter by project name")

    create_p = subs.add_parser("create", help="Create a new lode", exit_on_error=False)
    _add_create_args(create_p)

    restart_p = subs.add_parser("restart", help="Restart an inactive lode", exit_on_error=False)
    restart_p.add_argument("lode_id", help="Lode ID to restart")

    watch_p = subs.add_parser("watch", help="Watch lode status events", exit_on_error=False)
    watch_p.add_argument("lode_id", help="Lode ID to watch")
    wait_p = subs.add_parser("wait", help="Wait for lode to ship", exit_on_error=False)
    wait_p.add_argument("lode_id", help="Lode ID to wait for")
    wait_p.add_argument("--timeout", type=float, default=0, help="Timeout in seconds (0=forever)")
    status_p = subs.add_parser("status", help="Show a lode's status", exit_on_error=False)
    status_p.add_argument("lode_id", help="Lode ID to show")
    show_p = subs.add_parser("show", help="Show a lode's status", exit_on_error=False)
    show_p.add_argument("lode_id", help="Lode ID to show")

    try:
        parsed = parse_args(parser, args)
    except ArgumentError as e:
        print(f"error: {e}\n")
        if args and args[0] == "create":
            create_p.print_help()
        else:
            parser.print_help()
        return 1
    except SystemExit:
        return 0

    subcommand = parsed.subcommand or "list"
    socket_path = _socket()

    if subcommand in ("list", "ls"):
        err = require_server()
        if err:
            return err
        archived = getattr(parsed, "archived", False)
        if archived:
            lodes = client.list_archived_lodes(socket_path)
            lodes.sort(key=lambda lode: lode.get("updated_at", 0), reverse=True)
            project_filter = getattr(parsed, "project", None)
            if project_filter:
                lodes = [lode for lode in lodes if lode.get("project") == project_filter]
            if not lodes:
                print("No archived lodes")
                return 0
        else:
            lodes = client.list_lodes(socket_path)
            lodes = [lode for lode in lodes if lode.get("stage") in STAGE_ORDER]
            lodes.sort(key=lambda lode: STAGE_ORDER.get(lode.get("stage", "mill"), 99))
            project_filter = getattr(parsed, "project", None)
            if project_filter:
                lodes = [lode for lode in lodes if lode.get("project") == project_filter]
            if not lodes:
                print("No active lodes")
                return 0
        for lode in lodes:
            print(format_lode_line(lode))
        return 0

    if subcommand == "create":
        if (rc := require_not_inside_lode()) is not None:
            return rc
        project_name = parsed.project
        if parsed.scope == ["-"]:
            scope = sys.stdin.read().strip()
        elif parsed.scope:
            scope = " ".join(parsed.scope)
        else:
            scope = sys.stdin.read().strip()
        if not scope:
            print("error: no scope provided\n")
            create_p.print_help()
            return 1
        if len(scope) < 42:
            print(f"error: scope too short ({len(scope)} chars, minimum 42)\n")
            create_p.print_help()
            return 1
        project = find_project(project_name)
        if not project:
            print(f"Project not found: {project_name}")
            return 1
        err = require_server()
        if err:
            return err
        lode = client.create_lode(socket_path, project_name, scope, spawn=True)
        if lode:
            print(f"Created lode {lode['id']} ({project_name})")
        else:
            print(f"Created lode for {project_name}")
        return 0

    if subcommand == "restart":
        if (rc := require_not_inside_lode()) is not None:
            return rc
        lode_id = parsed.lode_id
        err = require_server()
        if err:
            return err
        lode = client.get_lode(socket_path, lode_id)
        if not lode:
            print(f"Lode not found: {lode_id}")
            return 1
        if lode.get("active"):
            print(f"Cannot restart: lode {lode_id} is active")
            return 1
        stage = lode.get("stage", "")
        if stage not in ("mill", "refine", "ship"):
            print(f"Cannot restart: lode {lode_id} stage is {stage}")
            return 1
        client.restart_lode(socket_path, lode_id, stage)
        print(f"Restarting {stage} for {lode_id}")
        return 0

    if subcommand == "watch":
        if (rc := require_not_inside_lode()) is not None:
            return rc
        lode_id = parsed.lode_id
        if require_server():
            return 1
        lode = client.get_lode(socket_path, lode_id)
        if not lode:
            print(f"Lode '{lode_id}' not found")
            return 1
        if not lode.get("active"):
            print(f"Lode '{lode_id}' is not active")
            return 1

        # Print initial state
        print(format_watch_line(lode))

        done = threading.Event()
        result = [0]

        def on_message(message: dict) -> None:
            msg_type = message.get("type")
            if msg_type not in ("lode_updated", "lode_archived"):
                return
            msg_lode = message.get("lode", {})
            if msg_lode.get("id") != lode_id:
                return
            print(format_watch_line(msg_lode))
            if msg_type == "lode_archived":
                done.set()
            elif msg_lode.get("state") == "error":
                result[0] = 1
                done.set()
            elif msg_lode.get("stage") == "shipped":
                done.set()

        conn = client.HopperConnection(socket_path)
        try:
            conn.start(callback=on_message)
            done.wait()
        except KeyboardInterrupt:
            pass
        finally:
            conn.stop()
        return result[0]

    if subcommand == "wait":
        if (rc := require_not_inside_lode()) is not None:
            return rc
        lode_id = parsed.lode_id
        if require_server():
            return 1

        # Try active lode first (exact match via connect)
        lode = client.get_lode(socket_path, lode_id)
        if lode:
            if lode.get("stage") == "shipped":
                print(format_lode_detail(lode))
                return 0
            if not lode.get("active"):
                print(f"Lode '{lode_id}' is not active")
                return 1
        else:
            # Try prefix match across active + archived
            lode, error = _lookup_lode(socket_path, lode_id)
            if not lode:
                print(error or f"Lode '{lode_id}' not found")
                return 1
            # Found shipped lode via prefix — already done
            if lode.get("stage") == "shipped":
                print(format_lode_detail(lode))
                return 0
            if not lode.get("active"):
                print(f"Lode '{lode['id']}' is not active")
                return 1
            # Found active lode via prefix — update lode_id for the wait
            lode_id = lode["id"]

        done = threading.Event()
        result = [0]

        def on_message(message: dict) -> None:
            msg_type = message.get("type")
            if msg_type not in ("lode_updated", "lode_archived"):
                return
            msg_lode = message.get("lode", {})
            if msg_lode.get("id") != lode_id:
                return
            if msg_type == "lode_archived":
                done.set()
            elif msg_lode.get("state") == "error":
                result[0] = 1
                done.set()
            elif msg_lode.get("stage") == "shipped":
                done.set()

        conn = client.HopperConnection(socket_path)
        try:
            conn.start(callback=on_message)
            timeout = parsed.timeout or None
            completed = done.wait(timeout=timeout)
            if not completed:
                print(f"Timed out waiting for lode '{lode_id}'")
                result[0] = 2
            elif result[0] == 1:
                print(f"Lode '{lode_id}' entered error state")
        except KeyboardInterrupt:
            pass
        finally:
            conn.stop()
        return result[0]

    if subcommand in ("status", "show"):
        err = require_server()
        if err:
            return err
        lode, error = _lookup_lode(socket_path, parsed.lode_id)
        if error:
            print(error)
            return 1
        print(format_lode_detail(lode))
        return 0

    return 0


@command("implement", "Create a lode for an implementation request")
def cmd_implement(args: list[str]) -> int:
    """Alias for hop lode create."""
    if (
        rc := _create_alias_help("implement", "Create a lode for an implementation request", args)
    ) is not None:
        return rc
    return cmd_lode(["create"] + args)


@command("submit", "Create a lode (alias for implement)", group="aliases")
def cmd_submit(args: list[str]) -> int:
    """Alias for hop lode create."""
    if (
        rc := _create_alias_help("submit", "Create a lode (alias for implement)", args)
    ) is not None:
        return rc
    return cmd_lode(["create"] + args)


@command("list", "List lodes (alias for lode list)", group="aliases")
def cmd_list(args: list[str]) -> int:
    """Alias for hop lode list."""
    if "-h" in args or "--help" in args:
        p = make_parser("list", "List lodes (alias for lode list)")
        p.add_argument("-a", "--archived", action="store_true", help="Show archived lodes")
        p.add_argument("-p", "--project", help="Filter by project name")
        try:
            parse_args(p, args)
        except SystemExit:
            return 0
    return cmd_lode(["list"] + args)


@command("projects", "List projects (alias for project list)", group="aliases")
def cmd_projects(args: list[str]) -> int:
    """Alias for hop project list."""
    if "-h" in args or "--help" in args:
        p = make_parser("projects", "List projects (alias for project list)")
        try:
            parse_args(p, args)
        except SystemExit:
            return 0
    return cmd_project(args)


@command("wait", "Wait for a lode to ship (alias for lode wait)", group="aliases")
def cmd_wait(args: list[str]) -> int:
    """Alias for hop lode wait."""
    if "-h" in args or "--help" in args:
        p = make_parser("wait", "Wait for a lode to ship (alias for lode wait)")
        p.add_argument("lode_id", help="Lode ID to wait for")
        p.add_argument("--timeout", type=float, default=0, help="Timeout in seconds (0=forever)")
        try:
            parse_args(p, args)
        except SystemExit:
            return 0
    return cmd_lode(["wait"] + args)


@command("show", "Show lode details (alias for lode show)", group="aliases")
def cmd_show(args: list[str]) -> int:
    """Alias for hop lode show."""
    if "-h" in args or "--help" in args:
        p = make_parser("show", "Show lode details (alias for lode show)")
        p.add_argument("lode_id", help="Lode ID to show")
        try:
            parse_args(p, args)
        except SystemExit:
            return 0
    return cmd_lode(["show"] + args)


@command("watch", "Watch lode status events (alias for lode watch)", group="aliases")
def cmd_watch(args: list[str]) -> int:
    """Alias for hop lode watch."""
    if "-h" in args or "--help" in args:
        p = make_parser("watch", "Watch lode status events (alias for lode watch)")
        p.add_argument("lode_id", help="Lode ID to watch")
        try:
            parse_args(p, args)
        except SystemExit:
            return 0
    return cmd_lode(["watch"] + args)


@command("restart", "Restart an inactive lode (alias for lode restart)", group="aliases")
def cmd_restart(args: list[str]) -> int:
    """Alias for hop lode restart."""
    if "-h" in args or "--help" in args:
        p = make_parser("restart", "Restart an inactive lode (alias for lode restart)")
        p.add_argument("lode_id", help="Lode ID to restart")
        try:
            parse_args(p, args)
        except SystemExit:
            return 0
    return cmd_lode(["restart"] + args)


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
    handler, *_ = COMMANDS[cmd]
    return handler(cmd_args)
