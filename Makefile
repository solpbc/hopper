-include .env

.PHONY: install install-user install-skills test ci clean timing

install:
	uv sync

install-user: install
	@test -f .venv/bin/hop || { echo "error: .venv/bin/hop not found — run 'make install' first"; exit 1; }
	@echo "$$PATH" | tr ':' '\n' | grep -qx "$$HOME/.local/bin" || { echo "error: ~/.local/bin is not in PATH — add it to your shell profile"; exit 1; }
	@mkdir -p ~/.local/bin
	ln -sfn $(CURDIR)/.venv/bin/hop ~/.local/bin/hop
	@mkdir -p ~/.claude/skills
	ln -sfn $(CURDIR)/skills/hop ~/.claude/skills/hop
	@echo "Symlinked ~/.local/bin/hop → $(CURDIR)/.venv/bin/hop"
	@echo "Symlinked ~/.claude/skills/hop → $(CURDIR)/skills/hop"
	@echo "Note: run 'make install-skills' (needs EXTRO_ROOT) to also land the hop skill in the org repo's .agents/skills — the path Grok sessions read (compat.claude is off for Grok, so ~/.claude/skills/hop above is Claude/Codex-only)."

# EXTRO_ROOT-scoped install — lands `hop` in the org repo's .claude/skills AND
# .agents/skills (Grok's native, project-scoped skill root; see
# cto/playbooks/grok-build-fleet.md). install-user alone only reaches
# ~/.claude/skills, which Grok never reads (compat.claude.skills=false by
# design). Every other extro-* tool Makefile already installs to both roots
# (see extro-hub/Makefile, extro-tools/Makefile); this target brings hop to
# parity. Not wired into install-user because EXTRO_ROOT (the org repo) does
# not exist on every fleet host that runs `make install-user`.
install-skills:
ifndef EXTRO_ROOT
	$(error EXTRO_ROOT is not set. Create .env with: EXTRO_ROOT=/path/to/extro)
endif
	@for dir in $(EXTRO_ROOT)/.claude/skills $(EXTRO_ROOT)/.agents/skills; do \
		mkdir -p $$dir; \
		rm -rf $$dir/hop; \
		ln -sfn $(CURDIR)/skills/hop $$dir/hop; \
	done
	@echo "  hop → $(CURDIR)/skills/hop"

test:
	uv run pytest

ci:
	uv run ruff format .
	uv run ruff check --fix .
	$(MAKE) test

timing:
	python3 scripts/timing.py

clean:
	rm -rf build/ dist/ *.egg-info/ .pytest_cache/ .ruff_cache/
	find . -type d -name __pycache__ -exec rm -rf {} +
