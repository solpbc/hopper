# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

import setproctitle

import hopper.code as hopper_code
from hopper import __version__, config
from hopper.cleanup import reap_swiftpm_testing_helpers
from hopper.client import set_lode_progress
from hopper.lodes import (
    current_time_ms,
    find_lode_by_prefix,
    format_age,
    get_lode_dir,
    get_worktree_dir,
    is_terminal_failure_kind,
    lode_icon,
    lode_status_for_display,
    lode_with_status_annotations,
)
from hopper.runner import _sum_process_tree_cpu_ms
from hopper.tmux import capture_pane

logger = logging.getLogger(__name__)

_GATE_FEEDBACK_DESCRIPTION = (
    "Send feedback to a gated lode. Exit 0 means Claude accepted a new user turn. "
    "Any reported failure leaves the lode gated and Hopper prints a safe next action.\n\n"
    "Forms:\n"
    '  hop gate feedback <lode_id> "<response>"\n'
    "  hop gate feedback <lode_id> < file.md\n"
    "  hop gate feedback <lode_id> - < file.md"
)
HELP_SKILL_REMINDER = "Note for AI agent sessions: load the `hop` skill before using this CLI."
WATCH_RECONCILE_SECONDS = 30.0
WATCH_OBSERVER_TIMEOUT_SECONDS = 300.0
LOAD_WARNING_PER_CPU = 1.0
_watch_monotonic = time.monotonic


def _watch_condition_wait(condition: threading.Condition, timeout_s: float) -> None:
    """Wait for a watch event; monkeypatched by deterministic tests."""
    condition.wait(timeout=timeout_s)


def _socket() -> Path:
    """Return the server socket path (late-binding, safe for tests)."""
    return config.server_socket_path()


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

    def _print_message(self, message, file=None) -> None:
        if not message:
            return
        text = message.rstrip("\n")
        if HELP_SKILL_REMINDER not in text.splitlines():
            text = f"{text}\n\n{HELP_SKILL_REMINDER}"
        super()._print_message(f"{text}\n", file)


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
    print()
    print(HELP_SKILL_REMINDER)


def _print_unresponsive_server(socket_path: Path, timeout: float) -> None:
    """Print the prescriptive failure for a listening but unresponsive server."""
    print(
        f"a hopper server is listening on {socket_path} but did not answer within "
        f"{timeout:g}s — it may be busy; retry, or stop it if wedged"
    )


def require_server(timeout: float = 2.0) -> int | None:
    """Check that the server is running. Returns exit code on failure, None on success."""
    from hopper.client import probe_server

    status = probe_server(_socket(), timeout=timeout)
    if status == "down":
        print("Server not running. Start it with: hop up")
        return 1
    if status == "unresponsive":
        _print_unresponsive_server(_socket(), timeout)
        return 1
    return None


def require_no_server(timeout: float = 2.0) -> int | None:
    """Check that the server is NOT running. Returns exit code on failure, None on success."""
    from hopper.client import probe_server

    socket_path = _socket()
    status = probe_server(socket_path, timeout=timeout)
    if status == "up":
        print(
            f"a hopper server is already running on {socket_path}; attach to the existing "
            "hopper session or stop that server before running hop up"
        )
        return 1
    if status == "unresponsive":
        _print_unresponsive_server(socket_path, timeout)
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


def _warn_target_load(socket_path: Path) -> None:
    """Best-effort warning about target-host load without gating submission."""
    try:
        one, five, fifteen = os.getloadavg()
        logical_cpus = os.cpu_count()
        if not logical_cpus or one / logical_cpus < LOAD_WARNING_PER_CPU:
            return
        from hopper.client import list_lodes

        registered_runners = sum(lode.get("active") is True for lode in list_lodes(socket_path))
        print(
            f"warning: target load 1m={one:.2f} 5m={five:.2f} 15m={fifteen:.2f} "
            f"across {logical_cpus} logical CPUs; lodes with a registered runner="
            f"{registered_runners}; creating anyway",
            file=sys.stderr,
        )
    except Exception:
        return


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
    from hopper.remote import run_remote

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
            _remember_lode_route(lode_id, host, remember_project)
    return result.returncode


def _remember_lode_route(lode_id: str, host: str, project: str = "") -> None:
    """Best-effort cache a remote lode route without failing its command."""
    from hopper.remote import remember_lode

    try:
        remember_lode(lode_id, host, project)
    except Exception as error:
        logger.warning(
            "Could not update remote lode cache for %s on %s: %s",
            lode_id,
            host,
            error,
        )


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
    from hopper.process import run_process_supervisor

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

    return run_process_supervisor(parsed.lode_id, _socket())


@command("process-worker", "Run one lode worker", group="internal")
def cmd_process_worker(args: list[str]) -> int:
    """Run the inner lode worker inside its prepared execution boundary."""
    from hopper.process import run_process

    parser = make_parser("process-worker", "Run one lode worker (internal command).")
    parser.add_argument("lode_id", help="Lode ID to run")
    try:
        parsed = parse_args(parser, args)
    except SystemExit:
        return 0
    except ArgumentError as error:
        print(f"error: {error}")
        parser.print_usage()
        return 1

    if err := require_server():
        return err
    return run_process(parsed.lode_id, _socket(), expect_scope=True)


@command("status", "Show or update lode status", group="lode")
def cmd_status(args: list[str]) -> int:
    """Show a local or remote lode, or update the current lode's status and title."""
    from hopper.client import get_lode, set_lode_status, set_lode_title

    parser = make_parser(
        "status",
        "Show or update lode status. "
        "Without arguments, displays the current status and title. "
        "With arguments, sets the status text. Use -t to set the title.",
    )
    parser.add_argument("text", nargs="*", help="New status text (optional)")
    parser.add_argument("-t", "--title", default=None, help="Set a short title for this lode")
    parser.add_argument("--json", dest="json_output", action="store_true", help="Output JSON")
    try:
        parsed = parse_args(parser, args)
    except SystemExit:
        return 0
    except ArgumentError as e:
        print(f"error: {e}")
        parser.print_usage()
        return 1

    lode_id = get_hopper_lid()
    if lode_id and parsed.json_output:
        if parsed.text or parsed.title is not None:
            print("error: --json cannot be combined with status text or --title")
            parser.print_usage()
            return 1
        return _show_lode_status(_socket(), lode_id, json_output=True)

    if not lode_id:
        if parsed.title is not None:
            print("Cannot set title from outside a lode.")
            return 1
        if not parsed.text:
            print("HOPPER_LID not set. Run this from within a hopper lode.")
            return 1
        if len(parsed.text) > 1:
            print("Too many arguments. Usage: hop status <lode-id>")
            return 1
        return _show_lode_status(
            _socket(),
            parsed.text[0],
            json_output=parsed.json_output,
        )

    if err := require_server():
        return err

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
    from hopper.client import get_lode, set_lode_state_acknowledged
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
        print(f"Lode {lode_id} not found or archived.")
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
    print(f"Saved to {output_path}")

    # Signal completion
    status = f"{stage.capitalize()} complete"
    acknowledgement = set_lode_state_acknowledged(_socket(), lode_id, "completed", status)
    if acknowledgement is None or (
        acknowledgement.get("accepted") is not True and acknowledgement.get("accepted") is not False
    ):
        print(
            "warning: completion disposition is UNKNOWN because the server did not "
            f"acknowledge it. Check with `hop lode status {lode_id}`.",
            file=sys.stderr,
        )
        return 0
    if acknowledgement["accepted"] is False:
        reason = acknowledgement.get("reason")
        refusal_messages = {
            "lode_not_found": f"Lode {lode_id} not found or archived.",
            "missing_run_generation": (
                "Completion was refused because this command has no runner generation. "
                "Run it inside the current lode runner, then retry."
            ),
            "stale_run_generation": (
                "Completion was refused because this runner generation is stale. "
                f"Check `hop lode status {lode_id}` and use the current runner."
            ),
            "terminal_failure": (
                "Completion was refused because this lode has a terminal failure. "
                f"Check `hop lode status {lode_id}` before recovering it."
            ),
        }
        print(
            refusal_messages.get(
                reason,
                "Completion was refused by the server. "
                f"Check `hop lode status {lode_id}` for its current state.",
            ),
            file=sys.stderr,
        )
        return 1

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

    parser = make_parser(
        "gate feedback",
        _GATE_FEEDBACK_DESCRIPTION,
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
            "Error: no feedback provided. No pane was touched. Use one of:\n"
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
        return 0

    if response is None:
        error = (
            "The feedback request returned no response. The delivery outcome is unknown and "
            "the lode may still be gated. Inspect with `hop lode peek "
            f"{parsed.lode_id}` before deciding whether to retry; do not resend the feedback "
            "blindly."
        )
    else:
        error = response.get("error", "Feedback failed without a recovery message.")
        if response.get("outcome") == "unknown_lode":
            remote_lode, checked = _find_remote_lode(parsed.lode_id)
            if remote_lode:
                return _run_remote_cli(
                    remote_lode["host"],
                    ["gate", "feedback", parsed.lode_id, "-"],
                    reason=f"lode {remote_lode['id']}",
                    stdin_text=text,
                )
            if checked:
                error = f"{error} Checked remote hosts: {checked}."
    print(error, file=sys.stderr)
    if response and response.get("outcome") == "unverified" and response.get("tail"):
        print("--- pane tail ---", file=sys.stderr)
        print(response["tail"], file=sys.stderr)
        print("--- end pane tail ---", file=sys.stderr)
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

    # Validate lode is in a stage that supports review gates
    lode = get_lode(_socket(), lode_id)
    if not lode:
        print(f"Lode {lode_id} not found.")
        return 1

    stage = lode.get("stage", "")
    if stage not in ("refine", "ship"):
        print(f"Lode {lode_id} is not in refine or ship stage.")
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
        probe_server,
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
        server_status = probe_server(_socket())
        if server_status == "up":
            add_backlog(_socket(), project, description, lode_id=lode_id)
        elif server_status == "down":
            items = load_backlog()
            add_backlog_item(items, project, description, lode_id=lode_id)
        else:
            _print_unresponsive_server(_socket(), 2.0)
            return 1

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
        server_status = probe_server(_socket())
        if server_status == "up":
            remove_backlog(_socket(), item.id)
        elif server_status == "down":
            remove_backlog_item(items, item.id)
        else:
            _print_unresponsive_server(_socket(), 2.0)
            return 1

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
    status_text = lode_status_for_display(lode)
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
    if not lode.get("recovery") and not is_terminal_failure_kind(lode.get("failure_kind")):
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

    status_text = lode_status_for_display(lode)
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


def _show_lode_status(socket_path: Path, lode_ref: str, *, json_output: bool) -> int:
    """Resolve and render one local or remote lode through the shared status path."""
    result = _resolve_lode_all_sources(socket_path, lode_ref)
    if result["outcome"] != "found":
        print(result["error"])
        return result["exit_code"]

    display_lode = dict(result["lode"])
    if result["host"] == "local":
        recovery = _load_lode_recovery(result["canonical_id"])
        if recovery is not None:
            display_lode["recovery"] = recovery
    if json_output:
        display_lode = lode_with_status_annotations(display_lode)
        print(json.dumps(display_lode, indent=2))
    else:
        print(format_lode_detail(display_lode))
    return 0


def _lookup_lode(socket_path, prefix: str) -> tuple[dict | None, str | None]:
    """Look up a lode by ID prefix across active and archived lodes."""
    import hopper.client as client

    result, payload = client.read_lode_snapshot(socket_path, prefix)
    if result == "found":
        return payload, None
    if result == "ambiguous":
        return None, f"Ambiguous prefix '{prefix}', matches: {', '.join(payload)}"
    if result == "absent":
        return None, f"Lode '{prefix}' not found."
    return None, f"Lode status unavailable for '{prefix}': {payload}"


class _RemoteLodeProbeState(str):
    """Remote probe outcome with ambiguity IDs when the host supplied them."""

    matches: tuple[str, ...]

    def __new__(cls, value: str, *, matches: tuple[str, ...] = ()):
        state = super().__new__(cls, value)
        state.matches = matches
        return state


def _remote_ambiguity_matches(output: str) -> tuple[str, ...]:
    """Extract IDs from current and legacy remote ambiguity diagnostics."""
    patterns = (
        r"Ambiguous lode prefix .+?\. Matches: (.+?)(?:\. Probes:|\n|$)",
        r"Ambiguous prefix .+?, matches: (.+?)(?:\n|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, output, flags=re.IGNORECASE)
        if match is None:
            continue
        ids: list[str] = []
        for item in match.group(1).split(","):
            lode_id = item.strip().rsplit(":", 1)[-1].strip().rstrip(".")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", lode_id):
                return ()
            ids.append(lode_id)
        if len(ids) >= 2 and len(set(ids)) == len(ids):
            return tuple(ids)
    return ()


def _remote_lode_status(
    host: str,
    lode_id: str,
    timeout: float = 5.0,
) -> tuple[dict | None, _RemoteLodeProbeState]:
    """Return (lode, probe state), distinguishing absence from unreadability."""
    from hopper.remote import run_remote

    try:
        result = run_remote(host, ["lode", "status", lode_id, "--json"], timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None, _RemoteLodeProbeState("unreadable")
    if result.returncode != 0:
        output = f"{result.stdout}\n{result.stderr}"
        ambiguity_matches = _remote_ambiguity_matches(output)
        if result.returncode == 1 and ambiguity_matches:
            return None, _RemoteLodeProbeState(
                "ambiguous",
                matches=ambiguity_matches,
            )
        state = (
            "absent" if result.returncode == 1 and "not found" in output.lower() else "unreadable"
        )
        return None, _RemoteLodeProbeState(state)
    try:
        lode = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None, _RemoteLodeProbeState("unreadable")
    if not isinstance(lode, dict) or not lode.get("id"):
        return None, _RemoteLodeProbeState("unreadable")
    lode["host"] = host
    return lode, _RemoteLodeProbeState("found")


def _remote_hosts() -> list[str]:
    """Return configured discovery hosts unless remote routing is disabled."""
    if _remote_disabled():
        return []
    from hopper.remote import remote_registry

    return sorted(set(remote_registry().values()))


def _cached_lode_host(key: str, hosts: list[str]) -> str | None:
    """Return a cached route only while its host remains configured."""
    from hopper.remote import load_lode_cache

    entry = load_lode_cache().get(key)
    host = entry.get("host") if isinstance(entry, dict) else None
    return host if isinstance(host, str) and host in hosts else None


class _RemoteProbeSummary(str):
    """Human-readable first-match probe evidence with a structured availability flag."""

    unavailable: bool

    def __new__(cls, value: str, *, unavailable: bool):
        summary = super().__new__(cls, value)
        summary.unavailable = unavailable
        return summary


def _find_remote_lode(
    prefix: str,
    *,
    remember_result: bool = True,
) -> tuple[dict | None, _RemoteProbeSummary]:
    """Use cached-first, first-match lookup for pane and lifecycle commands.

    Authoritative all-source callers must use _resolve_lode_all_sources.
    """
    hosts = _remote_hosts()
    if not hosts:
        return None, _RemoteProbeSummary("", unavailable=False)
    checked: list[str] = []
    unreadable: set[str] = set()

    cached_host = _cached_lode_host(prefix, hosts)
    if cached_host:
        host = cached_host
        checked.append(host)
        lode, probe_state = _remote_lode_status(host, prefix)
        if probe_state in ("ambiguous", "unreadable"):
            unreadable.add(host)
        if lode:
            if remember_result:
                _remember_lode_route(lode["id"], host, lode.get("project", ""))
            return lode, _RemoteProbeSummary(
                ", ".join(checked),
                unavailable=bool(unreadable),
            )

    remaining_hosts = [host for host in hosts if host not in checked]
    if not remaining_hosts:
        summary = ", ".join(checked)
        if unreadable:
            summary += f" [unreadable: {', '.join(sorted(unreadable))}]"
        return None, _RemoteProbeSummary(summary, unavailable=bool(unreadable))

    lock = threading.Lock()
    found: list[dict] = []

    def check_host(host: str) -> None:
        lode, probe_state = _remote_lode_status(host, prefix)
        with lock:
            checked.append(host)
            if probe_state in ("ambiguous", "unreadable"):
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
        if remember_result:
            _remember_lode_route(lode["id"], lode["host"], lode.get("project", ""))
        return lode, _RemoteProbeSummary(
            ", ".join(sorted(set(checked))),
            unavailable=bool(unreadable),
        )
    summary = ", ".join(sorted(set(checked)))
    if unreadable:
        summary += f" [unreadable: {', '.join(sorted(unreadable))}]"
    return None, _RemoteProbeSummary(summary, unavailable=bool(unreadable))


def _resolution_result(
    outcome: str,
    *,
    lode: dict | None = None,
    host: str | None = None,
    canonical_id: str | None = None,
    error: str | None = None,
    probe_summary: str = "",
) -> dict:
    """Build one stable lode-resolution result."""
    return {
        "outcome": outcome,
        "lode": lode,
        "host": host,
        "canonical_id": canonical_id,
        "error": error,
        "probe_summary": probe_summary,
        "exit_code": 0 if outcome == "found" else 2 if outcome == "unavailable" else 1,
    }


def _probe_summary_entry(source: str, outcome: str, detail: object = None) -> str:
    """Format one deterministic, single-line probe summary entry."""
    suffix = ""
    if detail:
        if isinstance(detail, (list, tuple)):
            text = ", ".join(str(item) for item in detail)
        else:
            text = " ".join(str(detail).split())
        suffix = f" ({text})"
    return f"{source}={outcome}{suffix}"


def _found_resolution(lode: dict, host: str, probes: list[str]) -> dict:
    """Return a copied, host-stamped successful resolution."""
    resolved = dict(lode)
    resolved["host"] = host
    return _resolution_result(
        "found",
        lode=resolved,
        host=host,
        canonical_id=resolved["id"],
        probe_summary="; ".join(probes),
    )


def _failed_resolution(
    outcome: str,
    prefix: str,
    probes: list[str],
    matches: list[tuple[str, str]] | None = None,
) -> dict:
    """Return a failed resolution with one truthful, actionable message."""
    summary = "; ".join(probes)
    if outcome == "ambiguous":
        match_text = ", ".join(f"{host}:{lode_id}" for host, lode_id in matches or [])
        error = f"Ambiguous lode prefix '{prefix}'. Matches: {match_text}. Probes: {summary}."
    elif outcome == "unavailable":
        error = f"Lode status unavailable for '{prefix}'. Probes: {summary}."
    else:
        error = f"Lode '{prefix}' not found. Probes: {summary}."
    return _resolution_result(
        outcome,
        error=error,
        probe_summary=summary,
    )


def _resolve_lode_all_sources(socket_path: Path, prefix: str) -> dict:
    """Resolve a lode authoritatively across local and configured remote sources."""
    import hopper.client as client

    local_outcome, local_payload = client.read_lode_snapshot(socket_path, prefix)
    probes = [
        _probe_summary_entry(
            "local",
            local_outcome,
            local_payload
            if local_outcome
            in {
                "ambiguous",
                "unavailable",
            }
            else None,
        )
    ]
    matches: list[tuple[str, dict]] = []
    unavailable = local_outcome == "unavailable"

    if local_outcome == "found":
        if local_payload["id"] == prefix:
            return _found_resolution(local_payload, "local", probes)
        matches.append(("local", local_payload))
    elif local_outcome == "ambiguous":
        local_matches = [("local", lode_id) for lode_id in local_payload]
        return _failed_resolution("ambiguous", prefix, probes, local_matches)

    hosts = _remote_hosts()
    if not hosts:
        if unavailable:
            return _failed_resolution("unavailable", prefix, probes)
        if matches:
            return _found_resolution(matches[0][1], matches[0][0], probes)
        return _failed_resolution("absent", prefix, probes)

    cached_host = _cached_lode_host(prefix, hosts)
    remaining_hosts = list(hosts)
    if cached_host:
        remaining_hosts.remove(cached_host)
        lode, state = _remote_lode_status(cached_host, prefix)
        ambiguity_matches = getattr(state, "matches", ())
        probes.append(_probe_summary_entry(cached_host, state, ambiguity_matches))
        if lode and lode["id"] == prefix:
            _remember_lode_route(lode["id"], cached_host, lode.get("project", ""))
            return _found_resolution(lode, cached_host, probes)
        if lode:
            matches.append((cached_host, lode))
        elif state == "ambiguous":
            found_ids = [
                *[(host, match["id"]) for host, match in matches],
                *[(cached_host, lode_id) for lode_id in ambiguity_matches],
            ]
            return _failed_resolution("ambiguous", prefix, probes, found_ids)
        elif state == "unreadable":
            unavailable = True

    lock = threading.Lock()
    remote_results: dict[str, tuple[dict | None, str]] = {}

    def probe(host: str) -> None:
        result = _remote_lode_status(host, prefix)
        with lock:
            remote_results[host] = result

    threads = [
        threading.Thread(target=probe, args=(host,), daemon=True) for host in remaining_hosts
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5.5)

    exact_matches: list[tuple[str, dict]] = []
    for host, thread in zip(remaining_hosts, threads):
        lode, state = remote_results.get(host, (None, "unreadable"))
        ambiguity_matches = getattr(state, "matches", ())
        probes.append(_probe_summary_entry(host, state, ambiguity_matches))
        if lode and lode["id"] == prefix:
            exact_matches.append((host, lode))
        elif lode:
            matches.append((host, lode))
        elif state == "ambiguous":
            matches.extend((host, {"id": lode_id}) for lode_id in ambiguity_matches)
        elif state == "unreadable" or thread.is_alive():
            unavailable = True

    if exact_matches:
        host, lode = exact_matches[0]
        _remember_lode_route(lode["id"], host, lode.get("project", ""))
        return _found_resolution(lode, host, probes)

    if len(matches) >= 2:
        found_ids = [(host, lode["id"]) for host, lode in matches]
        return _failed_resolution("ambiguous", prefix, probes, found_ids)
    if unavailable:
        return _failed_resolution("unavailable", prefix, probes)
    if matches:
        host, lode = matches[0]
        if host != "local":
            _remember_lode_route(lode["id"], host, lode.get("project", ""))
        return _found_resolution(lode, host, probes)
    return _failed_resolution("absent", prefix, probes)


def _lookup_lode_with_remote(socket_path, prefix: str) -> dict:
    """Use local-then-first-remote lookup for pane commands.

    Authoritative all-source callers must use _resolve_lode_all_sources.
    """
    import hopper.client as client

    local_outcome, local_payload = client.read_lode_snapshot(socket_path, prefix)
    if local_outcome == "found":
        return _resolution_result(
            "found",
            lode=local_payload,
            canonical_id=local_payload["id"],
        )
    if local_outcome == "ambiguous":
        return _resolution_result(
            "ambiguous",
            error=f"Ambiguous prefix '{prefix}', matches: {', '.join(local_payload)}",
        )

    remote_lode, checked = _find_remote_lode(prefix)
    if remote_lode:
        return _resolution_result(
            "found",
            lode=remote_lode,
            host=remote_lode.get("host"),
            canonical_id=remote_lode["id"],
        )
    if local_outcome == "unavailable":
        local_error = f"Lode status unavailable for '{prefix}': {local_payload}"
        if checked.unavailable:
            message = f"{local_error}. Remote probes: {checked}."
        elif checked:
            message = f"{local_error}. Checked remote hosts: {checked}."
        else:
            message = f"{local_error}. No remote hosts configured."
        return _resolution_result("unavailable", error=message)
    if checked.unavailable:
        return _resolution_result(
            "unavailable",
            error=f"Lode status unavailable for '{prefix}'. Remote probes: {checked}.",
        )
    suffix = f" Checked remote hosts: {checked}." if checked else " No remote hosts configured."
    return _resolution_result(
        "absent",
        error=f"Lode '{prefix}' not found.{suffix}",
    )


def _format_watch_line(lode: dict) -> str:
    """Format one watch transition."""
    icon = lode_icon(lode)
    return f"{icon} {lode.get('id', '')} {lode.get('stage', '')}  {lode.get('status', '')}"


def _read_watch_snapshot(socket_path: Path, lode_id: str) -> tuple[str, dict | None, str]:
    """Read one exact durable watch snapshot with probe evidence."""
    import hopper.client as client

    outcome, payload = client.read_lode_snapshot(socket_path, lode_id)
    if outcome == "found" and isinstance(payload, dict) and payload.get("id") == lode_id:
        return "found", dict(payload), _probe_summary_entry("local", "found", lode_id)
    if outcome == "absent":
        return "absent", None, _probe_summary_entry("local", "absent")
    if outcome == "unavailable":
        return "unavailable", None, _probe_summary_entry("local", "unavailable", payload)
    return (
        "unavailable",
        None,
        _probe_summary_entry("local", "unavailable", f"invalid exact snapshot: {outcome}"),
    )


def _watch_terminal_code(lode: dict) -> int | None:
    """Print terminal guidance and return its watch exit code."""
    lode_id = lode["id"]
    if lode.get("stage") == "shipped":
        return 0
    if lode.get("state") == "error":
        print(_format_lode_error(lode))
        return 1
    if isinstance(lode.get("archived_at"), int):
        print(f"Lode '{lode_id}' is archived and cannot change.")
        print(f"Inspect with: hop lode status {lode_id}")
        return 1
    if not lode.get("active"):
        print(f"Lode '{lode_id}' is not active.")
        print(f"Resume with: hop lode resume {lode_id}")
        return 1
    return None


def _run_local_lode_watch(socket_path: Path, initial_lode: dict) -> int:
    """Watch one locally resident lode using events plus durable reconciliation."""
    import hopper.client as client

    lode_id = initial_lode["id"]
    condition = threading.Condition()
    events: list[dict] = []
    reconcile_requested = False
    prior_state = initial_lode.get("state")
    last_line_key = None

    def emit(lode: dict) -> None:
        nonlocal last_line_key, prior_state
        line_key = (
            lode.get("stage"),
            lode.get("state"),
            lode.get("status"),
            lode.get("active"),
            lode.get("archived_at"),
        )
        if line_key != last_line_key:
            print(_format_watch_line(lode), flush=True)
            last_line_key = line_key
        state = lode.get("state")
        if state != prior_state:
            if state == "gated":
                print(f"Lode {lode_id} is gated. Review with: hop gate show {lode_id}", flush=True)
            elif prior_state == "gated":
                print(f"Lode {lode_id} gate resumed.", flush=True)
            prior_state = state

    emit(initial_lode)
    terminal_code = _watch_terminal_code(initial_lode)
    if terminal_code is not None:
        return terminal_code

    def on_message(message: dict) -> None:
        if message.get("type") not in ("lode_updated", "lode_archived"):
            return
        lode = message.get("lode")
        if not isinstance(lode, dict) or lode.get("id") != lode_id:
            return
        with condition:
            events.append(dict(lode))
            condition.notify_all()

    def request_reconcile() -> None:
        nonlocal reconcile_requested
        with condition:
            reconcile_requested = True
            condition.notify()

    connection = client.HopperConnection(socket_path)
    last_successful_read = _watch_monotonic()
    next_reconcile = last_successful_read
    last_probe_summary = _probe_summary_entry("local", "found", lode_id)
    try:
        connection.start(callback=on_message, on_connect=request_reconcile)
        # Reconcile immediately after start as well as after each confirmed connection.
        request_reconcile()
        while True:
            with condition:
                pending_events = list(events)
                events.clear()
                should_reconcile = reconcile_requested
                reconcile_requested = False
            if pending_events:
                for event_lode in pending_events:
                    emit(event_lode)
                should_reconcile = True

            now = _watch_monotonic()
            if should_reconcile or now >= next_reconcile:
                outcome, snapshot, last_probe_summary = _read_watch_snapshot(
                    socket_path,
                    lode_id,
                )
                next_reconcile = now + WATCH_RECONCILE_SECONDS
                if outcome == "found":
                    last_successful_read = _watch_monotonic()
                    emit(snapshot)
                    terminal_code = _watch_terminal_code(snapshot)
                    if terminal_code is not None:
                        return terminal_code
                elif outcome == "absent":
                    print(f"Lode '{lode_id}' not found. Probes: {last_probe_summary}.")
                    print(f"Retry with: hop lode status {lode_id}")
                    return 1

            now = _watch_monotonic()
            if now - last_successful_read >= WATCH_OBSERVER_TIMEOUT_SECONDS:
                print(f"Lode status unavailable for '{lode_id}'. Probes: {last_probe_summary}.")
                print(f"Retry with: hop lode status {lode_id}")
                return 2

            deadline = min(
                next_reconcile,
                last_successful_read + WATCH_OBSERVER_TIMEOUT_SECONDS,
            )
            with condition:
                if not events:
                    _watch_condition_wait(
                        condition,
                        max(0.0, deadline - _watch_monotonic()),
                    )
    except KeyboardInterrupt:
        return 0
    finally:
        connection.stop()


def _tail_text(text: str, lines: int = 10) -> str:
    """Return the last N lines of text."""
    return "\n".join(text.splitlines()[-lines:])


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
    wait_p.add_argument(
        "--poll", type=float, default=30, help="Status reconciliation interval seconds"
    )
    wait_p.add_argument(
        "--observer-timeout",
        type=float,
        default=300,
        help="Seconds without a valid status observation before failing (0=disabled)",
    )
    wait_p.add_argument("--json", dest="json_output", action="store_true", help="Output JSONL")
    status_p = subs.add_parser("status", help="Show a lode's status", exit_on_error=False)
    status_p.add_argument("lode_id", help="Lode ID to show")
    status_p.add_argument("--json", dest="json_output", action="store_true", help="Output JSON")
    show_p = subs.add_parser("show", help="Show a lode's status", exit_on_error=False)
    show_p.add_argument("lode_id", help="Lode ID to show")
    show_p.add_argument("--json", dest="json_output", action="store_true", help="Output JSON")
    path_p = subs.add_parser("path", help="Show a lode's worktree path", exit_on_error=False)
    path_p.add_argument("lode_id", help="Lode ID to show")
    path_p.add_argument("--json", dest="json_output", action="store_true", help="Output JSON")
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
    nudge_p.add_argument("text", nargs="?", default=None, help="Text to submit (default: continue)")
    nudge_p.add_argument("--text", dest="text_option", default=None, help="Text to submit")
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
        if args and args[0] == "wait":
            from hopper.wait import WAIT_SUMMARY_NO_TARGET

            print(WAIT_SUMMARY_NO_TARGET, file=sys.stderr)
        return 1
    except SystemExit:
        return 0

    subcommand = parsed.subcommand or "list"
    if subcommand == "nudge" and parsed.text is not None and parsed.text_option is not None:
        print("error: positional text and --text cannot be used together")
        nudge_p.print_usage()
        return 1
    if subcommand == "nudge":
        parsed.nudge_text = (
            parsed.text_option
            if parsed.text_option is not None
            else parsed.text
            if parsed.text is not None
            else "continue"
        )
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
            lodes = [lode_with_status_annotations(lode) for lode in lodes]
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
        _warn_target_load(socket_path)
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
        resolved = _resolve_lode_all_sources(socket_path, parsed.lode_id)
        if resolved["outcome"] != "found":
            print(resolved["error"])
            return resolved["exit_code"]
        lode_id = resolved["canonical_id"]
        if resolved["host"] != "local":
            return _run_remote_cli(
                resolved["host"],
                ["lode", subcommand, lode_id],
                reason=f"lode {lode_id}",
            )
        operation = client.pause_lode if subcommand == "pause" else client.resume_lode
        response = operation(socket_path, lode_id)
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

    if subcommand == "path":
        resolved = _resolve_lode_all_sources(socket_path, parsed.lode_id)
        if resolved["outcome"] != "found":
            print(
                resolved["error"],
                file=sys.stderr if parsed.json_output else sys.stdout,
            )
            return resolved["exit_code"]

        lode_id = resolved["canonical_id"]
        host = resolved["host"]
        if host == "local":
            worktree = get_worktree_dir(lode_id)
            try:
                path = worktree.resolve(strict=True) if worktree.is_dir() else None
            except OSError:
                path = None
            if path is None or not path.is_dir():
                print(
                    f"No worktree exists for lode '{lode_id}'.",
                    file=sys.stderr if parsed.json_output else sys.stdout,
                )
                return 1
            payload = {
                "id": lode_id,
                "host": "local",
                "path": str(path),
                "exists": True,
            }
        else:
            from hopper.remote import run_remote

            try:
                remote_result = run_remote(
                    host,
                    ["lode", "path", lode_id, "--json"],
                    timeout=8,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                print(f"Lode path unavailable for '{lode_id}' on {host}: {error}", file=sys.stderr)
                return 2
            if remote_result.returncode != 0:
                output = f"{remote_result.stdout}\n{remote_result.stderr}".lower()
                if "no worktree exists" in output:
                    print(
                        f"No worktree exists for lode '{lode_id}' on {host}.",
                        file=sys.stderr if parsed.json_output else sys.stdout,
                    )
                    return 1
                if "not found" in output:
                    print(
                        f"Lode '{lode_id}' not found on {host}.",
                        file=sys.stderr if parsed.json_output else sys.stdout,
                    )
                    return 1
                print(
                    f"Lode path unavailable for '{lode_id}' on {host}: "
                    f"remote command exited {remote_result.returncode}",
                    file=sys.stderr,
                )
                return 2
            try:
                payload = json.loads(remote_result.stdout)
            except json.JSONDecodeError:
                payload = None
            if (
                not isinstance(payload, dict)
                or set(payload) != {"id", "host", "path", "exists"}
                or payload.get("id") != lode_id
                or not isinstance(payload.get("path"), str)
                or not Path(payload["path"]).is_absolute()
                or payload.get("exists") is not True
            ):
                print(
                    f"Lode path unavailable for '{lode_id}' on {host}: invalid remote response",
                    file=sys.stderr,
                )
                return 2
            payload = dict(payload)
            payload["host"] = host

        if parsed.json_output:
            print(json.dumps(payload, separators=(",", ":")))
        elif host == "local":
            print(payload["path"])
        else:
            print(f"{host}:{payload['path']}")
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
        if lode.get("active") and not parsed.force:
            print(f"Cannot restart: lode {lode_id} has a registered runner.")
            print(f"Attach to it, or retry with: hop lode restart {lode_id} --force")
            return 1
        if lode.get("active") and parsed.force:
            print(f"Terminating the registered runner for lode {lode_id} before restart.")
        stage = lode.get("stage", "")
        if stage not in ("mill", "refine", "ship"):
            print(f"Cannot restart: lode {lode_id} stage is {stage}.")
            print(f"Check its current state with: hop lode status {lode_id}")
            return 1
        started = bool(lode.get("claude", {}).get(stage, {}).get("started"))
        if started and not parsed.force and lode.get("state") != "error":
            print(f"Lode {lode_id} has been started (claude[{stage}].started=True).")
            print("Restarting discards in-progress work.")
            print("Pass --force to override:")
            print(f"  hop lode restart {lode_id} --force")
            return 1
        acknowledgement = client.restart_lode(
            socket_path,
            lode_id,
            stage,
            force=parsed.force,
        )
        if acknowledgement is None:
            print(
                "Restart disposition is UNKNOWN because the server did not acknowledge it. "
                f"Check `hop lode status {lode_id}` before retrying."
            )
            return 1
        if acknowledgement.get("accepted") is not True:
            reason = acknowledgement.get("reason")
            messages = {
                "lode_not_found": (
                    f"Restart refused: lode {lode_id} was not found. Check the ID with "
                    "`hop lode list`, then retry."
                ),
                "invalid_stage": (
                    f"Restart refused: stage {stage} is not restartable. Check "
                    f"`hop lode status {lode_id}`."
                ),
                "pending_runner_result": (
                    "Restart refused while the prior guarded runner result is pending. "
                    "Wait about 60 seconds, check "
                    f"`hop lode status {lode_id}`, then retry."
                ),
                "runner_identity_unknown": (
                    "Restart refused because the existing runner identity could not be "
                    f"verified. Inspect `hop lode status {lode_id}`, then retry after the "
                    "runner is gone."
                ),
                "runner_identity_unverified": (
                    "Restart refused because the registered runner could not be matched to "
                    f"its live tmux pane. Inspect `hop lode status {lode_id}` and the pane, "
                    "then retry after the runner is gone."
                ),
                "termination_failed": (
                    "Restart refused because the existing runner did not exit. Inspect "
                    f"`hop lode status {lode_id}`, then retry after it exits."
                ),
                "already_live": (
                    "Restart refused because a runner is still live. Attach to it, or retry "
                    f"with `hop lode restart {lode_id} --force`."
                ),
                "tmux_unreachable": (
                    "Restart refused because tmux liveness is unknown. Verify tmux is "
                    f"running, check `hop lode status {lode_id}`, then retry."
                ),
                "spawn_failed": (
                    "Restart failed while creating the replacement pane. Verify tmux is "
                    f"running, check `hop lode status {lode_id}`, then retry."
                ),
            }
            print(
                messages.get(
                    reason,
                    "Restart was refused by the server. "
                    f"Check `hop lode status {lode_id}`, then retry.",
                )
            )
            return 1
        print(f"Restarting {stage} for {lode_id}")
        return 0

    if subcommand == "watch":
        if (rc := require_not_inside_lode()) is not None:
            return rc
        resolved = _resolve_lode_all_sources(socket_path, parsed.lode_id)
        if resolved["outcome"] != "found":
            print(resolved["error"])
            return resolved["exit_code"]
        if resolved["host"] == "local":
            return _run_local_lode_watch(socket_path, resolved["lode"])

        from hopper.remote import run_remote_streaming

        try:
            return run_remote_streaming(
                resolved["host"],
                ["lode", "watch", resolved["canonical_id"]],
            )
        except OSError as error:
            print(
                f"remote command failed on {resolved['host']}: {error}",
                file=sys.stderr,
            )
            return 2

    if subcommand == "wait":
        from hopper.wait import WAIT_SUMMARY_NO_TARGET, wait_for_lodes

        if (rc := require_not_inside_lode()) is not None:
            print(WAIT_SUMMARY_NO_TARGET, file=sys.stderr)
            return rc

        return wait_for_lodes(
            socket_path,
            parsed.lode_id,
            timeout_s=parsed.timeout,
            poll_s=parsed.poll,
            observer_timeout_s=parsed.observer_timeout,
            json_output=parsed.json_output,
            lookup_local=_lookup_lode,
            find_remote=lambda prefix: _find_remote_lode(prefix, remember_result=False),
            probe_remote=_remote_lode_status,
        )

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
                remote_args = ["lode", subcommand, remote_lode["id"]]
                if subcommand == "peek":
                    remote_args.extend(["-n", str(parsed.lines)])
                elif subcommand == "nudge":
                    remote_args.extend(["--", parsed.nudge_text])
                else:
                    remote_args.append(parsed.choice)
                return _run_remote_cli(
                    remote_lode["host"],
                    remote_args,
                    reason=f"lode {remote_lode['id']}",
                )
            return err
        resolved = _lookup_lode_with_remote(socket_path, parsed.lode_id)
        if resolved["outcome"] != "found":
            print(resolved["error"])
            return resolved["exit_code"]
        lode = resolved["lode"]
        if resolved["host"]:
            remote_args = ["lode", subcommand, resolved["canonical_id"]]
            if subcommand == "peek":
                remote_args.extend(["-n", str(parsed.lines)])
            elif subcommand == "nudge":
                remote_args.extend(["--", parsed.nudge_text])
            else:
                remote_args.append(parsed.choice)
            return _run_remote_cli(resolved["host"], remote_args, reason=f"lode {lode['id']}")

        if subcommand == "peek":
            pane = lode.get("tmux_pane")
            pane_text = capture_pane(pane, plain=True) if pane else None
            if pane_text is None:
                print(
                    f"pane {pane or '<unknown>'} no longer exists "
                    f"(lode active={lode.get('active')}, state={lode.get('state')})"
                )
                return 1
            lines = max(1, parsed.lines)
            print("\n".join(pane_text.splitlines()[-lines:]))
            return 0
        if subcommand == "answer" and parsed.choice not in {str(i) for i in range(1, 10)}:
            print("choice must be a digit 1..9")
            return 1
        text = parsed.nudge_text if subcommand == "nudge" else parsed.choice
        response = client.send_pane_input(
            socket_path,
            lode["id"],
            text,
            paste=subcommand == "nudge",
        )
        if response and response.get("type") == "pane_input_sent":
            print("submitted")
            return 0

        if response is None:
            error = (
                "The pane-input request returned no response. The delivery outcome is unknown. "
                f"Inspect with `hop lode peek {lode['id']}` before deciding whether to retry; "
                "do not resend blindly."
            )
        else:
            error = response.get("error", "Pane input failed without a recovery message.")
        print(error, file=sys.stderr)
        if response and response.get("outcome") == "unverified" and response.get("tail"):
            print("--- pane tail ---", file=sys.stderr)
            print(response["tail"], file=sys.stderr)
            print("--- end pane tail ---", file=sys.stderr)
        return 1

    if subcommand in ("status", "show"):
        return _show_lode_status(
            socket_path,
            parsed.lode_id,
            json_output=getattr(parsed, "json_output", False),
        )

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


@command(
    "feedback",
    "Send verified feedback to a gated lode (alias for gate feedback)",
    group="aliases",
)
def cmd_feedback(args: list[str]) -> int:
    """Alias for hop gate feedback."""
    if "-h" in args or "--help" in args:
        p = make_parser("feedback", _GATE_FEEDBACK_DESCRIPTION)
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
        p.add_argument(
            "--poll", type=float, default=30, help="Status reconciliation interval seconds"
        )
        p.add_argument(
            "--observer-timeout",
            type=float,
            default=300,
            help="Seconds without a valid status observation before failing (0=disabled)",
        )
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
CHECK_CPU_QUIET_THRESHOLD_MS = 60_000


class _CheckProgress:
    """Report elapsed time and sustained process-tree CPU silence."""

    def __init__(self, command_text: str, started_at: int) -> None:
        self.command_text = hopper_code.truncate_progress_command(command_text)
        self.started_at = started_at
        self.pid: int | None = None
        self.last_cpu_ms: int | None = None
        self.last_cpu_change_at = started_at

    def bind(self, pid: int) -> None:
        """Bind the tracker to the command process after launch."""
        self.pid = pid

    def summary(self, now_ms: int) -> str:
        """Return a factual progress summary for the current process tree."""
        cpu_quiet_ms = 0
        if self.pid is not None:
            cpu_ms = _sum_process_tree_cpu_ms(self.pid)
            if cpu_ms is not None:
                if self.last_cpu_ms is None or cpu_ms > self.last_cpu_ms:
                    self.last_cpu_change_at = now_ms
                self.last_cpu_ms = cpu_ms
                cpu_quiet_ms = now_ms - self.last_cpu_change_at

        elapsed = hopper_code.format_progress_duration(now_ms - self.started_at)
        summary = f"{self.command_text} — running {elapsed}"
        if cpu_quiet_ms >= CHECK_CPU_QUIET_THRESHOLD_MS:
            quiet = hopper_code.format_progress_duration(cpu_quiet_ms)
            return f"{summary}; no process-tree CPU progress for {quiet}"
        return summary


@command("check", "Run a validation command with bounded output and its real exit status")
def cmd_check(args: list[str]) -> int:
    """Run a bare-terminal command, print its output tail, and return its real status.

    Replaces the false-green `make ci 2>&1 | tail -30` pattern used to keep a
    long CI log out of an agent's context: a pipe reports the pager's exit code,
    not the command's, so a failing build can be truncated into an apparent
    success. `hop check` captures combined stdout+stderr, prints the last -n
    lines plus an explicit `exited N` summary, and returns the command's own
    exit code. The CLI dispatcher refuses non-terminal stdout before this runs,
    because a downstream pipe would otherwise mask this function's status --
    unless --allow-capture says the caller propagates the exit code itself.
    """
    parser = make_parser(
        "check",
        "Run a validation command with terminal output, print only its tail, and exit "
        "with the command's real status. "
        "Usage: hop check [-n LINES] [--allow-capture] -- <command> [args...]",
    )
    parser.add_argument(
        "-n",
        "--lines",
        type=int,
        default=CHECK_TAIL_LINES,
        help=f"Trailing output lines to print (default: {CHECK_TAIL_LINES})",
    )
    parser.add_argument(
        "--allow-capture",
        action="store_true",
        help=(
            "Run even though stdout is captured rather than a terminal. Pass this only "
            "when you propagate the exit code; it is unsafe with a downstream pipe."
        ),
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

    reap_swiftpm_testing_helpers()

    heartbeat = None
    progress = None
    lode_id = get_hopper_lid()
    if lode_id:
        try:
            started_at = current_time_ms()
            command_text = " ".join(command)
            progress = _CheckProgress(command_text, started_at)
            heartbeat = hopper_code.ProgressHeartbeat(
                lambda summary: set_lode_progress(_socket(), lode_id, summary),
                progress.summary,
                interval=hopper_code.HEARTBEAT_INTERVAL_SEC,
            )
        except Exception:
            logger.debug("failed to create check heartbeat", exc_info=True)

    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as output_file:
        try:
            proc = subprocess.Popen(
                command,
                stdout=output_file,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except FileNotFoundError:
            if heartbeat:
                try:
                    heartbeat.stop()
                except Exception:
                    logger.debug("failed to stop check heartbeat", exc_info=True)
            print(f"hop check: command not found: {command[0]}", file=sys.stderr)
            return 127

        if progress:
            progress.bind(proc.pid)

        try:
            if heartbeat:
                try:
                    heartbeat.start()
                except Exception:
                    logger.debug("failed to start check heartbeat", exc_info=True)
            proc.wait()
        finally:
            if heartbeat:
                try:
                    heartbeat.stop()
                except Exception:
                    logger.debug("failed to stop check heartbeat", exc_info=True)

        output_file.seek(0)
        output = output_file.read()

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


def _check_stdout_is_terminal() -> bool:
    """Return whether `hop check` can expose its status directly to its caller."""
    try:
        return sys.stdout.isatty()
    except (AttributeError, OSError, ValueError):
        return False


def _hop_own_args(cmd_args: list[str]) -> list[str]:
    """Return only hop's own flags, stopping at the `--` that begins the payload.

    `hop check -- make ci --allow-capture` must not be read as passing
    --allow-capture to hop; everything after the first `--` belongs to the
    command being run.
    """
    if "--" in cmd_args:
        return cmd_args[: cmd_args.index("--")]
    return cmd_args


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

    if cmd == "check":
        own_args = _hop_own_args(cmd_args)
        if not any(arg in {"-h", "--help"} for arg in own_args):
            if not _check_stdout_is_terminal() and "--allow-capture" not in own_args:
                print(
                    "hop check: refusing non-terminal stdout because a downstream pipe can "
                    "mask its exit status; run `hop check -n <lines> -- <command>` bare. "
                    "If your stdout is captured (not piped) and you propagate the exit "
                    "code, re-run with --allow-capture. "
                    "No validation command was started.",
                    file=sys.stderr,
                )
                return 2

    if explicit_host and explicit_host != "local" and not _remote_disabled():
        expanded_arg = _locally_expanded_home_arg(cmd, cmd_args)
        if expanded_arg:
            print(
                f"error: remote argument {expanded_arg!r} points into the local home; "
                "quote the tilde (for example, '~/src') so hop expands it on the remote host",
                file=sys.stderr,
            )
            return 2
        if cmd == "watch" or (cmd == "lode" and cmd_args[:1] == ["watch"]):
            from hopper.remote import run_remote_streaming

            try:
                return run_remote_streaming(explicit_host, [cmd, *cmd_args])
            except OSError as error:
                print(f"remote command failed on {explicit_host}: {error}", file=sys.stderr)
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
