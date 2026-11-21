# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Guards for the supply-chain and injection hardening of the workflows (#405).

* Every ``uses:`` that is not a local ``./`` action is pinned to a 40-hex commit SHA with a
  ``# vX.Y.Z`` comment naming the release the SHA belongs to (first-party ``actions/*`` included).
* The licence marker of ``release.yml`` is written and checked, and nothing else.
* ``steps.<id>.outputs.dependency-names`` (text from a Dependabot PR) never sits inside a
  ``run:`` block; it reaches the script through ``env:``.
* The ``.github/release-tools`` toolchain is excluded from the Dependabot auto-merge.

Limit: the pin test reads the text of the workflow files line by line; it cannot tell whether the
SHA really is the commit of the commented tag (that is checked once with ``gh api`` when pinning).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
WORKFLOWS = sorted((REPO / ".github" / "workflows").glob("*.yml"))
USES_RE = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<ref>\S+)\s*(?:#\s*(?P<comment>.*))?$")
PINNED_RE = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
VERSION_COMMENT_RE = re.compile(r"^v\d+(\.\d+){0,2}\b")
LICENCE_MARKER = "<!-- labelyard:licence-changes -->"
DEPENDENCY_NAMES_RE = re.compile(r"steps\.[\w-]+\.outputs\.dependency-names")


def _uses_lines(path: Path) -> list[tuple[int, str, str | None]]:
    found: list[tuple[int, str, str | None]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        match = USES_RE.match(line)
        if match and not match.group("ref").startswith("./"):
            found.append((number, match.group("ref"), match.group("comment")))
    return found


def test_workflow_files_are_found() -> None:
    assert WORKFLOWS, "no workflow files found; the glob is wrong"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_action_is_pinned_by_sha(path: Path) -> None:
    problems = []
    for number, ref, comment in _uses_lines(path):
        if not PINNED_RE.match(ref):
            problems.append(f"{path.name}:{number}: {ref} is not pinned to a 40-hex SHA")
        elif not comment or not VERSION_COMMENT_RE.match(comment.strip()):
            problems.append(f"{path.name}:{number}: {ref} lacks a '# vX.Y.Z' comment")
    assert not problems, "\n".join(problems)


def test_release_yml_licence_marker_is_unique() -> None:
    text = (REPO / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert text.count(LICENCE_MARKER) == 2, "the marker is written once and checked once"
    # The skip check looks for the marker only, not for the visible heading.
    assert "grep -q '^## Licence changes'" not in text


def _run_blocks(workflow: dict[str, Any]) -> list[tuple[str, str]]:
    blocks = []
    for job_id, job in (workflow.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            if "run" in step:
                blocks.append((f"{job_id}/{step.get('name', '?')}", step["run"]))
    return blocks


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_dependency_names_never_inline_in_run(path: Path) -> None:
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    offenders = [name for name, run in _run_blocks(workflow) if DEPENDENCY_NAMES_RE.search(run)]
    assert not offenders, f"{path.name}: dependency-names inline in run of {offenders}"


def test_dependency_names_reach_the_script_through_env() -> None:
    path = REPO / ".github" / "workflows" / "dependabot-auto-merge.yml"
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["auto-merge"]["steps"]
    major = next(s for s in steps if s.get("name") == "Comment for major updates")
    assert major["env"]["DEPENDENCY_NAMES"] == "${{ steps.meta.outputs.dependency-names }}"
    assert "${DEPENDENCY_NAMES}" in major["run"]


def test_release_tools_are_excluded_from_auto_merge() -> None:
    path = REPO / ".github" / "workflows" / "dependabot-auto-merge.yml"
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["auto-merge"]["steps"]
    for name in ("Approve patch + minor updates", "Enable auto-merge for patch + minor updates"):
        step = next(s for s in steps if s.get("name") == name)
        assert "/.github/release-tools" in step["if"], name
