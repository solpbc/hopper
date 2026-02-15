.PHONY: install install-user test ci clean

install:
	uv sync

install-user: install
	@test -f .venv/bin/hop || { echo "error: .venv/bin/hop not found — run 'make install' first"; exit 1; }
	@echo "$$PATH" | tr ':' '\n' | grep -qx "$$HOME/.local/bin" || { echo "error: ~/.local/bin is not in PATH — add it to your shell profile"; exit 1; }
	@mkdir -p ~/.local/bin
	ln -sf $(CURDIR)/.venv/bin/hop ~/.local/bin/hop
	@mkdir -p ~/.claude/skills
	ln -sf $(CURDIR)/skills/hop ~/.claude/skills/hop
	@echo "Symlinked ~/.local/bin/hop → $(CURDIR)/.venv/bin/hop"
	@echo "Symlinked ~/.claude/skills/hop → $(CURDIR)/skills/hop"

test:
	pytest

ci:
	ruff format .
	ruff check --fix .

clean:
	rm -rf build/ dist/ *.egg-info/ .pytest_cache/ .ruff_cache/
	find . -type d -name __pycache__ -exec rm -rf {} +
