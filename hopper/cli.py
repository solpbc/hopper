# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import setproctitle

import hopper.code as hopper_code
from hopper import __version__, config
from hopper.client import set_lode_progress
from hopper.lodes import (
    STATUS_ERROR,
    STATUS_SHIPPED,
    current_time_ms,
    find_lode_by_prefix,
    find_lodes_by_prefix,
    format_age,
    get_lode_dir,
    lode_icon,
)
from hopper.tmux import capture_pane, paste_buffer, send_keys

STUCK_GRACE_MS = 120_000

logger = logging.getLogger(__name__)


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


class HopperArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that raises on errors but keeps normal --help behavior."""

    def error(self, message: str) -> None:
        raise ArgumentError(message)


def make_parser(cmd: str, description: str) -> argparse.ArgumentParser:
    """Create an argument parser for a subcommand.

    Returns a parser configured with:
    - prog set to 'hop <cmd>' for proper usage lines
    - exit_on_error=False so we can handle errors gracefully
    """
    return HopperArgumentParser(
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
    print("Usage: hop [-H host|--host host] <command> [options]")
    for group_key, group_label in HELP_GROUPS:
        cmds = [(n, d) for n, (_, d, g) in COMMANDS.items() if g == group_key]
        if cmds:
            print(f"\n{group_label}:")
            for name, desc in cmds:
                print(f"  {name:<12} {desc}")
    print()
    print("Options:")
    print("  -H, --host   Run the command on a remote hopper host (use 'local' to force local)")
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


def _remote_disabled() -> bool:
    """Return True when routing must be skipped."""
    return bool(os.environ.get("HOP_NO_ROUTE") or os.environ.get("HOPPER_LID"))


def _global_host_arg(args: list[str]) -> tuple[str | None, list[str], str | None]:
    """Parse the global -H/--host flag before command dispatch."""
    if not args:
        return None, args, None
    if args[0] in ("-H", "--host"):
        if len(args) < 2:
            return None, args, "error: -H/--host requires a host"
        return args[1], args[2:], None
    if args[0].startswith("--host="):
        return args[0].split("=", 1)[1], args[1:], None
    return None, args, None


def _locally_expanded_home_arg(cmd: str, args: list[str]) -> str | None:
    """Find a path arg whose unquoted tilde expanded against the local home."""
    if cmd != "project" or len(args) < 2 or args[0] != "add":
        return None
    home = str(Path.home())
    path_arg = args[1]
    return path_arg if path_arg == home or path_arg.startswith(f"{home}/") else None


def _stdin_for_remote(cmd: str, cmd_args: list[str]) -> str | None:
    """Read stdin only for commands that are expected to consume it."""
    if sys.stdin.isatty():
        return None
    if cmd in ("implement", "submit", "feedback"):
        return sys.stdin.read()
    if cmd == "lode" and cmd_args and cmd_args[0] == "create":
        return sys.stdin.read()
    if cmd == "gate" and cmd_args and cmd_args[0] == "feedback":
        return sys.stdin.read()
    return None


def _extract_create_project(cmd: str, cmd_args: list[str]) -> str | None:
    """Return the project argument for create-like commands."""
    args = cmd_args
    if cmd == "lode":
        if not args or args[0] != "create":
            return None
        args = args[1:]
    elif cmd not in ("implement", "submit"):
        return None

    index = 0
    while index < len(args):
        arg = args[index]
        if arg in ("-f", "--force", "--json"):
            index += 1
            continue
        if arg.startswith("-"):
            index += 1
            continue
        return arg
    return None


def _create_wants_json(cmd: str, cmd_args: list[str]) -> bool:
    args = cmd_args[1:] if cmd == "lode" and cmd_args[:1] == ["create"] else cmd_args
    return "--json" in args


def _remote_host_for_create(project: str) -> tuple[str, str] | None:
    """Resolve a create command to a remote host when local should not handle it."""
    from hopper.projects import find_project
    from hopper.remote import remote_registry

    registry = remote_registry()
    host = registry.get(project)
    if not host:
        return None
    project_record = find_project(project)
    if project_record and not project_record.disabled:
        return None
    return host, f"remote.{project}"


def _remote_process_output(
    result,
    *,
    host: str,
    annotate_create: bool = False,
    annotate_json: bool = False,
) -> None:
    """Pass through remote output, optionally adding host context."""
    stdout = result.stdout
    if annotate_json and stdout.strip():
        try:
            payload = json.loads(stdout)
            if isinstance(payload, dict):
                payload["host"] = host
                stdout = json.dumps(payload) + "\n"
        except json.JSONDecodeError:
            pass
    elif annotate_create and stdout.strip():
        lines = stdout.splitlines()
        if lines:
            match = re.match(r"^(Created lode \S+ \([^)]+\))(.*)$", lines[0])
            if match and " on " not in lines[0]:
                lines[0] = f"{match.group(1)} on {host}{match.group(2)}"
                stdout = "\n".join(lines) + ("\n" if result.stdout.endswith("\n") else "")

    if stdout:
        sys.stdout.write(stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)


def _run_remote_cli(
    host: str,
    hop_args: list[str],
    *,
    reason: str,
    stdin_text: str | None = None,
    annotate_create: bool = False,
    annotate_json: bool = False,
    remember_project: str | None = None,
) -> int:
    """Run a remote hop command and mirror its result locally."""
    from hopper.remote import remember_lode, run_remote

    print(f"→ {host} ({reason})", file=sys.stderr)
    try:
        result = run_remote(host, hop_args, stdin_text=stdin_text)
    except subprocess.TimeoutExpired as e:
        print(f"remote command timed out on {host}: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"remote command failed on {host}: {e}", file=sys.stderr)
        return 1

    _remote_process_output(
        result,
        host=host,
        annotate_create=annotate_create,
        annotate_json=annotate_json,
    )
    if result.returncode == 0 and remember_project:
        lode_id = None
        try:
            payload = json.loads(result.stdout)
            if isinstance(payload, dict):
                lode_id = payload.get("id")
        except json.JSONDecodeError:
            match = re.search(r"Created lode (\S+)", result.stdout)
            if match:
                lode_id = match.group(1)
        if isinstance(lode_id, str) and lode_id:
            remember_lode(lode_id, host, remember_project)
    return result.returncode


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
        disable_project,
        enable_project,
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
        choices=["add", "remove", "rename", "list", "disable", "enable"],
        default="list",
        help="Action to perform (default: list)",
    )
    parser.add_argument("path", nargs="?", help="Path (for add) or name (for remove/rename)")
    parser.add_argument("new_name", nargs="?", help="New name (for rename)")
    parser.add_argument("reason", nargs="*", help="Reason (for disable)")
    try:
        parsed = parse_args(parser, args)
    except SystemExit:
        return 0
    except ArgumentError as e:
        print(f"error: {e}")
        parser.print_usage()
        return 1

    if parsed.action not in ("rename", "disable") and parsed.new_name is not None:
        print(f"error: unexpected argument: {parsed.new_name}")
        parser.print_usage()
        return 1
    if parsed.action != "disable" and parsed.reason:
        print(f"error: unexpected argument: {parsed.reason[0]}")
        parser.print_usage()
        return 1

    if parsed.action == "list":
        projects = load_projects()
        if not projects:
            print("No projects configured. Use: hop project add <path>")
            return 0
        for p in projects:
            status = ""
            if p.disabled:
                status = f" (disabled: {p.disabled_reason})" if p.disabled_reason else " (disabled)"
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

    if parsed.action == "disable":
        if not parsed.path:
            print("error: name required for disable")
            parser.print_usage()
            return 1
        reason = " ".join(t for t in [parsed.new_name, *parsed.reason] if t)
        if disable_project(parsed.path, reason):
            print(f"Disabled project: {parsed.path}")
            if reason:
                print(f"  reason: {reason}")
            try:
                reload_projects(_socket())
            except Exception:
                pass
            return 0
        else:
            print(f"Project not found: {parsed.path}")
            return 1

    if parsed.action == "enable":
        if not parsed.path:
            print("error: name required for enable")
            parser.print_usage()
            return 1
        if enable_project(parsed.path):
            print(f"Enabled project: {parsed.path}")
            try:
                reload_projects(_socket())
            except Exception:
                pass
            return 0
        else:
            print(f"Project not found: {parsed.path}")
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


@command("remote", "Manage remote hopper hosts")
def cmd_remote(args: list[str]) -> int:
    """Manage project -> remote hopper host mappings."""
    from hopper.projects import find_project
    from hopper.remote import remote_registry, remove_remote, run_remote, set_remote

    parser = make_parser("remote", "Manage project -> remote hopper host mappings.")
    subs = parser.add_subparsers(dest="subcommand")
    list_p = subs.add_parser("list", aliases=["ls"], help="List remotes", exit_on_error=False)
    list_p.add_argument("--json", dest="json_output", action="store_true", help="Output JSON")
    set_p = subs.add_parser("set", help="Set a project remote", exit_on_error=False)
    set_p.add_argument("project", help="Project name")
    set_p.add_argument("host", help="Remote host")
    rm_p = subs.add_parser(
        "rm",
        aliases=["remove"],
        help="Remove a project remote",
        exit_on_error=False,
    )
    rm_p.add_argument("project", help="Project name")

    try:
        parsed = parse_args(parser, args)
    except SystemExit:
        return 0
    except ArgumentError as e:
        print(f"error: {e}")
        parser.print_usage()
        return 1

    subcommand = parsed.subcommand or "list"
    if subcommand in ("list", "ls"):
        registry = remote_registry()
        rows = [{"project": project, "host": host} for project, host in sorted(registry.items())]
        if getattr(parsed, "json_output", False):
            print(json.dumps({"remotes": rows}, indent=2))
            return 0
        if not rows:
            print("No remote projects configured.")
            return 0
        for row in rows:
            print(f"{row['project']:<24} {row['host']}")
        return 0

    if subcommand == "set":
        project = find_project(parsed.project)
        if project and not project.disabled:
            print(f"error: project '{parsed.project}' is active locally; disable it before routing")
            print(f'  hop project disable {parsed.project} --reason "moved to {parsed.host}"')
            return 1
        try:
            result = run_remote(parsed.host, ["ping"], timeout=15)
            failed = result.returncode != 0
            detail = (result.stderr or result.stdout or "remote ping failed").strip()
        except (OSError, subprocess.TimeoutExpired) as e:
            failed = True
            detail = str(e)
        if failed:
            print(
                f"warning: remote host {parsed.host} did not answer hop ping: {detail}",
                file=sys.stderr,
            )
        set_remote(parsed.project, parsed.host)
        print(f"remote.{parsed.project}={parsed.host}")
        return 0

    if subcommand in ("rm", "remove"):
        if not remove_remote(parsed.project):
            print(f"Remote project '{parsed.project}' not set.")
            return 1
        print(f"Removed remote.{parsed.project}")
        return 0

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


def _cmd_gate_show(args: list[str]) -> int:
    """Show a lode's gate.md review doc."""
    import hopper.client as client

    parser = make_parser("gate show", "Show gate review details")
    parser.add_argument("lode_id", help="Lode ID to show")
    try:
        parsed = parse_args(parser, args)
    except SystemExit:
        return 0
    except ArgumentError as e:
        print(f"error: {e}")
        parser.print_usage()
        return 1

    if err := require_server():
        remote_lode, _checked = _find_remote_lode(parsed.lode_id)
        if remote_lode:
            return _run_remote_cli(
                remote_lode["host"],
                ["gate", "show", parsed.lode_id],
                reason=f"lode {remote_lode['id']}",
            )
        return err

    gate_data = client.get_gate(_socket(), parsed.lode_id)
    if not gate_data:
        remote_lode, checked = _find_remote_lode(parsed.lode_id)
        if remote_lode:
            return _run_remote_cli(
                remote_lode["host"],
                ["gate", "show", parsed.lode_id],
                reason=f"lode {remote_lode['id']}",
            )
        suffix = f" Checked remote hosts: {checked}." if checked else ""
        print(f"Error: lode {parsed.lode_id} not found.{suffix}")
        return 1

    lode = gate_data["lode"]
    gate_text = gate_data.get("gate", "").rstrip("\n")
    print(
        f"Lode: {lode.get('id', '')}\n"
        f"Stage: {lode.get('stage', '')}\n"
        f"State: {lode.get('state', '')}\n\n"
        f"--- gate.md ---\n{gate_text}\n---\n\n"
        f'Respond with: hop gate feedback {lode.get("id", "")} "<your response>"'
    )
    return 0


def _cmd_gate_feedback(args: list[str]) -> int:
    """Send feedback to a gated lode."""
    import hopper.client as client

    description = (
        "Send feedback to a gated lode. Forms:\n"
        '  hop gate feedback <lode_id> "<response>"\n'
        "  hop gate feedback <lode_id> < file.md\n"
        "  hop gate feedback <lode_id> - < file.md"
    )
    parser = make_parser(
        "gate feedback",
        description,
    )
    parser.formatter_class = argparse.RawDescriptionHelpFormatter
    parser.add_argument("lode_id", help="Lode ID to send feedback to")
    parser.add_argument("text", nargs="?", help="Feedback text")
    try:
        parsed = parse_args(parser, args)
    except SystemExit:
        return 0
    except ArgumentError as e:
        print(f"error: {e}")
        parser.print_usage()
        return 1

    text = sys.stdin.read() if parsed.text in (None, "-") else parsed.text
    if not text.strip():
        print(
            "Error: no feedback provided. Use one of:\n"
            '  hop gate feedback <lode_id> "<response>"\n'
            "  hop gate feedback <lode_id> < file.md\n"
            "  hop gate feedback <lode_id> - < file.md",
            file=sys.stderr,
        )
        return 1

    if err := require_server():
        remote_lode, _checked = _find_remote_lode(parsed.lode_id)
        if remote_lode:
            return _run_remote_cli(
                remote_lode["host"],
                ["gate", "feedback", parsed.lode_id, "-"],
                reason=f"lode {remote_lode['id']}",
                stdin_text=text,
            )
        return err

    response = client.send_gate_feedback(_socket(), parsed.lode_id, text)
    if response and response.get("type") == "feedback_sent":
        print(f"Feedback sent to {parsed.lode_id} (pane {response.get('tmux_pane', '')})")
        if not response.get("submitted", True):
            print("Warning: feedback paste was not verified as submitted.", file=sys.stderr)
            tail = response.get("tail", "")
            if tail:
                print(tail, file=sys.stderr)
        return 0

    error = (
        response.get("error", "failed to send feedback") if response else "failed to send feedback"
    )
    remote_lode, checked = _find_remote_lode(parsed.lode_id)
    if remote_lode:
        return _run_remote_cli(
            remote_lode["host"],
            ["gate", "feedback", parsed.lode_id, "-"],
            reason=f"lode {remote_lode['id']}",
            stdin_text=text,
        )
    suffix = f" Checked remote hosts: {checked}." if checked else ""
    print(f"Error: {error}.{suffix}")
    return 1


@command("gate", "Pause lode at a review gate", group="lode")
def cmd_gate(args: list[str]) -> int:
    """Save gate review doc and pause lode for user review."""
    if args and args[0] == "show":
        return _cmd_gate_show(args[1:])
    if args and args[0] == "feedback":
        return _cmd_gate_feedback(args[1:])

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
        if parsed.project:
            items = [i for i in items if i.project == parsed.project]
            if not items:
                print(f"No backlog items for project: {parsed.project}")
                return 0
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
    host = lode.get("host")
    project = lode.get("project", "")
    title = lode.get("title", "")
    status_text = lode.get("status", "")
    if host:
        return f"  {host:<14} {icon} {stage:<7} {lid}  {project:<16} {title:<28} {status_text}"
    return f"  {icon} {stage:<7} {lid}  {project:<16} {title:<28} {status_text}"


def _format_lode_error(lode: dict) -> str:
    """Format error state output for a lode."""
    lode_id = lode.get("id", "")
    lines = [f"error: lode {lode_id} is in error state"]
    stage = lode.get("stage", "")
    if stage:
        lines.append(f"  stage: {stage}")
    status = lode.get("status", "")
    if status:
        lines.append(f"  status: {status}")
    if not lode.get("recovery"):
        lines.append("")
        lines.append(f"to retry: hop lode restart {lode_id}")
    return "\n".join(lines)


def _load_lode_recovery(lode_id: str) -> dict | None:
    """Load a local lode's recovery record without breaking status rendering."""
    recovery_path = get_lode_dir(lode_id) / "recovery.json"
    try:
        record = json.loads(recovery_path.read_text())
        if not isinstance(record, dict):
            raise ValueError("recovery record is not a JSON object")
        return record
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.warning(f"Failed to read recovery record {recovery_path}: {exc}")
        return None


def format_lode_detail(lode: dict) -> str:
    """Format a lode as a multi-line detailed view."""
    lines = [format_lode_line(lode)]
    if lode.get("state") == "error":
        lines.append("")
        lines.append(_format_lode_error(lode))
        lines.append("")
    lines.append(f"  id:       {lode.get('id', '')}")
    if lode.get("host"):
        lines.append(f"  host:     {lode.get('host', '')}")
    lines.append(f"  project:  {lode.get('project', '')}")
    lines.append(f"  stage:    {lode.get('stage', '')}")
    lines.append(f"  state:    {lode.get('state', '')}")

    status_text = lode.get("status", "")
    if status_text:
        lines.append(f"  status:   {status_text}")
    progress_text = lode.get("last_progress_summary", "")
    if progress_text:
        lines.append(f"  progress: {progress_text}")

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
    if lode.get("active") and lode.get("tmux_pane"):
        lines.append(f"  pane:     {lode['tmux_pane']}")
    recovery = lode.get("recovery")
    if recovery:
        snapshot = recovery.get("snapshot", {})
        lines.append("")
        lines.append("  recovery:")
        lines.append(f"    outcome:   {snapshot.get('outcome', '')}")
        if snapshot.get("sha"):
            lines.append(f"    sha:       {snapshot['sha']}")
        if snapshot.get("git_error"):
            lines.append(f"    git_error: {snapshot['git_error']}")
        lines.append(f"    failed_at: {recovery.get('failed_at', '')}")
        lines.append(f"    stage:     {recovery.get('stage', '')}")
        lines.append(f"    branch:    {recovery.get('branch') or 'unavailable'}")
        lines.append(f"    worktree:  {recovery.get('worktree_path') or 'unavailable'}")
        lines.append(f"    reason:    {recovery.get('reason', '')}")
    if lode.get("state") == "gated":
        lines.append("")
        lines.append(f"Gate blocked. Review with: hop gate show {lode.get('id', '')}")
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


def _remote_lode_status(host: str, lode_id: str, timeout: float = 5.0) -> tuple[dict | None, str]:
    """Return (lode, probe state), distinguishing absence from unreadability."""
    from hopper.remote import run_remote

    try:
        result = run_remote(host, ["lode", "status", lode_id, "--json"], timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None, "unreadable"
    if result.returncode != 0:
        output = f"{result.stdout}\n{result.stderr}".lower()
        return None, "absent" if result.returncode == 1 and "not found" in output else "unreadable"
    try:
        lode = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None, "unreadable"
    if not isinstance(lode, dict) or not lode.get("id"):
        return None, "unreadable"
    lode["host"] = host
    return lode, "found"


def _find_remote_lode(prefix: str) -> tuple[dict | None, str]:
    """Find a lode on configured remote hosts using cache, then fan-out."""
    from hopper.remote import load_lode_cache, remember_lode, remote_registry

    registry = remote_registry()
    hosts = sorted(set(registry.values()))
    checked: list[str] = []
    unreadable: set[str] = set()

    cache_entry = load_lode_cache().get(prefix)
    if cache_entry and isinstance(cache_entry.get("host"), str):
        host = cache_entry["host"]
        checked.append(host)
        lode, probe_state = _remote_lode_status(host, prefix)
        if probe_state == "unreadable":
            unreadable.add(host)
        if lode:
            remember_lode(lode["id"], host, lode.get("project", ""))
            return lode, ", ".join(checked)

    remaining_hosts = [host for host in hosts if host not in checked]
    if not remaining_hosts:
        summary = ", ".join(checked)
        if unreadable:
            summary += f" [unreadable: {', '.join(sorted(unreadable))}]"
        return None, summary

    lock = threading.Lock()
    found: list[dict] = []

    def check_host(host: str) -> None:
        lode, probe_state = _remote_lode_status(host, prefix)
        with lock:
            checked.append(host)
            if probe_state == "unreadable":
                unreadable.add(host)
            if lode and not found:
                found.append(lode)

    threads = [
        threading.Thread(target=check_host, args=(host,), daemon=True) for host in remaining_hosts
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5.5)
    with lock:
        for host, thread in zip(remaining_hosts, threads):
            if thread.is_alive():
                if host not in checked:
                    checked.append(host)
                unreadable.add(host)

    if found:
        lode = found[0]
        remember_lode(lode["id"], lode["host"], lode.get("project", ""))
        return lode, ", ".join(sorted(set(checked)))
    summary = ", ".join(sorted(set(checked)))
    if unreadable:
        summary += f" [unreadable: {', '.join(sorted(unreadable))}]"
    return None, summary


def _lookup_lode_with_remote(socket_path, prefix: str) -> tuple[dict | None, str | None]:
    """Look up a lode locally, then on configured remote hosts."""
    lode, error = _lookup_lode(socket_path, prefix)
    if lode or (error and not error.startswith("Lode '")):
        return lode, error
    remote_lode, checked = _find_remote_lode(prefix)
    if remote_lode:
        return remote_lode, None
    if "[unreadable:" in checked:
        return None, f"Lode status unavailable for '{prefix}'. Remote probes: {checked}."
    suffix = f" Checked remote hosts: {checked}." if checked else " No remote hosts configured."
    return None, f"Lode '{prefix}' not found.{suffix}"


def _tail_text(text: str, lines: int = 10) -> str:
    """Return the last N lines of text."""
    return "\n".join(text.splitlines()[-lines:])


def _submission_tail(text: str) -> str:
    """Return a small tail used to check whether pasted input remains pending."""
    compact = " ".join(text.strip().split())
    return compact[-80:] if compact else ""


def _pane_has_pending_text(pane_text: str | None, submitted_text: str) -> bool:
    """Heuristic for whether submitted text still appears on the input line."""
    if not pane_text:
        return False
    tail = _submission_tail(submitted_text)
    if not tail:
        return False
    compact_tail = " ".join(_tail_text(pane_text, 5).split())
    return tail in compact_tail


def _submit_to_pane(target: str, text: str, *, paste: bool = True) -> tuple[bool, str]:
    """Submit text to a tmux pane and return (submitted, post-submit tail)."""
    before = capture_pane(target, plain=True)
    if before is None:
        return False, f"pane {target} no longer exists"
    delivered = paste_buffer(target, text) if paste else send_keys(target, text)
    if not delivered:
        return False, f"failed to deliver text to {target}"
    send_keys(target, "Enter")
    time.sleep(2)
    after = capture_pane(target, plain=True)
    if not _pane_has_pending_text(after, text):
        return True, _tail_text(after or "", 10)
    send_keys(target, "Enter")
    time.sleep(0.2)
    retry_after = capture_pane(target, plain=True)
    submitted = not _pane_has_pending_text(retry_after, text)
    return submitted, _tail_text(retry_after or after or "", 10)


def _add_create_args(parser):
    """Add lode create arguments to a parser."""
    parser.add_argument("project", help="Project name")
    parser.add_argument("-f", "--force", action="store_true", help="Override dirty-repo check")
    parser.add_argument("--json", dest="json_output", action="store_true", help="Output JSON")
    parser.formatter_class = argparse.RawDescriptionHelpFormatter
    prog = parser.prog
    parser.epilog = (
        "scope is read from stdin:\n"
        f'  echo "scope text" | {prog} <project>\n'
        f"  cat scope.md | {prog} <project>\n"
        f"  {prog} <project> <<'EOF'\n"
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
    from hopper.projects import disabled_project_message, find_project

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
    list_p.add_argument("--json", dest="json_output", action="store_true", help="Output JSON")
    list_p.add_argument(
        "--all-hosts",
        action="store_true",
        help="Aggregate local and configured remote hopper hosts",
    )

    create_p = subs.add_parser("create", help="Create a new lode", exit_on_error=False)
    _add_create_args(create_p)

    restart_p = subs.add_parser("restart", help="Restart an inactive lode", exit_on_error=False)
    restart_p.add_argument("lode_id", help="Lode ID to restart")
    restart_p.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Restart even if Claude has already started for this stage",
    )
    pause_p = subs.add_parser("pause", help="Pause a lode and retain its worktree")
    pause_p.add_argument("lode_id", help="Lode ID to pause")
    resume_p = subs.add_parser("resume", help="Resume a paused or dead-pane lode")
    resume_p.add_argument("lode_id", help="Lode ID to resume")

    watch_p = subs.add_parser("watch", help="Watch lode status events", exit_on_error=False)
    watch_p.add_argument("lode_id", help="Lode ID to watch")
    wait_p = subs.add_parser("wait", help="Wait for lode to ship", exit_on_error=False)
    wait_p.add_argument("lode_id", nargs="+", help="Lode ID(s) to wait for")
    wait_p.add_argument("--timeout", type=float, default=0, help="Timeout in seconds (0=forever)")
    wait_p.add_argument("--poll", type=float, default=30, help="Remote poll interval seconds")
    wait_p.add_argument("--json", dest="json_output", action="store_true", help="Output JSONL")
    status_p = subs.add_parser("status", help="Show a lode's status", exit_on_error=False)
    status_p.add_argument("lode_id", help="Lode ID to show")
    status_p.add_argument("--json", dest="json_output", action="store_true", help="Output JSON")
    show_p = subs.add_parser("show", help="Show a lode's status", exit_on_error=False)
    show_p.add_argument("lode_id", help="Lode ID to show")
    show_p.add_argument("--json", dest="json_output", action="store_true", help="Output JSON")
    log_p = subs.add_parser("log", help="Show activity log for a lode", exit_on_error=False)
    log_p.add_argument("lode_id", help="Lode ID (or prefix)")
    log_p.add_argument("-n", "--tail", type=int, default=0, help="Show last N entries")
    log_p.add_argument("--json", dest="json_output", action="store_true", help="Output as JSON")
    kill_p = subs.add_parser("kill", help="Kill a running lode", exit_on_error=False)
    kill_p.add_argument("lode_id", help="Lode ID to kill")
    kill_p.add_argument("-f", "--force", action="store_true", help="Force kill (no confirmation)")
    peek_p = subs.add_parser("peek", help="Show plain text from a lode pane", exit_on_error=False)
    peek_p.add_argument("lode_id", help="Lode ID to inspect")
    peek_p.add_argument("-n", "--lines", type=int, default=40, help="Number of lines to show")
    nudge_p = subs.add_parser("nudge", help="Send text to a lode pane", exit_on_error=False)
    nudge_p.add_argument("lode_id", help="Lode ID to nudge")
    nudge_p.add_argument("--text", default="continue", help="Text to submit")
    answer_p = subs.add_parser("answer", help="Answer a numbered lode prompt", exit_on_error=False)
    answer_p.add_argument("lode_id", help="Lode ID to answer")
    answer_p.add_argument("choice", help="Numbered choice, 1-9")

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
        if err and not getattr(parsed, "all_hosts", False):
            return err
        archived = getattr(parsed, "archived", False)
        project_filter = getattr(parsed, "project", None)

        def local_lodes() -> list[dict]:
            if err:
                return []
            if archived:
                rows = client.list_archived_lodes(socket_path)
                rows.sort(key=lambda lode: lode.get("updated_at", 0), reverse=True)
            else:
                rows = client.list_lodes(socket_path)
                rows = [lode for lode in rows if lode.get("stage") in STAGE_ORDER]
                rows.sort(key=lambda lode: STAGE_ORDER.get(lode.get("stage", "mill"), 99))
            if project_filter:
                rows = [lode for lode in rows if lode.get("project") == project_filter]
            return rows

        lodes = local_lodes()
        if getattr(parsed, "all_hosts", False):
            from hopper.remote import remote_registry, run_remote

            for lode in lodes:
                lode["host"] = "local"
            remote_args = ["lode", "list", "--json"]
            if archived:
                remote_args.append("--archived")
            if project_filter:
                remote_args.extend(["--project", project_filter])
            for host in sorted(set(remote_registry().values())):
                try:
                    result = run_remote(host, remote_args, timeout=8)
                except (OSError, subprocess.TimeoutExpired):
                    continue
                if result.returncode != 0:
                    continue
                try:
                    payload = json.loads(result.stdout)
                except json.JSONDecodeError:
                    continue
                for lode in payload.get("lodes", []) if isinstance(payload, dict) else []:
                    if isinstance(lode, dict):
                        lode["host"] = host
                        lodes.append(lode)
        if getattr(parsed, "json_output", False):
            print(json.dumps({"lodes": lodes}, indent=2))
            return 0
        if not lodes:
            print("No archived lodes" if archived else "No active lodes")
            return 0
        for lode in lodes:
            print(format_lode_line(lode))
        return 0

    if subcommand == "create":
        if (rc := require_not_inside_lode()) is not None:
            return rc
        project_name = parsed.project
        if sys.stdin.isatty():
            print("error: scope must be provided via stdin\n")
            create_p.print_help()
            return 1
        scope = sys.stdin.read().strip()
        if not scope:
            print("error: no scope provided (stdin was empty)\n")
            create_p.print_help()
            return 1
        if len(scope) < 42:
            print(f"error: scope too short ({len(scope)} chars, minimum 42)\n")
            create_p.print_help()
            return 1
        project = find_project(project_name)
        if not project:
            from hopper.projects import get_active_projects

            names = ", ".join(p.name for p in get_active_projects())
            print(f"Project '{project_name}' not found.")
            print(f"Registered projects: {names}")
            return 1
        if project.disabled:
            print(disabled_project_message(project))
            return 1
        if not parsed.force:
            from hopper.git import dirty_status

            status = dirty_status(project.path)
            if status:
                print(f"error: project repo has uncommitted changes: {project.path}")
                print("hint: commit or stash changes first, or use --force to override.")
                print()
                for line in status.splitlines():
                    print(f"  {line}")
                return 1
        err = require_server()
        if err:
            return err
        lode = client.create_lode(socket_path, project_name, scope, spawn=True)
        if getattr(parsed, "json_output", False):
            if not lode:
                print("error: lode was not created", file=sys.stderr)
                return 1
            print(json.dumps({"id": lode["id"], "project": project_name, "host": "local"}))
            return 0
        if lode:
            print(f"Created lode {lode['id']} ({project_name})")
        else:
            print(f"Created lode for {project_name}")
        return 0

    if subcommand in ("pause", "resume"):
        if (rc := require_not_inside_lode()) is not None:
            return rc
        err = require_server()
        if err:
            remote_lode, _checked = _find_remote_lode(parsed.lode_id)
            if remote_lode:
                return _run_remote_cli(
                    remote_lode["host"],
                    ["lode", subcommand, parsed.lode_id],
                    reason=f"lode {remote_lode['id']}",
                )
            return err
        operation = client.pause_lode if subcommand == "pause" else client.resume_lode
        response = operation(socket_path, parsed.lode_id)
        expected = "lode_paused" if subcommand == "pause" else "lode_resumed"
        if not response or response.get("type") != expected:
            error = (
                response.get("error", f"failed to {subcommand} lode")
                if response
                else (f"failed to {subcommand} lode")
            )
            print(f"Cannot {subcommand}: {error}")
            return 1
        if subcommand == "pause":
            print(f"Paused lode {response['lode']['id']}; worktree and stage session retained")
        else:
            print(f"Resuming lode {response['lode']['id']} (pane {response.get('tmux_pane', '')})")
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
            remote_lode, checked = _find_remote_lode(lode_id)
            if remote_lode:
                return _run_remote_cli(
                    remote_lode["host"],
                    ["lode", "restart", lode_id, *(["--force"] if parsed.force else [])],
                    reason=f"lode {remote_lode['id']}",
                )
            suffix = f" Checked remote hosts: {checked}." if checked else ""
            print(f"Lode not found: {lode_id}.{suffix}")
            return 1
        if lode.get("active"):
            pane = lode.get("tmux_pane")
            pane_dead = not pane or capture_pane(pane, plain=True) is None
            if not (parsed.force and pane_dead):
                print(f"Cannot restart: lode {lode_id} is active")
                if pane_dead:
                    print("pane appears dead; pass --force to restart anyway")
                return 1
            print(f"Detected active lode with dead pane {pane}; restarting with --force")
        stage = lode.get("stage", "")
        if stage not in ("mill", "refine", "ship"):
            print(f"Cannot restart: lode {lode_id} stage is {stage}")
            return 1
        started = bool(lode.get("claude", {}).get(stage, {}).get("started"))
        if started and not parsed.force and lode.get("state") != "error":
            print(f"Lode {lode_id} has been started (claude[{stage}].started=True).")
            print("Restarting discards in-progress work.")
            print("Pass --force to override:")
            print(f"  hop lode restart {lode_id} --force")
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
        if lode.get("state") == "error":
            print(_format_lode_error(lode))
            return 1
        if not lode.get("active"):
            print(f"Lode '{lode_id}' is not active")
            return 1

        # Print initial state
        print(format_watch_line(lode))

        done = threading.Event()
        result = [0]
        prior_states: dict[str, str] = {lode_id: lode.get("state")}

        def on_message(message: dict) -> None:
            msg_type = message.get("type")
            if msg_type not in ("lode_updated", "lode_archived"):
                return
            msg_lode = message.get("lode", {})
            msg_lode_id = msg_lode.get("id")
            if msg_lode_id != lode_id:
                return
            print(format_watch_line(msg_lode))
            old_state = prior_states.get(msg_lode_id)
            new_state = msg_lode.get("state")
            if old_state != new_state:
                if new_state == "gated":
                    print(f"Lode {msg_lode_id} is gated. Review with: hop gate show {msg_lode_id}")
                elif old_state == "gated":
                    print(f"Lode {msg_lode_id} gate resumed.")
                prior_states[msg_lode_id] = new_state
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
        if result[0] == 1:
            err_lode = client.get_lode(socket_path, lode_id)
            if err_lode:
                print(_format_lode_error(err_lode))
            else:
                print(f"Lode '{lode_id}' entered error state")
        return result[0]

    if subcommand == "wait":
        if (rc := require_not_inside_lode()) is not None:
            return rc
        lode_ids = parsed.lode_id
        server_err = require_server()
        local_available = not server_err
        json_output = getattr(parsed, "json_output", False)
        poll_interval = max(10.0, float(getattr(parsed, "poll", 30) or 30))

        def _shipped_line(lid: str, title: str) -> str:
            if title:
                return f"{STATUS_SHIPPED} {lid} shipped ({title})"
            return f"{STATUS_SHIPPED} {lid} shipped"

        def _error_line(lid: str, status: str) -> str:
            return f"{STATUS_ERROR} {lid} error: {status}"

        def _stuck_diagnostic(lid: str, status: str, tmux_pane: str | None) -> str:
            if status:
                lines = [f"{STATUS_ERROR} {lid} stuck: {status}"]
            else:
                lines = [f"{STATUS_ERROR} {lid} stuck"]
            if not tmux_pane:
                lines.append("  pane: <unknown>")
                return "\n".join(lines)

            lines.append(f"  pane: {tmux_pane}")
            lines.append("  --- last 50 lines of pane ---")
            pane_capture = capture_pane(tmux_pane)
            if pane_capture:
                pane_lines = pane_capture.split("\n")[-50:]
                lines.extend(f"  {line}" for line in pane_lines)
            else:
                lines.append("  <pane capture failed>")
            lines.append("  --- end pane ---")
            return "\n".join(lines)

        def _event(lode: dict, outcome: str) -> dict:
            event = {
                "id": lode.get("id", ""),
                "outcome": outcome,
                "stage": lode.get("stage", ""),
                "state": lode.get("state", ""),
                "status": lode.get("status", ""),
            }
            if lode.get("host"):
                event["host"] = lode["host"]
            return event

        def _print_terminal(lode: dict, outcome: str) -> None:
            lid = lode.get("id", "")
            if json_output:
                print(json.dumps(_event(lode, outcome)))
                if outcome == "stuck":
                    print(
                        _stuck_diagnostic(lid, lode.get("status", ""), lode.get("tmux_pane")),
                        file=sys.stderr,
                    )
                return
            if outcome == "shipped":
                print(_shipped_line(lid, lode.get("title", "")))
            elif outcome == "error":
                print(_error_line(lid, lode.get("status", "")))
            elif outcome == "gated":
                print(f"Lode {lid} is gated. Review with: hop gate show {lid}")
            elif outcome == "stuck":
                print(_stuck_diagnostic(lid, lode.get("status", ""), lode.get("tmux_pane")))
            elif outcome == "timeout":
                print(f"Timed out waiting for lode(s): {lid}")

        def _terminal(lode: dict) -> tuple[str, int] | None:
            if lode.get("state") == "error":
                return "error", 1
            if lode.get("state") == "gated":
                return "gated", 2
            if lode.get("state") == "stuck":
                return "stuck", 3
            if lode.get("stage") == "shipped":
                return "shipped", 0
            return None

        resolved: dict[str, dict] = {}
        pending: set[str] = set()
        pending_remote: dict[str, str] = {}
        stuck_timers: dict[str, threading.Timer] = {}

        for raw_id in lode_ids:
            lode = client.get_lode(socket_path, raw_id) if local_available else None
            if not lode and local_available:
                lode, error = _lookup_lode(socket_path, raw_id)
                if error and not error.startswith("Lode '"):
                    print(error)
                    return 1
            if not lode:
                remote_lode, checked = _find_remote_lode(raw_id)
                if remote_lode:
                    lode = remote_lode
                else:
                    suffix = (
                        f" Checked remote hosts: {checked}."
                        if checked
                        else " No remote hosts configured."
                    )
                    print(f"Lode '{raw_id}' not found.{suffix}")
                    return 1

            if lode:
                lid = lode["id"]
                terminal = _terminal(lode)
                if terminal and terminal[0] == "error":
                    print(_format_lode_error(lode))
                    return 1
                if terminal and terminal[0] == "stuck":
                    _print_terminal(lode, "stuck")
                    return 3
                if terminal and terminal[0] == "gated":
                    _print_terminal(lode, "gated")
                    return 2
                if terminal and terminal[0] == "shipped":
                    resolved[lid] = lode
                elif not lode.get("active"):
                    print(f"Lode '{raw_id}' is not active")
                    return 1
                else:
                    resolved[lid] = lode
                    pending.add(lid)
                    if lode.get("host"):
                        pending_remote[lid] = lode["host"]

        for lid, lode in resolved.items():
            if lid not in pending:
                _print_terminal(lode, "shipped")

        if not pending:
            return 0

        done = threading.Event()
        result = [0]
        lock = threading.Lock()

        def _cancel_stuck_timer(lid: str) -> None:
            timer = stuck_timers.pop(lid, None)
            if timer is not None:
                timer.cancel()

        def _on_grace_expired(lid: str) -> None:
            if done.is_set():
                return
            try:
                current = client.get_lode(socket_path, lid)
            except Exception:
                return
            if not current or current.get("state") != "stuck":
                return
            _print_terminal(current, "stuck")
            result[0] = 3
            done.set()

        def _finish(lid: str, lode: dict, outcome: str, code: int) -> None:
            with lock:
                if lid not in pending:
                    return
                _cancel_stuck_timer(lid)
                pending.discard(lid)
                pending_remote.pop(lid, None)
                _print_terminal(lode, outcome)
                result[0] = max(result[0], code)
                if code != 0 or not pending:
                    done.set()

        def on_message(message: dict) -> None:
            msg_type = message.get("type")
            if msg_type not in ("lode_updated", "lode_archived"):
                return
            msg_lode = message.get("lode", {})
            lid = msg_lode.get("id")
            if lid not in pending:
                return
            state = msg_lode.get("state")
            if msg_type == "lode_updated" and state == "stuck":
                if lid not in stuck_timers:
                    timer = threading.Timer(STUCK_GRACE_MS / 1000.0, _on_grace_expired, args=[lid])
                    timer.daemon = True
                    timer.start()
                    stuck_timers[lid] = timer
                return
            _cancel_stuck_timer(lid)
            if msg_lode.get("state") == "error":
                _finish(lid, msg_lode, "error", 1)
            elif msg_lode.get("state") == "gated":
                _finish(lid, msg_lode, "gated", 2)
            elif msg_lode.get("stage") == "shipped" or msg_type == "lode_archived":
                _finish(lid, msg_lode, "shipped", 0)

        def _remote_poll_lode(lid: str, host: str) -> None:
            from hopper.remote import remember_lode, run_remote

            prior: tuple[str, str, str] | None = None
            stuck_since: float | None = None
            consecutive_failures = 0
            while not done.is_set() and lid in pending:
                try:
                    remote_result = run_remote(
                        host,
                        ["lode", "status", lid, "--json"],
                        timeout=max(5.0, min(poll_interval, 30.0)),
                    )
                except (OSError, subprocess.TimeoutExpired) as e:
                    consecutive_failures += 1
                    if consecutive_failures >= 2:
                        print(f"remote poll failed for {lid} on {host}: {e}", file=sys.stderr)
                    done.wait(5 if consecutive_failures == 1 else poll_interval)
                    continue

                if remote_result.returncode != 0:
                    consecutive_failures += 1
                    if consecutive_failures >= 2:
                        detail = (remote_result.stderr or remote_result.stdout).strip()
                        print(f"remote poll failed for {lid} on {host}: {detail}", file=sys.stderr)
                    done.wait(5 if consecutive_failures == 1 else poll_interval)
                    continue

                consecutive_failures = 0
                try:
                    lode = json.loads(remote_result.stdout)
                except json.JSONDecodeError:
                    done.wait(poll_interval)
                    continue
                if not isinstance(lode, dict):
                    done.wait(poll_interval)
                    continue
                lode["host"] = host
                remember_lode(lode.get("id", lid), host, lode.get("project", ""))
                signature = (
                    str(lode.get("stage", "")),
                    str(lode.get("state", "")),
                    str(lode.get("status", "")),
                )
                if prior is None:
                    prior = signature
                elif signature != prior and not json_output:
                    print(format_lode_line(lode))
                    prior = signature

                terminal = _terminal(lode)
                if terminal and terminal[0] == "stuck":
                    if stuck_since is None:
                        stuck_since = time.monotonic()
                    if (time.monotonic() - stuck_since) * 1000 >= STUCK_GRACE_MS:
                        _finish(lid, lode, "stuck", 3)
                        return
                else:
                    stuck_since = None
                if terminal and terminal[0] != "stuck":
                    _finish(lid, lode, terminal[0], terminal[1])
                    return
                done.wait(poll_interval)

        remote_threads = [
            threading.Thread(target=_remote_poll_lode, args=(lid, host), daemon=True)
            for lid, host in pending_remote.items()
        ]
        for thread in remote_threads:
            thread.start()

        conn = (
            client.HopperConnection(socket_path)
            if local_available and (pending - set(pending_remote))
            else None
        )
        try:
            if conn:
                conn.start(callback=on_message)
            timeout = parsed.timeout or None
            completed = done.wait(timeout=timeout)
            if not completed:
                remaining_lodes = [resolved[lid] for lid in sorted(pending) if lid in resolved]
                if json_output:
                    for lode in remaining_lodes:
                        print(json.dumps(_event(lode, "timeout")))
                else:
                    remaining = ", ".join(lode.get("id", "") for lode in remaining_lodes)
                    print(f"Timed out waiting for lode(s): {remaining}")
                result[0] = 4
        except KeyboardInterrupt:
            pass
        finally:
            for timer in list(stuck_timers.values()):
                timer.cancel()
            stuck_timers.clear()
            if conn:
                conn.stop()
        return result[0]

    if subcommand == "log":
        import json as json_mod

        from hopper.config import hopper_dir

        lode_id = parsed.lode_id
        if client.ping(socket_path):
            local_lode, local_error = _lookup_lode(socket_path, lode_id)
        else:
            local_lode, local_error = None, "local server unavailable"
        if not local_lode and local_error:
            remote_lode, _checked = _find_remote_lode(lode_id)
            if remote_lode:
                remote_args = ["lode", "log", lode_id]
                if parsed.tail:
                    remote_args.extend(["-n", str(parsed.tail)])
                if parsed.json_output:
                    remote_args.append("--json")
                return _run_remote_cli(
                    remote_lode["host"],
                    remote_args,
                    reason=f"lode {remote_lode['id']}",
                )
        log_file = hopper_dir() / "activity.log"
        if not log_file.exists():
            print("No activity log found.")
            return 1

        text = log_file.read_text()
        matches = []
        for line in text.splitlines():
            if f"Lode {lode_id}" in line or f"lode={lode_id}" in line:
                matches.append(line)

        if not matches:
            print(f"No log entries found for lode {lode_id}")
            return 0

        tail = getattr(parsed, "tail", 0)
        if tail > 0:
            matches = matches[-tail:]

        if getattr(parsed, "json_output", False):
            entries = []
            for line in matches:
                parts = line.split(None, 4)
                if len(parts) >= 5:
                    entries.append(
                        {
                            "timestamp": f"{parts[0]} {parts[1]}",
                            "level": parts[3],
                            "message": parts[4],
                        }
                    )
                else:
                    entries.append({"timestamp": "", "level": "", "message": line})
            print(json_mod.dumps(entries, indent=2))
        else:
            for line in matches:
                print(line)
        return 0

    if subcommand == "kill":
        err = require_server()
        if err:
            remote_lode, _checked = _find_remote_lode(parsed.lode_id)
            if remote_lode:
                return _run_remote_cli(
                    remote_lode["host"],
                    ["lode", "kill", parsed.lode_id, *(["--force"] if parsed.force else [])],
                    reason=f"lode {remote_lode['id']}",
                )
            return err
        lode_id = parsed.lode_id
        lode = client.get_lode(socket_path, lode_id)
        if not lode:
            archived = client.list_archived_lodes(socket_path)
            found = find_lode_by_prefix(archived, lode_id)
            if found:
                print(f"Lode {found['id']} is already archived.")
                return 0
            remote_lode, checked = _find_remote_lode(lode_id)
            if remote_lode:
                return _run_remote_cli(
                    remote_lode["host"],
                    ["lode", "kill", lode_id, *(["--force"] if parsed.force else [])],
                    reason=f"lode {remote_lode['id']}",
                )
            print(f"Lode not found: {lode_id}")
            if checked:
                print(f"Checked remote hosts: {checked}.")
            return 1
        if lode.get("stage") == "shipped":
            print(f"Lode {lode['id']} has already shipped.")
            return 0
        if not client.kill_lode(socket_path, lode["id"]):
            print(f"Failed to kill lode {lode['id']}")
            return 1
        print(f"Killed lode {lode['id']}; worktree and branch retained for recovery")
        return 0

    if subcommand in ("peek", "nudge", "answer"):
        err = require_server()
        if err:
            remote_lode, _checked = _find_remote_lode(parsed.lode_id)
            if remote_lode:
                remote_args = ["lode", subcommand, parsed.lode_id]
                if subcommand == "peek":
                    remote_args.extend(["-n", str(parsed.lines)])
                elif subcommand == "nudge":
                    remote_args.extend(["--text", parsed.text])
                else:
                    remote_args.append(parsed.choice)
                return _run_remote_cli(
                    remote_lode["host"],
                    remote_args,
                    reason=f"lode {remote_lode['id']}",
                )
            return err
        lode, error = _lookup_lode_with_remote(socket_path, parsed.lode_id)
        if error:
            print(error)
            return 2 if error.startswith("Lode status unavailable") else 1
        if lode.get("host"):
            remote_args = ["lode", subcommand, parsed.lode_id]
            if subcommand == "peek":
                remote_args.extend(["-n", str(parsed.lines)])
            elif subcommand == "nudge":
                remote_args.extend(["--text", parsed.text])
            else:
                remote_args.append(parsed.choice)
            return _run_remote_cli(lode["host"], remote_args, reason=f"lode {lode['id']}")

        pane = lode.get("tmux_pane")
        pane_text = capture_pane(pane, plain=True) if pane else None
        if pane_text is None:
            print(
                f"pane {pane or '<unknown>'} no longer exists "
                f"(lode active={lode.get('active')}, state={lode.get('state')})"
            )
            return 1
        if subcommand == "peek":
            lines = max(1, parsed.lines)
            print("\n".join(pane_text.splitlines()[-lines:]))
            return 0
        if subcommand == "answer" and parsed.choice not in {str(i) for i in range(1, 10)}:
            print("choice must be a digit 1..9")
            return 1
        text = parsed.text if subcommand == "nudge" else parsed.choice
        submitted, tail = _submit_to_pane(pane, text, paste=subcommand == "nudge")
        print("submitted" if submitted else "not submitted")
        if tail:
            print("--- pane tail ---")
            print(tail)
            print("--- end pane tail ---")
        return 0 if submitted else 1

    if subcommand in ("status", "show"):
        err = require_server()
        if err:
            remote_lode, checked = _find_remote_lode(parsed.lode_id)
            if not remote_lode:
                if "[unreadable:" in checked:
                    print(
                        f"Lode status unavailable for '{parsed.lode_id}'. Remote probes: {checked}."
                    )
                    return 2
                return err
            lode, error = remote_lode, None
        else:
            lode, error = _lookup_lode_with_remote(socket_path, parsed.lode_id)
        if error:
            print(error)
            return 2 if error.startswith("Lode status unavailable") else 1
        display_lode = dict(lode)
        if not lode.get("host"):
            recovery = _load_lode_recovery(lode["id"])
            if recovery is not None:
                display_lode["recovery"] = recovery
        if getattr(parsed, "json_output", False):
            print(json.dumps(display_lode, indent=2))
            return 0
        print(format_lode_detail(display_lode))
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


@command("feedback", "Send feedback to a gated lode (alias for gate feedback)", group="aliases")
def cmd_feedback(args: list[str]) -> int:
    """Alias for hop gate feedback."""
    if "-h" in args or "--help" in args:
        description = (
            "Send feedback to a gated lode. Forms:\n"
            '  hop gate feedback <lode_id> "<response>"\n'
            "  hop gate feedback <lode_id> < file.md\n"
            "  hop gate feedback <lode_id> - < file.md"
        )
        p = make_parser("feedback", description)
        p.formatter_class = argparse.RawDescriptionHelpFormatter
        p.add_argument("lode_id", help="Lode ID to send feedback to")
        p.add_argument("text", nargs="?", help="Feedback text")
        try:
            parse_args(p, args)
        except SystemExit:
            return 0
    return _cmd_gate_feedback(args)


@command("list", "List lodes (alias for lode list)", group="aliases")
def cmd_list(args: list[str]) -> int:
    """Alias for hop lode list."""
    if "-h" in args or "--help" in args:
        p = make_parser("list", "List lodes (alias for lode list)")
        p.add_argument("-a", "--archived", action="store_true", help="Show archived lodes")
        p.add_argument("-p", "--project", help="Filter by project name")
        p.add_argument("--json", dest="json_output", action="store_true", help="Output JSON")
        p.add_argument("--all-hosts", action="store_true", help="Aggregate remote hosts")
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
        p.add_argument("lode_id", nargs="+", help="Lode ID(s) to wait for")
        p.add_argument("--timeout", type=float, default=0, help="Timeout in seconds (0=forever)")
        p.add_argument("--poll", type=float, default=30, help="Remote poll interval seconds")
        p.add_argument("--json", dest="json_output", action="store_true", help="Output JSONL")
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
        p.add_argument("--json", dest="json_output", action="store_true", help="Output JSON")
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
        p.add_argument(
            "-f",
            "--force",
            action="store_true",
            help="Restart even if Claude has already started for this stage",
        )
        try:
            parse_args(p, args)
        except SystemExit:
            return 0
    return cmd_lode(["restart"] + args)


@command("log", "Show lode activity log (alias for lode log)", group="aliases")
def cmd_log(args: list[str]) -> int:
    """Alias for hop lode log."""
    if "-h" in args or "--help" in args:
        p = make_parser("log", "Show lode activity log (alias for lode log)")
        p.add_argument("lode_id", help="Lode ID (or prefix)")
        p.add_argument("-n", "--tail", type=int, default=0, help="Show last N entries")
        p.add_argument("--json", dest="json_output", action="store_true", help="Output as JSON")
        try:
            parse_args(p, args)
        except SystemExit:
            return 0
    return cmd_lode(["log"] + args)


@command("kill", "Kill a running lode (alias for lode kill)", group="aliases")
def cmd_kill(args: list[str]) -> int:
    """Alias for hop lode kill."""
    if "-h" in args or "--help" in args:
        p = make_parser("kill", "Kill a running lode (alias for lode kill)")
        p.add_argument("lode_id", help="Lode ID to kill")
        p.add_argument("-f", "--force", action="store_true", help="Force kill (no confirmation)")
        try:
            parse_args(p, args)
        except SystemExit:
            return 0
    return cmd_lode(["kill"] + args)


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


# Default number of trailing output lines `hop check` prints.
CHECK_TAIL_LINES = 50


@command("check", "Run a validation command, truncating output but keeping its exit status")
def cmd_check(args: list[str]) -> int:
    """Run a command, print only its output tail, and exit with its real status.

    Replaces the false-green `make ci 2>&1 | tail -30` pattern used to keep a
    long CI log out of an agent's context: a pipe reports the pager's exit code,
    not the command's, so a failing build can be truncated into an apparent
    success. `hop check` captures combined stdout+stderr, prints the last -n
    lines plus an explicit `exited N` summary, and returns the command's own
    exit code — so a red command can never be reported as green.
    """
    parser = make_parser(
        "check",
        "Run a validation command, print only the tail of its output, and exit "
        "with the command's real status (never the pager's). "
        "Usage: hop check [-n LINES] -- <command> [args...]",
    )
    parser.add_argument(
        "-n",
        "--lines",
        type=int,
        default=CHECK_TAIL_LINES,
        help=f"Trailing output lines to print (default: {CHECK_TAIL_LINES})",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Command to run, e.g. -- make ci",
    )
    try:
        parsed = parse_args(parser, args)
    except SystemExit:
        return 0
    except ArgumentError as e:
        print(f"error: {e}")
        parser.print_usage()
        return 1

    command = parsed.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("error: no command given. Usage: hop check [-n LINES] -- <command> [args...]")
        parser.print_usage()
        return 1
    if parsed.lines < 0:
        print("error: --lines must be non-negative")
        return 1

    heartbeat = None
    lode_id = get_hopper_lid()
    if lode_id:
        try:
            started_at = current_time_ms()
            command_text = " ".join(command)
            heartbeat = hopper_code.ProgressHeartbeat(
                lambda summary: set_lode_progress(_socket(), lode_id, summary),
                lambda now_ms: (
                    f"{hopper_code.truncate_progress_command(command_text)} — running "
                    f"{hopper_code.format_progress_duration(now_ms - started_at)}"
                ),
                interval=hopper_code.HEARTBEAT_INTERVAL_SEC,
            )
        except Exception:
            logger.debug("failed to create check heartbeat", exc_info=True)

    try:
        if heartbeat:
            try:
                heartbeat.start()
            except Exception:
                logger.debug("failed to start check heartbeat", exc_info=True)
        try:
            proc = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except FileNotFoundError:
            print(f"hop check: command not found: {command[0]}", file=sys.stderr)
            return 127
    finally:
        if heartbeat:
            try:
                heartbeat.stop()
            except Exception:
                logger.debug("failed to stop check heartbeat", exc_info=True)

    output = proc.stdout or ""
    total = len(output.splitlines())
    tail = _tail_text(output, parsed.lines) if parsed.lines else ""
    if tail:
        print(tail)

    shown = min(parsed.lines, total)
    truncated = f", showing last {shown} of {total} lines" if total > shown else ""
    print(
        f"hop check: `{' '.join(command)}` exited {proc.returncode}{truncated}",
        file=sys.stderr,
    )
    return proc.returncode


def main() -> int:
    """Main entry point with command dispatch."""
    args = sys.argv[1:]
    explicit_host, args, host_error = _global_host_arg(args)
    if host_error:
        print(host_error)
        return 1

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

    if explicit_host and explicit_host != "local" and not _remote_disabled():
        expanded_arg = _locally_expanded_home_arg(cmd, cmd_args)
        if expanded_arg:
            print(
                f"error: remote argument {expanded_arg!r} points into the local home; "
                "quote the tilde (for example, '~/src') so hop expands it on the remote host",
                file=sys.stderr,
            )
            return 2
        stdin_text = _stdin_for_remote(cmd, cmd_args)
        return _run_remote_cli(
            explicit_host,
            [cmd, *cmd_args],
            reason=f"-H {explicit_host}",
            stdin_text=stdin_text,
            annotate_create=_extract_create_project(cmd, cmd_args) is not None,
            annotate_json=_create_wants_json(cmd, cmd_args),
            remember_project=_extract_create_project(cmd, cmd_args),
        )

    if not explicit_host and not _remote_disabled():
        project = _extract_create_project(cmd, cmd_args)
        if project:
            remote_target = _remote_host_for_create(project)
            if remote_target:
                host, reason = remote_target
                stdin_text = _stdin_for_remote(cmd, cmd_args)
                return _run_remote_cli(
                    host,
                    [cmd, *cmd_args],
                    reason=reason,
                    stdin_text=stdin_text,
                    annotate_create=True,
                    annotate_json=_create_wants_json(cmd, cmd_args),
                    remember_project=project,
                )

    # Dispatch to command handler
    handler, *_ = COMMANDS[cmd]
    return handler(cmd_args)
