#!/usr/bin/env python3
"""
PostToolUse hook (matcher: Agent): SUBAGENT USAGE LEDGER.

Part of claude-code-orchestration-kit (MIT). Author: Trevor Moore.

Why this exists
---------------
The quiet failure mode of an orchestrator setup is that the orchestrator stops
delegating (or a subagent resolves to a different model than you intended) and
nothing tells you. This hook records one line per finished subagent so you can
check what actually ran.

What it reads
-------------
The PostToolUse payload for the `Agent` tool carries, in `tool_response`:
    resolvedModel   the model the subagent actually ran on
    usage           {input_tokens, output_tokens,
                     cache_creation_input_tokens, cache_read_input_tokens}
    totalTokens, totalDurationMs, agentId
and in `tool_input`: `subagent_type`. (Field availability depends on your
Claude Code version; missing fields are recorded as null.)

What it writes
--------------
One JSON line per subagent, appended to
    <ORCH_KIT_LOG_DIR>/usage/<session_id>.jsonl
(default ORCH_KIT_LOG_DIR: ~/.claude/orchestration-kit/logs).

Fields: ts, agent_id, agent_type, model, input_tokens, output_tokens,
cache_read_input_tokens, cache_creation_input_tokens, total_tokens,
duration_ms, est_usd.

est_usd is null unless you provide prices. Set ORCH_KIT_PRICES to a JSON object
mapping a model-name prefix to USD per million tokens, e.g.
    {"claude-sonnet": {"in": 3.0, "out": 15.0}}
Cache reads are counted at 0.1x the input price and cache writes at 1.25x;
those multipliers are rough assumptions, so treat est_usd as an estimate. This
kit ships no price table: prices change and you should supply your own.

Behavior
--------
Payloads with no `usage` (e.g. a background spawn that has not finished) are
skipped. The hook never blocks and never prints; any error is swallowed and the
exit code is always 0.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


def log_dir() -> Path:
    env = os.environ.get("ORCH_KIT_LOG_DIR")
    if env:
        return Path(os.path.expanduser(env))
    return Path(os.path.expanduser("~")) / ".claude" / "orchestration-kit" / "logs"


def safe_id(value) -> str:
    s = re.sub(r"[^A-Za-z0-9_.-]", "_", str(value or "unknown"))
    return s[:120] or "unknown"


def load_prices() -> dict:
    raw = os.environ.get("ORCH_KIT_PRICES", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def estimate_usd(model: str, inp: int, out: int, cache_read: int, cache_create: int):
    for prefix, p in load_prices().items():
        if isinstance(p, dict) and str(model).startswith(prefix):
            pin, pout = float(p.get("in", 0)), float(p.get("out", 0))
            usd = (
                inp * pin
                + cache_read * pin * 0.1
                + cache_create * pin * 1.25
                + out * pout
            ) / 1_000_000
            return round(usd, 6)
    return None


def main():
    payload = json.load(sys.stdin)
    if not isinstance(payload, dict):
        return

    resp = payload.get("tool_response") or {}
    if not isinstance(resp, dict):
        return
    usage = resp.get("usage") or {}
    if not isinstance(usage, dict) or not usage:
        return

    model = resp.get("resolvedModel") or "unknown"
    inp = int(usage.get("input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_create = int(usage.get("cache_creation_input_tokens") or 0)

    tin = payload.get("tool_input")
    row = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "agent_id": resp.get("agentId"),
        "agent_type": tin.get("subagent_type") if isinstance(tin, dict) else None,
        "model": model,
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_create,
        "total_tokens": resp.get("totalTokens"),
        "duration_ms": resp.get("totalDurationMs"),
        "est_usd": estimate_usd(model, inp, out, cache_read, cache_create),
    }

    d = log_dir() / "usage"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / f"{safe_id(payload.get('session_id'))}.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)  # PostToolUse must never interfere with the tool result
