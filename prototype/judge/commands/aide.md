---
description: AIDE — status, feedback, or inspect/re-run the last prompt optimization
---

Behavior depends on the argument:

## `/aide status` (or `/aide doctor`)

Find the AIDE checkout: read `~/.claude/settings.json`, locate the `UserPromptSubmit` hook command containing `judge/prompt_judge.py`, and take its directory. Run `python3 <that-dir>/doctor.py` and show the user its output verbatim. If no AIDE hook is registered, say so and point to the install instructions in the repo README.

## `/aide scope` or `/aide scope <prompt|full>`

With no argument: read `~/.claude-judge/config.json` (missing file or missing key means `prompt`) and tell the user the current scope in one line: `prompt` = coaching on prompts only; `full` = adds tool gates and stop verification.

With an argument (`prompt` or `full`): read the existing config JSON if present, set its `scope` key to the given value, and Write the merged object back to `~/.claude-judge/config.json`. Confirm in one line. Reject any other value.

## `/aide feedback <text>`

The user is reporting how an AIDE intervention behaved (wrong fire, useful fire, annoying, etc.). Use the Write tool to create `~/.claude-judge/feedback/<unix-epoch>.json` (new file, never overwrite an existing one):

```json
{"ts": <unix epoch>, "text": "<their words verbatim>", "recent_telemetry": [<last 3 lines of ~/.claude-judge/telemetry.jsonl, as objects>]}
```

Confirm in one line that it was recorded.

## `/aide` or `/aide show`

Use the Read tool on `~/.claude-judge/pending_transform.md` if it exists and is not expired.

- If the file contains an `<!-- expires:TIMESTAMP -->` header and TIMESTAMP is in the past, tell the user the optimization expired. Do not delete the file — the judge overwrites it on the next transform.
- If the file is missing, tell the user: "No recent AIDE optimization. AIDE saves one whenever it optimises a flagged prompt (you'll have seen a 'prompt optimised' line)."
- Otherwise display the saved optimized prompt verbatim in a code block so the user can inspect what the agent was directed to act on. Add nothing beyond one lead-in line.

## `/aide run`

Submit the saved optimized prompt from `pending_transform.md` as the user's actual request and execute it.

Do not modify saved text in any mode. Optimizations are applied automatically at submit time — `show`/`run` exist for inspection and for re-running after a `*`-bypassed or expired turn.
