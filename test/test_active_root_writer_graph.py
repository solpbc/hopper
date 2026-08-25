# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Static guard for production writers of the active lode root."""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = ROOT / "hopper"

EXPECTED_DIRECT_SAVE_CALLERS = Counter(
    {
        "hopper/lodes.py::_update_lode_field": 1,
        "hopper/lodes.py::archive_lode": 1,
        "hopper/lodes.py::archive_lode_for_action": 1,
        "hopper/lodes.py::begin_lode_gate_delivery": 1,
        "hopper/lodes.py::bind_lode_stage_session": 1,
        "hopper/lodes.py::clear_lode_gate": 1,
        "hopper/lodes.py::create_lode": 1,
        "hopper/lodes.py::publish_lode_gate": 1,
        "hopper/lodes.py::reset_lode_stage_session": 1,
        "hopper/lodes.py::set_lode_stage_session_started": 1,
        "hopper/lodes.py::unarchive_lode": 1,
        "hopper/lodes.py::update_lode_coder_session": 1,
        "hopper/lodes.py::update_lode_state": 1,
        "hopper/projects.py::rename_project_in_data": 1,
        "hopper/server.py::_deliver_lode_pane_input": 1,
        "hopper/server.py::Server._adopt_action_spawn_receipt": 1,
        "hopper/server.py::Server._apply_completion_backlog": 1,
        "hopper/server.py::Server._apply_completion_stage": 1,
        "hopper/server.py::Server._apply_manual_archive": 1,
        "hopper/server.py::Server._apply_manual_lode_mutation": 1,
        "hopper/server.py::Server._clear_completed_action": 1,
        "hopper/server.py::Server._clear_completed_manual_action": 2,
        "hopper/server.py::Server._finalize_lode_disconnect": 1,
        "hopper/server.py::Server._gated_spawn": 1,
        "hopper/server.py::Server._gated_spawn.persist_outcome": 1,
        "hopper/server.py::Server._handle_lode_run_result": 1,
        "hopper/server.py::Server._handle_mutation": 3,
        "hopper/server.py::Server._handle_mutation.refuse_stage_protocol": 1,
        "hopper/server.py::Server._handle_registration_capture_result": 1,
        "hopper/server.py::Server._on_client_disconnect": 1,
        "hopper/server.py::Server._prepare_action_spawn": 1,
        "hopper/server.py::Server._project_action": 1,
        "hopper/server.py::Server._promote_backlog_item": 1,
        "hopper/server.py::Server._reconcile_action_records": 1,
        "hopper/server.py::Server._reconcile_startup_lodes": 1,
        "hopper/server.py::Server._register_lode_client": 1,
        "hopper/server.py::Server._save_reap_progress": 1,
        "hopper/server.py::Server._set_action_refusal": 1,
        "hopper/server.py::Server._set_terminal_failure": 1,
    }
)

EXPECTED_LODES_WRITER_FUNCTIONS = {
    "_update_lode_field",
    "archive_lode",
    "archive_lode_for_action",
    "begin_lode_gate_delivery",
    "bind_lode_stage_session",
    "clear_lode_gate",
    "create_lode",
    "publish_lode_gate",
    "reset_lode_claude_stage",
    "reset_lode_stage_session",
    "set_lode_claude_started",
    "set_lode_stage_session_started",
    "unarchive_lode",
    "update_lode_branch",
    "update_lode_coder_session",
    "update_lode_codex_thread",
    "update_lode_stage",
    "update_lode_state",
    "update_lode_status",
    "update_lode_title",
    "update_lode_worktree_path",
}

EXPECTED_PROJECT_WRITER_FUNCTIONS = {"rename_project_in_data"}

EXPECTED_EXTERNAL_WRITER_IMPORTS = {
    ("hopper/projects.py", "save_lodes"),
    ("hopper/server.py", "archive_lode"),
    ("hopper/server.py", "archive_lode_for_action"),
    ("hopper/server.py", "begin_lode_gate_delivery"),
    ("hopper/server.py", "bind_lode_stage_session"),
    ("hopper/server.py", "clear_lode_gate"),
    ("hopper/server.py", "create_lode"),
    ("hopper/server.py", "publish_lode_gate"),
    ("hopper/server.py", "reset_lode_claude_stage"),
    ("hopper/server.py", "save_lodes"),
    ("hopper/server.py", "set_lode_claude_started"),
    ("hopper/server.py", "unarchive_lode"),
    ("hopper/server.py", "update_lode_branch"),
    ("hopper/server.py", "update_lode_coder_session"),
    ("hopper/server.py", "update_lode_codex_thread"),
    ("hopper/server.py", "update_lode_stage"),
    ("hopper/server.py", "update_lode_state"),
    ("hopper/server.py", "update_lode_status"),
    ("hopper/server.py", "update_lode_title"),
    ("hopper/server.py", "update_lode_worktree_path"),
}

EXPECTED_RENAME_DATA_IMPORTS = {("hopper/cli.py", "rename_project_in_data")}


def _qualified_scope(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    names: list[str] = []
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(current.name)
    return ".".join(reversed(names)) or "<module>"


def _direct_save_callers(sources: dict[str, str]) -> Counter[str]:
    callers: Counter[str] = Counter()
    for relative_path, source in sources.items():
        tree = ast.parse(source, filename=relative_path)
        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "save_lodes"
            ):
                callers[f"{relative_path}::{_qualified_scope(node, parents)}"] += 1
    return callers


def _module_writer_functions(source: str, *, filename: str) -> set[str]:
    tree = ast.parse(source, filename=filename)
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    local_calls = {
        name: {
            call.func.id
            for call in ast.walk(node)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
        }
        & functions.keys()
        for name, node in functions.items()
    }
    writers = {
        name
        for name, node in functions.items()
        if any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "save_lodes"
            for call in ast.walk(node)
        )
    }
    while True:
        expanded = writers | {name for name, calls in local_calls.items() if calls & writers}
        if expanded == writers:
            return writers
        writers = expanded


def _external_imports(
    sources: dict[str, str], *, module: str, names: set[str]
) -> set[tuple[str, str]]:
    imports: set[tuple[str, str]] = set()
    for relative_path, source in sources.items():
        tree = ast.parse(source, filename=relative_path)
        module_aliases: dict[str, tuple[str, ...]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == module:
                for alias in node.names:
                    if alias.name == "*" or alias.name in names:
                        imports.add((relative_path, alias.name))
            elif isinstance(node, ast.ImportFrom) and node.module == module.rpartition(".")[0]:
                leaf = module.rpartition(".")[2]
                for alias in node.names:
                    if alias.name == leaf:
                        module_aliases[alias.asname or alias.name] = ()
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == module:
                        if alias.asname:
                            module_aliases[alias.asname] = ()
                        else:
                            module_aliases[module.partition(".")[0]] = tuple(module.split(".")[1:])
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            parts: list[str] = []
            current: ast.AST = node
            while isinstance(current, ast.Attribute):
                parts.append(current.attr)
                current = current.value
            if not isinstance(current, ast.Name):
                continue
            parts.append(current.id)
            parts.reverse()
            suffix = module_aliases.get(parts[0])
            if suffix is None or tuple(parts[1:-1]) != suffix or parts[-1] not in names:
                continue
            imports.add((relative_path, parts[-1]))
    return imports


def _assert_exact(label: str, actual: object, expected: object) -> None:
    assert actual, f"{label} discovery returned zero; the guard is not observing production"
    assert actual == expected, f"{label} changed\nactual={actual!r}\nexpected={expected!r}"


def test_production_active_root_writer_graph_is_exact() -> None:
    sources = {
        path.relative_to(ROOT).as_posix(): path.read_text()
        for path in sorted(PRODUCTION.glob("*.py"))
    }
    direct_callers = _direct_save_callers(sources)
    writer_functions = _module_writer_functions(
        sources["hopper/lodes.py"], filename="hopper/lodes.py"
    )
    project_writer_functions = _module_writer_functions(
        sources["hopper/projects.py"], filename="hopper/projects.py"
    )
    external_imports = _external_imports(
        {path: source for path, source in sources.items() if path != "hopper/lodes.py"},
        module="hopper.lodes",
        names=writer_functions | {"save_lodes"},
    )
    rename_imports = _external_imports(
        {path: source for path, source in sources.items() if path != "hopper/projects.py"},
        module="hopper.projects",
        names=project_writer_functions,
    )

    _assert_exact("direct save_lodes callers", direct_callers, EXPECTED_DIRECT_SAVE_CALLERS)
    _assert_exact(
        "transitive hopper.lodes writers", writer_functions, EXPECTED_LODES_WRITER_FUNCTIONS
    )
    _assert_exact(
        "transitive hopper.projects writers",
        project_writer_functions,
        EXPECTED_PROJECT_WRITER_FUNCTIONS,
    )
    _assert_exact(
        "external active-root writer imports", external_imports, EXPECTED_EXTERNAL_WRITER_IMPORTS
    )
    _assert_exact("project data-rename imports", rename_imports, EXPECTED_RENAME_DATA_IMPORTS)


def test_writer_graph_guard_rejects_zero_stale_and_unauthorized_roots() -> None:
    with pytest.raises(AssertionError, match="returned zero"):
        _assert_exact("fixture", set(), {"expected"})
    with pytest.raises(AssertionError, match="changed"):
        _assert_exact("fixture", {"expected"}, {"expected", "stale"})

    sources = {
        "hopper/lodes.py": """
def save_lodes(rows): pass
def write(rows): save_lodes(rows)
def wrapper(rows): write(rows)
""",
        "hopper/cli.py": "from hopper.lodes import wrapper\n",
    }
    assert _direct_save_callers(sources) == Counter({"hopper/lodes.py::write": 1})
    writers = _module_writer_functions(sources["hopper/lodes.py"], filename="hopper/lodes.py")
    imports = _external_imports(
        {"hopper/cli.py": sources["hopper/cli.py"]},
        module="hopper.lodes",
        names=writers | {"save_lodes"},
    )
    assert writers == {"write", "wrapper"}
    assert imports == {("hopper/cli.py", "wrapper")}
    with pytest.raises(AssertionError, match="changed"):
        _assert_exact("fixture imports", imports, {("hopper/server.py", "wrapper")})

    qualified = _external_imports(
        {"hopper/cli.py": "import hopper.lodes as persistence\npersistence.wrapper([])\n"},
        module="hopper.lodes",
        names=writers | {"save_lodes"},
    )
    assert qualified == {("hopper/cli.py", "wrapper")}

    wildcard = _external_imports(
        {"hopper/cli.py": "from hopper.lodes import *\n"},
        module="hopper.lodes",
        names=writers | {"save_lodes"},
    )
    assert wildcard == {("hopper/cli.py", "*")}
