#!/usr/bin/env python3
"""
PreToolUse hook: ORCHESTRATOR GUARD.

Part of claude-code-orchestration-kit (MIT). Author: Trevor Moore.

Why this exists
---------------
In an orchestrator/worker setup the main session (the "orchestrator") is
supposed to plan, write briefs and judge results. Bulk reading and bulk editing
belong in subagents, whose context is discarded when they finish. Anything the
main session reads is re-sent on every later turn, so large reads there are the
expensive failure mode. CLAUDE.md can ask for delegation; this hook enforces
the parts that can be enforced mechanically.

How it tells main thread from subagent
--------------------------------------
Claude Code puts `agent_id` in the PreToolUse payload only when the tool call
originates inside a subagent. So:

    agent_id present -> subagent    -> allowed (this is where the work belongs)
    agent_id absent  -> main thread -> the rules below apply

What it does (main thread only)
-------------------------------
  * Read of a file larger than ORCH_KIT_MAX_READ_BYTES (default 25000) without
    an explicit `limit` -> DENY, with a pointer to the scout agent.
  * Bash command that starts with a file dumper (cat/head/tail/type/less/more/
    bat/Get-Content/gc) and contains no pipe -> DENY.
  * Edit / Write -> WARN (a `systemMessage`, the call still proceeds). If
    ORCH_KIT_MAX_MAIN_EDITS is a positive integer, the Nth+1 main-thread
    Edit/Write in one session is DENIED instead. The counter is a small file
    in ORCH_KIT_STATE_DIR.

What it does (everywhere, including inside subagents; opt-in)
-------------------------------------------------------------
  * If ORCH_KIT_DENY_MODELS is set (a regex, case-insensitive), any Agent /
    Task / Workflow spawn whose requested `model` matches it is DENIED, as is
    any spawn while CLAUDE_CODE_SUBAGENT_MODEL matches it (that variable
    overrides every agent's frontmatter). Workflow scripts are scanned for
    `model: '...'` / `model = '...'` assignments. Unset by default, so this
    check does nothing unless you configure it. Set ORCH_KIT_ALLOW_DENIED_MODELS=1
    to bypass it for one session.

This check runs before the escape hatches and before the subagent
short-circuit, because a nested spawn is how a disallowed model sneaks back in.

Escape hatches (they disable the read/edit rules only, not the model check)
---------------------------------------------------------------------------
  ORCH_KIT_GUARD=off   disable the read/bash/edit rules
  ORCH_KIT_NATIVE=1    same; for sessions where the orchestrator is deliberately
                       doing the work end-to-end itself

Recording
---------
Every deny/warn is appended as one JSON line to
<ORCH_KIT_LOG_DIR>/guard_events.jsonl. Logging failures are ignored.

Fail-open
---------
Malformed input or any unexpected exception exits 0 with no output, so a bug in
this hook can never block Claude Code. (Intentional denials also exit 0; the
decision is carried in the JSON on stdout.)
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_MAX_BYTES = 25_000
SPAWN_TOOLS = ("Agent", "Task", "Workflow")

# Commands whose whole purpose is to spray a file into the transcript.
DUMPERS = re.compile(
    r"^\s*(?:sudo\s+)?(cat|bat|head|tail|more|less|type|gc|Get-Content)\b",
    re.IGNORECASE,
)

# `model: 'x'` / `model = "x"` assignments inside a Workflow script.
WORKFLOW_MODEL = re.compile(r"""model\s*[:=]\s*['"`]?([\w.\[\]-]+)""", re.IGNORECASE)


def _base_dir() -> Path:
    return Path(os.path.expanduser("~")) / ".claude" / "orchestration-kit"


def log_dir() -> Path:
    env = os.environ.get("ORCH_KIT_LOG_DIR")
    return Path(os.path.expanduser(env)) if env else _base_dir() / "logs"


def state_dir() -> Path:
    env = os.environ.get("ORCH_KIT_STATE_DIR")
    return Path(os.path.expanduser(env)) if env else _base_dir() / "state"


def safe_id(value) -> str:
    """Session ids become file names; keep them to a safe character set."""
    s = re.sub(r"[^A-Za-z0-9_.-]", "_", str(value or "unknown"))
    return s[:120] or "unknown"


def record(payload: dict, action: str, detail: str) -> None:
    try:
        d = log_dir()
        d.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "session_id": payload.get("session_id"),
            "tool": payload.get("tool_name"),
            "action": action,
            "detail": detail[:300],
        }
        with open(d / "guard_events.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception:
        pass


def allow():
    """Emit nothing and exit 0: the tool call proceeds untouched."""
    sys.exit(0)


def deny(reason: str, payload: dict):
    record(payload, "deny", reason)
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
    )
    sys.exit(0)


def warn(message: str, payload: dict):
    """Show the user a message without blocking.

    `systemMessage` is a top-level hook output field. With no
    `permissionDecision`, the normal permission flow is untouched.
    """
    record(payload, "warn", message)
    json.dump({"systemMessage": message}, sys.stdout)
    sys.exit(0)


def deny_models_pattern():
    raw = os.environ.get("ORCH_KIT_DENY_MODELS", "").strip()
    if not raw:
        return None
    try:
        return re.compile(raw, re.IGNORECASE)
    except re.error:
        return None  # a bad pattern disables the check rather than breaking the hook


def check_denied_model(tool: str, tin: dict, payload: dict):
    """Deny spawns that would put a subagent on a model you ruled out."""
    pat = deny_models_pattern()
    if pat is None or os.environ.get("ORCH_KIT_ALLOW_DENIED_MODELS") == "1":
        return

    env_val = os.environ.get("CLAUDE_CODE_SUBAGENT_MODEL", "")
    if env_val and pat.search(env_val):
        deny(
            f"DENIED SUBAGENT MODEL: CLAUDE_CODE_SUBAGENT_MODEL={env_val} matches "
            f"ORCH_KIT_DENY_MODELS. That variable overrides every agent's frontmatter, "
            f"so it would reroute every subagent. Unset it or set an allowed model.",
            payload,
        )

    requested = str(tin.get("model") or "")
    if requested and pat.search(requested):
        deny(
            f"DENIED SUBAGENT MODEL: this {tool} spawn requests model='{requested}', "
            f"which matches ORCH_KIT_DENY_MODELS. Re-spawn with an allowed model, or "
            f"omit `model` to use the agent's frontmatter.",
            payload,
        )

    if tool == "Workflow":
        for m in WORKFLOW_MODEL.finditer(str(tin.get("script") or "")):
            if pat.search(m.group(1)):
                deny(
                    f"DENIED SUBAGENT MODEL: this Workflow script routes an agent onto "
                    f"'{m.group(1)}', which matches ORCH_KIT_DENY_MODELS.",
                    payload,
                )


def count_main_edit(session_id) -> int:
    """Increment and return this session's main-thread Edit/Write count (0 on error)."""
    try:
        d = state_dir()
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{safe_id(session_id)}.edits"
        try:
            n = int(f.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            n = 0
        n += 1
        f.write_text(str(n), encoding="utf-8")
        return n
    except Exception:
        return 0


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        allow()
    if not isinstance(payload, dict):
        allow()

    tool = payload.get("tool_name", "")
    tin = payload.get("tool_input") or {}
    if not isinstance(tin, dict):
        allow()

    # Model routing first: ahead of the escape hatches and the subagent short-circuit.
    if tool in SPAWN_TOOLS:
        check_denied_model(tool, tin, payload)

    if os.environ.get("ORCH_KIT_GUARD", "").lower() == "off":
        allow()
    if os.environ.get("ORCH_KIT_NATIVE", "") == "1":
        allow()

    # Subagents are where bulk work is supposed to happen: unrestricted.
    if payload.get("agent_id"):
        allow()

    try:
        max_bytes = int(os.environ.get("ORCH_KIT_MAX_READ_BYTES", DEFAULT_MAX_BYTES))
    except ValueError:
        max_bytes = DEFAULT_MAX_BYTES

    if tool == "Read":
        path = tin.get("file_path") or ""
        if tin.get("limit"):  # a bounded read is the disciplined form
            allow()
        try:
            size = os.path.getsize(path)
        except (OSError, TypeError, ValueError):
            allow()  # missing file: not a context risk
        if size > max_bytes:
            deny(
                f"ORCHESTRATOR GUARD: '{os.path.basename(path)}' is {size:,} bytes "
                f"(limit {max_bytes:,}). Anything read in the main session is re-sent on "
                f"every later turn.\n\n"
                f"Do this instead:\n"
                f"  - Delegate to the `scout` agent (it returns a compressed brief), OR\n"
                f"  - Re-issue the Read with an explicit `limit` for a bounded slice.\n\n"
                f"To read it whole anyway, set ORCH_KIT_GUARD=off.",
                payload,
            )
        allow()

    if tool == "Bash":
        cmd = str(tin.get("command") or "").strip()
        # `cat x | grep y` is a filtered, small result; a bare dump is not.
        if DUMPERS.match(cmd) and "|" not in cmd:
            deny(
                f"ORCHESTRATOR GUARD: this Bash command dumps a file straight into the "
                f"orchestrator's context.\n\n  {cmd[:160]}\n\n"
                f"Delegate the read to the `scout` agent, use the Read tool with a "
                f"`limit`, or pipe through a filter (grep/head) so the output is bounded.",
                payload,
            )
        allow()

    if tool in ("Edit", "Write"):
        name = os.path.basename(str(tin.get("file_path") or "")) or "<unknown>"
        try:
            max_edits = int(os.environ.get("ORCH_KIT_MAX_MAIN_EDITS", "0"))
        except ValueError:
            max_edits = 0
        n = count_main_edit(payload.get("session_id"))
        if max_edits > 0 and n > max_edits:
            deny(
                f"ORCHESTRATOR GUARD: main-thread {tool} on '{name}' is edit #{n} this "
                f"session (limit {max_edits}, ORCH_KIT_MAX_MAIN_EDITS). The orchestrator "
                f"does not write bulk code: delegate to `sonnet-implementer` with a brief.",
                payload,
            )
        warn(
            f"ORCHESTRATOR GUARD: main-thread {tool} on '{name}'. The orchestrator "
            f"delegates code changes to `sonnet-implementer`. A genuine one-line fix is "
            f"fine; if this is more than that, stop and delegate. "
            f"(ORCH_KIT_NATIVE=1 silences this.)",
            payload,
        )

    allow()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)  # fail open
