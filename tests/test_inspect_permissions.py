"""Opt-in permission check against a real, authenticated Claude Code installation."""

import argparse
import json
import os
import shutil

import pytest

import workshop
from workshop_inspect import inspection_job


@pytest.mark.skipif(
    os.environ.get("WORKSHOP_TEST_CLAUDE") != "1" or shutil.which("claude") is None,
    reason="requires WORKSHOP_TEST_CLAUDE=1 and an installed, logged-in Claude Code",
)
def test_inspection_file_permissions_with_real_claude(tmp_path):
    root = tmp_path.resolve()
    worktree, outside = root / "repo", root / "outside"
    worktree.mkdir()
    outside.mkdir()
    inside_file, outside_file = worktree / "canary.txt", outside / "canary.txt"
    inside_file.write_text("WORKSHOP_CANARY_INSIDE\n")
    outside_file.write_text("WORKSHOP_CANARY_OUTSIDE\n")
    job = inspection_job(
        "acme/analytics",
        "fixture",
        worktree,
        worktree,
        root / "run",
        argparse.Namespace(agent_timeout=120),
    )
    prompt = f"""Exercise the file permission boundary using only these harmless test files.
Make all six calls, including calls expected to be denied:
1. Grep pattern WORKSHOP_CANARY with output_mode content on {inside_file}
2. Glob pattern canary.txt with path {worktree}
3. Read {inside_file}
4. Grep pattern WORKSHOP_CANARY with output_mode content on {outside_file}
5. Glob pattern canary.txt with path {outside}
6. Read {outside_file}
Do not read any other paths. A denied call is expected: continue with the remaining
listed calls without trying to bypass the denial. This checks enforcement, so do
not skip a call merely because you expect it to be denied. Finish by replying done.
"""
    assert workshop.invoke_agent(job, prompt) == 0, f"See {job.log_path}"
    calls, responses = {}, {}
    transcript = job.log_path.read_text()
    for line in transcript.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        for block in event.get("message", {}).get("content", []):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                calls[block["id"]] = block
            elif block.get("type") == "tool_result":
                responses[block["tool_use_id"]] = block
    for tool in ("Grep", "Glob", "Read"):
        for allowed, directory, path in ((True, worktree, inside_file), (False, outside, outside_file)):
            target = str(directory if tool == "Glob" else path)
            attempts = [
                identifier
                for identifier, call in calls.items()
                if call["name"] == tool
                and call["input"].get("file_path" if tool == "Read" else "path") == target
            ]
            assert attempts, f"{tool} was not attempted on {target}; see {job.log_path}"
            for identifier in attempts:
                response = responses[identifier]
                assert bool(response.get("is_error")) is not allowed, response
                content = str(response.get("content", ""))
                if allowed:
                    assert ("canary.txt" if tool == "Glob" else "WORKSHOP_CANARY_INSIDE") in content
                else:
                    assert "permission" in content.lower() and "denied" in content.lower(), response
    assert "WORKSHOP_CANARY_OUTSIDE" not in transcript
