"""TUI for managing coding agents."""

from dataclasses import dataclass, field

from blessed import Terminal


@dataclass
class Row:
    """A row in a table."""

    id: str
    label: str


@dataclass
class TUIState:
    """State for the TUI."""

    ore_rows: list[Row] = field(default_factory=lambda: [Row("new", "new shovel")])
    processing_rows: list[Row] = field(default_factory=list)
    cursor_index: int = 0

    @property
    def total_rows(self) -> int:
        return len(self.ore_rows) + len(self.processing_rows)

    def cursor_up(self) -> "TUIState":
        new_index = (self.cursor_index - 1) % self.total_rows
        return TUIState(self.ore_rows, self.processing_rows, new_index)

    def cursor_down(self) -> "TUIState":
        new_index = (self.cursor_index + 1) % self.total_rows
        return TUIState(self.ore_rows, self.processing_rows, new_index)


def render(term: Terminal, state: TUIState) -> None:
    """Render the TUI to the terminal."""
    print(term.home + term.clear, end="")

    row_num = 0

    # ORE table
    print(term.bold("ORE"))
    print()
    for row in state.ore_rows:
        if row_num == state.cursor_index:
            print(term.reverse(f"> {row.label}"))
        else:
            print(f"  {row.label}")
        row_num += 1

    # Spacing between tables
    print()
    print()

    # PROCESSING table
    print(term.bold("PROCESSING"))
    print()
    if state.processing_rows:
        for row in state.processing_rows:
            if row_num == state.cursor_index:
                print(term.reverse(f"> {row.label}"))
            else:
                print(f"  {row.label}")
            row_num += 1
    else:
        print(term.dim("  (empty)"))


def run_tui(term: Terminal) -> int:
    """Run the TUI main loop."""
    state = TUIState()

    with term.fullscreen(), term.cbreak(), term.hidden_cursor():
        render(term, state)

        while True:
            key = term.inkey()

            if key.name == "KEY_UP" or key == "k":
                state = state.cursor_up()
            elif key.name == "KEY_DOWN" or key == "j":
                state = state.cursor_down()
            elif key == "q" or key.name == "KEY_ESCAPE":
                break

            render(term, state)

    return 0
