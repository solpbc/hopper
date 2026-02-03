"""Ore runner - wraps Claude execution with session lifecycle management."""

from pathlib import Path

from hopper import prompt
from hopper.runner import BaseRunner


class OreRunner(BaseRunner):
    """Runs Claude for an ore-stage session, managing active/inactive state."""

    _done_label = "Shovel done"
    _first_run_state = "new"
    _done_status = "Shovel-ready prompt saved"
    _next_stage = "processing"

    def __init__(self, session_id: str, socket_path: Path):
        super().__init__(session_id, socket_path)
        self.scope: str = ""

    def _load_session_data(self, session_data: dict) -> None:
        self.scope = session_data.get("scope", "")

    def _setup(self) -> int | None:
        # Validate project directory if set
        if self.project_dir and not Path(self.project_dir).is_dir():
            print(f"Project directory not found: {self.project_dir}")
            return 1
        return None

    def _build_command(self) -> tuple[list[str], str | None]:
        cwd = self.project_dir if self.project_dir else None

        skip = "--dangerously-skip-permissions"

        if self.is_first_run:
            context = {}
            if self.project_name:
                context["project"] = self.project_name
            if self.project_dir:
                context["dir"] = self.project_dir
            if self.scope:
                context["scope"] = self.scope
            initial_prompt = prompt.load("shovel", context=context if context else None)
            cmd = ["claude", skip, "--session-id", self.session_id, initial_prompt]
        else:
            cmd = ["claude", skip, "--resume", self.session_id]

        return cmd, cwd


def run_ore(session_id: str, socket_path: Path) -> int:
    """Entry point for ore command."""
    runner = OreRunner(session_id, socket_path)
    return runner.run()
