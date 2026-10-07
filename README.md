# claude-code-orchestration-kit

v0.1.0 - a small, copyable template for running Claude Code as an orchestrator that delegates to cheaper, fresh-context subagents. Agent definitions, a CLAUDE.md snippet, and two stdlib-only Python hooks (a guard and a usage ledger) with tests.

![Routing ladder](docs/routing-ladder.svg)

## The problem

In a long Claude Code session, everything the main thread reads is re-sent on every later turn. If the main session also reads big files, dumps logs, and writes most of the code itself, its context fills with material it only needed once, and nobody independent checks the result.

This kit sets up a division of labor:

- The main session (Opus, in the author's setup) plans, writes self-contained briefs, judges verdicts, and integrates.
- Sonnet subagents implement and verify, each in a fresh context that is discarded when it finishes. The verifier cannot edit source, so the code's author never grades it.
- A Haiku `scout` does read-only sweeps and returns a short brief instead of file contents.
- After a verifier FAIL, retry once with a fresh spawn. After a second FAIL, escalate to an Opus agent. If that fails too, the orchestrator does it itself.

Two hooks back the convention with something mechanical: one denies/nudges the main thread when it does bulk reads or bulk edits, the other records what each subagent actually ran on. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full routing rules, brief format and parallel/sequential rules.

This is a convention plus enforcement of its easiest parts, not a framework. The retry/escalation order is instructions to the model; the hooks do not count failures. I make no claim about cost or quality gains: use the ledger on your own sessions.

## Contents

```
agents/
  sonnet-implementer.md   executes a brief, runs the verify command
  sonnet-verifier.md      runs checks, returns VERDICT: PASS/FAIL, cannot edit source
  scout.md                read-only sweeps (Read, Grep, Glob only), returns a compressed brief
  opus-escalation.md      root-cause tier after two verifier failures
  CLAUDE.snippet.md       example orchestration section for your CLAUDE.md (not an agent)
hooks/
  orchestrator_guard.py   PreToolUse: read/bash/edit discipline for the main thread
  agent_usage_log.py      PostToolUse (Agent): one JSON line per finished subagent
examples/
  settings.snippet.json   how to register the hooks in ~/.claude/settings.json
tests/
  test_hooks.py           pytest, drives the hooks via subprocess with fake payloads
docs/
  ARCHITECTURE.md         routing ladder, brief format, hook details
  routing-ladder.svg
```

## Install

Requires Python 3 on PATH as `python` (stdlib only; tested on 3.14). pytest is needed only to run the tests.

1. Copy the agents (not `CLAUDE.snippet.md`):

   ```
   cp agents/sonnet-*.md agents/scout.md agents/opus-escalation.md ~/.claude/agents/
   ```

2. Copy the hooks:

   ```
   mkdir -p ~/.claude/hooks
   cp hooks/orchestrator_guard.py hooks/agent_usage_log.py ~/.claude/hooks/
   ```

3. Merge `examples/settings.snippet.json` into `~/.claude/settings.json` (merge the `hooks` entries into any you already have; do not overwrite the file). If your shell does not expand `~` in hook commands (for example on Windows with cmd or PowerShell), replace it with the absolute path.

4. Paste the contents of `agents/CLAUDE.snippet.md` into your `~/.claude/CLAUDE.md` and adjust it.

5. Edit the `model:` lines in the agent files to model IDs your account can use. They are the author's values and are kept as-is.

Usage after that is the normal Claude Code flow: ask the main session for work, and it delegates per the snippet. To check what ran, read the ledger:

```
cat ~/.claude/orchestration-kit/logs/usage/<session_id>.jsonl
```

Hook behavior depends on the payload your Claude Code version sends (notably `agent_id` in PreToolUse and `resolvedModel` / `usage` in the Agent PostToolUse response). Try the hooks on a throwaway session first.

## Configuration

All paths and thresholds are environment variables. None are required.

| Variable | Used by | Default | Effect |
|----------|---------|---------|--------|
| `ORCH_KIT_LOG_DIR` | both | `~/.claude/orchestration-kit/logs` | Where `usage/<session>.jsonl` and `guard_events.jsonl` are written |
| `ORCH_KIT_STATE_DIR` | guard | `~/.claude/orchestration-kit/state` | Where the per-session main-thread edit counter is kept |
| `ORCH_KIT_MAX_READ_BYTES` | guard | `25000` | Main-thread `Read` of a larger file without `limit` is denied |
| `ORCH_KIT_MAX_MAIN_EDITS` | guard | `0` (warn only) | If a positive integer, main-thread `Edit`/`Write` number N+1 in a session is denied instead of warned |
| `ORCH_KIT_GUARD` | guard | unset | `off` disables the Read/Bash/Edit rules |
| `ORCH_KIT_NATIVE` | guard | unset | `1` disables the Read/Bash/Edit rules (orchestrator deliberately works end-to-end) |
| `ORCH_KIT_DENY_MODELS` | guard | unset (check off) | Case-insensitive regex. Agent/Task/Workflow spawns requesting a matching `model`, or running while `CLAUDE_CODE_SUBAGENT_MODEL` matches, are denied, including from inside subagents |
| `ORCH_KIT_ALLOW_DENIED_MODELS` | guard | unset | `1` bypasses the model denylist |
| `ORCH_KIT_PRICES` | usage log | unset | JSON mapping model-name prefix to USD per million tokens, e.g. `{"claude-sonnet": {"in": 3.0, "out": 15.0}}`. Without it, `est_usd` is `null` |

`~` is expanded in the two directory variables.

## What the hooks do

`orchestrator_guard.py` (PreToolUse). When the payload has no `agent_id` (main thread):

- `Read` of a file over the size cap with no `limit`: denied, pointing at `scout` or a bounded read.
- `Bash` whose command starts with `cat`, `head`, `tail`, `type`, `less`, `more`, `bat`, `gc` or `Get-Content` and contains no pipe: denied.
- `Edit` / `Write`: allowed with a visible warning to delegate (denied after `ORCH_KIT_MAX_MAIN_EDITS`, if you set it).

Calls from subagents (payload has `agent_id`) are never restricted by these rules. Each deny/warn is appended to `guard_events.jsonl`. Malformed input or any unexpected error exits 0 silently (fail-open). The Bash check is a prefix heuristic and will not catch every way of dumping a file.

`agent_usage_log.py` (PostToolUse, matcher `Agent`). Appends one line per finished subagent to `usage/<session_id>.jsonl`: timestamp, agent id, `subagent_type`, `resolvedModel`, token counts, duration, `est_usd`. Responses with no `usage` (a background spawn that has not finished) are skipped. It prints nothing and always exits 0.

## Results from actually running it

Both outputs below are real, produced on the author's machine (Windows, Python 3.14, pytest 9.1.1). Directory paths are abbreviated to `$D`, a fresh temp directory exported as `ORCH_KIT_LOG_DIR=$D/logs` and `ORCH_KIT_STATE_DIR=$D/state`.

### (a) Test suite

```
$ python -m pytest -q
.............                                                            [100%]
13 passed in 3.12s
```

The tests run each hook as a subprocess with a fake JSON payload on stdin and temp directories, and cover: large/bounded/small reads, subagent bypass, Bash dump vs piped command, edit warning, edit cap, escape hatches, opt-in model denylist, fail-open on garbage input, usage line contents, cost estimate only with prices, async payload skipping, and session-id sanitizing.

### (b) A hook invoked by hand

Usage ledger, fed a fake finished-subagent payload:

```
$ echo '{"session_id":"demo-session","tool_name":"Agent","tool_input":{"subagent_type":"sonnet-implementer","prompt":"add a retry to fetch()"},"tool_response":{"agentId":"agent-42","resolvedModel":"claude-sonnet-5-5","usage":{"input_tokens":12000,"output_tokens":1800,"cache_read_input_tokens":30000,"cache_creation_input_tokens":0},"totalTokens":43800,"totalDurationMs":21500}}' | python hooks/agent_usage_log.py
$ echo $?
0
$ cat $D/logs/usage/demo-session.jsonl
{"ts": "2026-10-07T12:53:27+00:00", "agent_id": "agent-42", "agent_type": "sonnet-implementer", "model": "claude-sonnet-5-5", "input_tokens": 12000, "output_tokens": 1800, "cache_read_input_tokens": 30000, "cache_creation_input_tokens": 0, "total_tokens": 43800, "duration_ms": 21500, "est_usd": null}
```

`est_usd` is `null` because no `ORCH_KIT_PRICES` was set.

Guard, a 40,000-byte file read from the main thread (stdout is the hook decision Claude Code receives):

```
$ echo '{"session_id":"demo-session","tool_name":"Read","tool_input":{"file_path":"$D/big.txt"}}' | python hooks/orchestrator_guard.py
{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "ORCHESTRATOR GUARD: 'big.txt' is 40,000 bytes (limit 25,000). Anything read in the main session is re-sent on every later turn.\n\nDo this instead:\n  - Delegate to the `scout` agent (it returns a compressed brief), OR\n  - Re-issue the Read with an explicit `limit` for a bounded slice.\n\nTo read it whole anyway, set ORCH_KIT_GUARD=off."}}
```

Main-thread `Edit` (allowed, with a nudge) and the same large `Read` with `agent_id` set (no output, allowed):

```
$ echo '{"session_id":"demo-session","tool_name":"Edit","tool_input":{"file_path":"src/app.py"}}' | python hooks/orchestrator_guard.py
{"systemMessage": "ORCHESTRATOR GUARD: main-thread Edit on 'app.py'. The orchestrator delegates code changes to `sonnet-implementer`. A genuine one-line fix is fine; if this is more than that, stop and delegate. (ORCH_KIT_NATIVE=1 silences this.)"}
$ echo '{"session_id":"demo-session","agent_id":"a1","tool_name":"Read","tool_input":{"file_path":"$D/big.txt"}}' | python hooks/orchestrator_guard.py
$ echo $?
0
```

(In the commands above, `$D` inside the single-quoted JSON stands for the real temp path used in the run.)

## Running the tests

```
python -m pip install pytest
python -m pytest -q
```

## License

MIT. Copyright 2026 Trevor Moore. Contact: mtrevor380@gmail.com
