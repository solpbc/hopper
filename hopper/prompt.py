"""Prompt loader for hopper."""

from pathlib import Path

PROMPTS_DIR = Path(__file__).parent / "prompts"


def load(name: str) -> str:
    """Load a prompt by name.

    Args:
        name: Prompt name (with or without .md extension)

    Returns:
        The prompt content with trailing whitespace stripped.

    Raises:
        FileNotFoundError: If the prompt doesn't exist.
    """
    if not name.endswith(".md"):
        name = f"{name}.md"

    path = PROMPTS_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Prompt not found: {name}")

    return path.read_text().strip()
