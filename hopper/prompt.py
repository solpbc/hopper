"""Prompt loader for hopper."""

from pathlib import Path
from string import Template

from hopper.config import load_config

PROMPTS_DIR = Path(__file__).parent / "prompts"


def _build_template_vars(context: dict[str, str] | None = None) -> dict[str, str]:
    """Build template variables from config and context.

    Loads user config as base, merges with context (context wins on conflict),
    and adds uppercase-first versions of all keys.

    Args:
        context: Optional dict to merge on top of config values.

    Returns:
        Dict of template variables including uppercase-first versions.
    """
    # Start with user config as base
    config = load_config()

    # Merge context on top (context takes precedence)
    if context:
        config.update(context)

    # Build final vars with uppercase-first versions
    template_vars: dict[str, str] = {}
    for key, value in config.items():
        template_vars[key] = value
        template_vars[key.capitalize()] = value.capitalize()

    return template_vars


def load(name: str, context: dict[str, str] | None = None) -> str:
    """Load a prompt by name with template substitution.

    Supports $variable substitution using Python's string.Template.
    Variables come from user config (hop config) merged with optional
    context dict. Context values override config values on conflict.
    For each key, an uppercase-first version is also available
    (e.g., name=bob provides both $name and $Name).

    Args:
        name: Prompt name (with or without .md extension)
        context: Optional dict of template variables (overrides config)

    Returns:
        The prompt content with trailing whitespace stripped and
        template variables substituted.

    Raises:
        FileNotFoundError: If the prompt doesn't exist.
    """
    if not name.endswith(".md"):
        name = f"{name}.md"

    path = PROMPTS_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Prompt not found: {name}")

    text = path.read_text().strip()
    template_vars = _build_template_vars(context)

    return Template(text).safe_substitute(template_vars)
