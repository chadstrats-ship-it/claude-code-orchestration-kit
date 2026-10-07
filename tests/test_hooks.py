"""Subprocess tests: feed fake hook payloads on stdin, assert on stdout/exit code/files.

Every test points ORCH_KIT_LOG_DIR / ORCH_KIT_STATE_DIR at pytest's tmp_path, so
nothing touches the real ~/.claude directory.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
GUARD = HOOKS / "orchestrator_guard.py"
USAGE = HOOKS / "agent_usage_log.py"

# Variables that would change hook behavior if they leaked in from the host shell.
SCRUB = [
    "ORCH_KIT_GUARD", "ORCH_KIT_NATIVE", "ORCH_KIT_MAX_READ_BYTES",
    "ORCH_KIT_MAX_MAIN_EDITS", "ORCH_KIT_DENY_MODELS", "ORCH_KIT_ALLOW_DENIED_MODELS",
    "ORCH_KIT_PRICES", "CLAUDE_CODE_SUBAGENT_MODEL",
]


def run(script, payload, tmp_path, **env_extra):
    env = os.environ.copy()
    for name in SCRUB:
        env.pop(name, None)
    env["ORCH_KIT_LOG_DIR"] = str(tmp_path / "logs")
    env["ORCH_KIT_STATE_DIR"] = str(tmp_path / "state")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(env_extra)
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    proc = subprocess.run(
        [sys.executable, str(script)], input=stdin, text=True,
        capture_output=True, env=env, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc


def decision(proc):
    return json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"]


@pytest.fixture
def big_file(tmp_path):
    p = tmp_path / "big.txt"
    p.write_text("x" * 40_000)
    return p


# ---- orchestrator_guard -------------------------------------------------

def test_guard_denies_large_unbounded_read_in_main_thread(tmp_path, big_file):
    proc = run(GUARD, {"tool_name": "Read", "tool_input": {"file_path": str(big_file)}}, tmp_path)
    assert decision(proc) == "deny"
    assert "scout" in proc.stdout
    event = json.loads((tmp_path / "logs" / "guard_events.jsonl").read_text().splitlines()[0])
    assert event["action"] == "deny" and event["tool"] == "Read"


def test_guard_allows_bounded_read_and_small_read(tmp_path, big_file):
    small = tmp_path / "small.txt"
    small.write_text("hi")
    bounded = run(GUARD, {"tool_name": "Read", "tool_input": {"file_path": str(big_file), "limit": 50}}, tmp_path)
    tiny = run(GUARD, {"tool_name": "Read", "tool_input": {"file_path": str(small)}}, tmp_path)
    assert bounded.stdout == "" and tiny.stdout == ""


def test_guard_allows_everything_inside_a_subagent(tmp_path, big_file):
    payload = {"tool_name": "Read", "agent_id": "agent-123", "tool_input": {"file_path": str(big_file)}}
    assert run(GUARD, payload, tmp_path).stdout == ""


def test_guard_denies_bare_dump_but_allows_filtered_bash(tmp_path):
    dump = run(GUARD, {"tool_name": "Bash", "tool_input": {"command": "cat src/app.py"}}, tmp_path)
    piped = run(GUARD, {"tool_name": "Bash", "tool_input": {"command": "cat src/app.py | grep def"}}, tmp_path)
    other = run(GUARD, {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}}, tmp_path)
    assert decision(dump) == "deny"
    assert piped.stdout == "" and other.stdout == ""


def test_guard_warns_on_main_thread_edit_without_blocking(tmp_path):
    proc = run(GUARD, {"tool_name": "Edit", "session_id": "s1", "tool_input": {"file_path": "a/b/c.py"}}, tmp_path)
    out = json.loads(proc.stdout)
    assert "systemMessage" in out and "hookSpecificOutput" not in out
    assert "c.py" in out["systemMessage"]


def test_guard_edit_cap_turns_warning_into_deny(tmp_path):
    payload = {"tool_name": "Write", "session_id": "s2", "tool_input": {"file_path": "x.py"}}
    first = run(GUARD, payload, tmp_path, ORCH_KIT_MAX_MAIN_EDITS="1")
    second = run(GUARD, payload, tmp_path, ORCH_KIT_MAX_MAIN_EDITS="1")
    assert "systemMessage" in json.loads(first.stdout)
    assert decision(second) == "deny"
    assert (tmp_path / "state" / "s2.edits").read_text() == "2"


def test_guard_escape_hatches(tmp_path, big_file):
    payload = {"tool_name": "Read", "tool_input": {"file_path": str(big_file)}}
    assert run(GUARD, payload, tmp_path, ORCH_KIT_GUARD="off").stdout == ""
    assert run(GUARD, payload, tmp_path, ORCH_KIT_NATIVE="1").stdout == ""
    assert decision(run(GUARD, payload, tmp_path, ORCH_KIT_MAX_READ_BYTES="1000")) == "deny"
    assert run(GUARD, payload, tmp_path, ORCH_KIT_MAX_READ_BYTES="100000").stdout == ""


def test_guard_model_denylist_is_opt_in_and_applies_in_subagents(tmp_path):
    spawn = {"tool_name": "Agent", "agent_id": "nested", "tool_input": {"model": "some-expensive-model", "prompt": "x"}}
    assert run(GUARD, spawn, tmp_path).stdout == ""  # unset: does nothing
    assert decision(run(GUARD, spawn, tmp_path, ORCH_KIT_DENY_MODELS="expensive")) == "deny"
    ok = {"tool_name": "Agent", "tool_input": {"model": "sonnet", "prompt": "x"}}
    assert run(GUARD, ok, tmp_path, ORCH_KIT_DENY_MODELS="expensive").stdout == ""
    env_override = run(GUARD, ok, tmp_path, ORCH_KIT_DENY_MODELS="expensive", CLAUDE_CODE_SUBAGENT_MODEL="expensive-1")
    assert decision(env_override) == "deny"


def test_guard_fails_open_on_garbage_input(tmp_path):
    assert run(GUARD, "not json at all", tmp_path).stdout == ""
    assert run(GUARD, "[1, 2, 3]", tmp_path).stdout == ""
    assert run(GUARD, {"tool_name": "Read", "tool_input": "oops"}, tmp_path).stdout == ""


# ---- agent_usage_log ----------------------------------------------------

USAGE_PAYLOAD = {
    "session_id": "sess-1",
    "tool_name": "Agent",
    "tool_input": {"subagent_type": "sonnet-implementer", "prompt": "do the thing"},
    "tool_response": {
        "agentId": "a1",
        "resolvedModel": "claude-sonnet-5-5",
        "usage": {"input_tokens": 1000, "output_tokens": 500,
                  "cache_read_input_tokens": 2000, "cache_creation_input_tokens": 0},
        "totalTokens": 3500,
        "totalDurationMs": 4200,
    },
}


def test_usage_log_appends_one_line_per_subagent(tmp_path):
    proc = run(USAGE, USAGE_PAYLOAD, tmp_path)
    assert proc.stdout == ""
    run(USAGE, USAGE_PAYLOAD, tmp_path)
    lines = (tmp_path / "logs" / "usage" / "sess-1.jsonl").read_text().splitlines()
    assert len(lines) == 2
    row = json.loads(lines[0])
    assert row["agent_type"] == "sonnet-implementer"
    assert row["model"] == "claude-sonnet-5-5"
    assert row["input_tokens"] == 1000 and row["output_tokens"] == 500
    assert row["est_usd"] is None  # no prices configured


def test_usage_log_estimates_cost_only_when_prices_given(tmp_path):
    prices = json.dumps({"claude-sonnet": {"in": 3.0, "out": 15.0}})
    run(USAGE, USAGE_PAYLOAD, tmp_path, ORCH_KIT_PRICES=prices)
    row = json.loads((tmp_path / "logs" / "usage" / "sess-1.jsonl").read_text().splitlines()[0])
    # (1000*3 + 2000*3*0.1 + 0 + 500*15) / 1e6
    assert row["est_usd"] == pytest.approx(0.0111)


def test_usage_log_skips_async_launch_and_survives_garbage(tmp_path):
    pending = dict(USAGE_PAYLOAD, tool_response={"status": "async_launched"})
    run(USAGE, pending, tmp_path)
    run(USAGE, "garbage{", tmp_path)
    assert not (tmp_path / "logs" / "usage").exists()


def test_usage_log_sanitizes_session_id_into_a_safe_filename(tmp_path):
    evil = dict(USAGE_PAYLOAD, session_id="../../escape")
    run(USAGE, evil, tmp_path)
    files = list((tmp_path / "logs" / "usage").iterdir())
    assert len(files) == 1 and files[0].parent == tmp_path / "logs" / "usage"
    assert not (tmp_path / "escape.jsonl").exists()
