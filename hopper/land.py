# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Landing engine and pre-land command configuration for Hopper."""

import shlex
import subprocess

from hopper import config
from hopper.git import DEFAULT_BRANCH_NAMES

PRELAND_CONFIG_PREFIX = "preland."


def _print_git_output(proc: subprocess.CompletedProcess) -> None:
    """Print the command's stdout and stderr in an output block."""
    print("output:")
    output = "".join(stream for stream in (proc.stdout, proc.stderr) if stream)
    if output:
        print(output.rstrip("\n"))


def land_feature(project: str, checkout: str, worktree: str, branch: str) -> int:
    """Execute pre-land command, fast-forward merge branch, and push if origin exists."""
    key = f"{PRELAND_CONFIG_PREFIX}{project}"
    try:
        cfg = config.load_config()
    except config.ConfigError as exc:
        print("error: pre-land command refused")
        print(f"observed: config at {exc.path} is {exc.reason}")
        print("Hopper did not run a pre-land command, did not merge, and did not push.")
        return 1

    preland_cmd_str = None
    preland_argv = None
    if key in cfg:
        raw_val = cfg[key]
        is_malformed = False
        if not isinstance(raw_val, str) or not raw_val.strip():
            is_malformed = True
        else:
            try:
                split_argv = shlex.split(raw_val)
            except ValueError:
                split_argv = []
            if not split_argv:
                is_malformed = True
            else:
                preland_cmd_str = raw_val
                preland_argv = split_argv

        if is_malformed:
            print("error: pre-land command refused")
            print(f"observed: config key '{key}' in {config.config_path()} is {raw_val!r}")
            print("Hopper did not run a pre-land command, did not merge, and did not push.")
            print(f"recover with: hop preland set {project} <command>")
            print(f"recover with: hop preland clear {project}")
            return 1

    rev_res = subprocess.run(
        ["git", "-C", checkout, "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
    )
    if rev_res.returncode != 0:
        print("error: landing refused")
        print(f"observed: git rev-parse failed in '{checkout}'")
        print("Hopper did not run the pre-land command, did not merge, and did not push.")
        return 1

    current_branch = rev_res.stdout.strip()
    if current_branch not in DEFAULT_BRANCH_NAMES:
        name = "detached HEAD" if current_branch == "HEAD" else current_branch
        print("error: landing refused")
        print(f"observed: {checkout} is on '{name}', not main or master")
        print("Hopper did not run the pre-land command, did not merge, and did not push.")
        return 1

    if preland_argv is not None:
        try:
            proc = subprocess.run(
                preland_argv,
                cwd=worktree,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            status = proc.returncode
            output = proc.stdout
        except (FileNotFoundError, OSError) as exc:
            status = 127
            output = str(exc)

        if status != 0:
            print("error: pre-land command failed")
            print(f"command: {preland_cmd_str}")
            print(f"worktree: {worktree}")
            print(f"status: {status}")
            print("output:")
            if output:
                print(output.rstrip("\n"))
            print("Hopper did not merge and did not push.")
            print(f"recover with: hop preland set {project} <command>")
            print(f"recover with: hop preland clear {project}")
            return status

    head_res = subprocess.run(
        ["git", "-C", checkout, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    if head_res.returncode != 0:
        print("error: landing refused")
        print(f"observed: git rev-parse HEAD failed in '{checkout}'")
        print("Hopper did not merge and did not push.")
        _print_git_output(head_res)
        return 1
    pre_merge_sha = head_res.stdout.strip()

    merge_res = subprocess.run(
        ["git", "-C", checkout, "merge", "--ff-only", branch],
        capture_output=True,
        text=True,
    )
    if merge_res.returncode != 0:
        print("error: merge failed")
        print(f"observed: git merge --ff-only {branch} failed")
        print("push: not-run")
        print("local-moved: no")
        print(f"pre-merge HEAD: {pre_merge_sha}")
        _print_git_output(merge_res)
        return 1

    remote_res = subprocess.run(
        ["git", "-C", checkout, "remote"],
        capture_output=True,
        text=True,
    )
    if remote_res.returncode != 0:
        print("error: remote query failed")
        print(f"observed: git remote failed in {checkout}")
        print("push: not-run")
        curr_res = subprocess.run(
            ["git", "-C", checkout, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
        )
        current_head = curr_res.stdout.strip() if curr_res.returncode == 0 else "unknown"
        local_moved = (
            "unknown"
            if current_head == "unknown"
            else "yes"
            if current_head != pre_merge_sha
            else "no"
        )
        print(f"local-moved: {local_moved}")
        print(f"pre-merge HEAD: {pre_merge_sha}")
        print(f"current HEAD: {current_head}")
        _print_git_output(remote_res)
        return 1

    remotes = {name.strip() for name in remote_res.stdout.splitlines() if name.strip()}
    curr_res = subprocess.run(
        ["git", "-C", checkout, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    if curr_res.returncode != 0 or not curr_res.stdout.strip():
        print("error: landing refused")
        print(f"observed: git rev-parse HEAD failed after merging in '{checkout}'")
        print("push: not-run")
        print("local-moved: unknown")
        print(f"pre-merge HEAD: {pre_merge_sha}")
        _print_git_output(curr_res)
        return 1
    new_head = curr_res.stdout.strip()

    if "origin" not in remotes:
        print(f"Checkout {checkout} fast-forwarded to {new_head}.")
        print("push: not-run")
        return 0

    push_res = subprocess.run(
        ["git", "-C", checkout, "push"],
        capture_output=True,
        text=True,
    )
    if push_res.returncode != 0:
        post_res = subprocess.run(
            ["git", "-C", checkout, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
        )
        current_head = post_res.stdout.strip() if post_res.returncode == 0 else "unknown"
        local_moved = (
            "unknown"
            if current_head == "unknown"
            else "yes"
            if current_head != pre_merge_sha
            else "no"
        )
        print("error: push failed")
        print(f"observed: git push failed in {checkout}")
        print("push: failed")
        print(f"local-moved: {local_moved}")
        print(f"pre-merge HEAD: {pre_merge_sha}")
        print(f"current HEAD: {current_head}")
        _print_git_output(push_res)
        return 1

    print(f"Checkout {checkout} fast-forwarded to {new_head}.")
    print("push: ok")
    return 0
