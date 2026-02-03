"""Codex CLI wrapper for hopper."""

import logging
import subprocess

logger = logging.getLogger(__name__)


def run_codex(prompt: str, cwd: str, output_file: str) -> tuple[int, list[str]]:
    """Run Codex in one-shot mode with full permissions.

    Args:
        prompt: The prompt text to send to Codex.
        cwd: Working directory for Codex.
        output_file: Path to write the final agent message.

    Returns:
        (exit_code, cmd) tuple. Exit code is 127 if codex not found,
        130 on KeyboardInterrupt.
    """
    cmd = [
        "codex",
        "exec",
        "--dangerously-bypass-approvals-and-sandbox",
        "-o",
        output_file,
        prompt,
    ]

    logger.debug(f"Running: codex exec in {cwd}")

    try:
        result = subprocess.run(cmd, cwd=cwd)
        return result.returncode, cmd
    except FileNotFoundError:
        logger.error("codex command not found")
        return 127, cmd
    except KeyboardInterrupt:
        return 130, cmd
