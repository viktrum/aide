---
description: AIDE — status, feedback, or inspect/re-run the last prompt optimization
---

Behavior depends on the argument:

## `/aide status` (or `/aide doctor`)

Find the AIDE checkout: read `~/.claude/settings.json`, locate the `UserPromptSubmit` hook command containing `judge/prompt_judge.py`, and take its directory. Run `python3 <that-dir>/doctor.py` and show the user its output verbatim. If no AIDE hook is registered, say so and point to the install instructions in the repo README.

## `/aide feedback <text>`

The user is reporting how an AIDE intervention behaved (wrong fire, useful fire, annoying, etc.). Append one JSON line to `~/.claude-judge/feedback.jsonl`:

```json
{"ts": <unix epoch>, "text": "<their words verbatim>", "session_id": "<current session if known>"}
```

Use an append (`>>`), never rewrite the file. Then read the last 3 lines of `~/.claude-judge/telemetry.jsonl` and include them in a second JSON line tagged `"context"`. Confirm in one line that it was recorded.

## `/aide` or `/aide show`

Read `~/.claude-judge/pending_transform.md` if it exists and is not expired.

- If the file contains an `<!-- expires:TIMESTAMP -->` header and TIMESTAMP is in the past, tell the user the optimization expired and delete the file.
- If the file is missing, tell the user: "No recent AIDE optimization. AIDE saves one whenever it optimises a flagged prompt (you'll have seen a 'prompt optimised' line)."
- Otherwise display the saved optimized prompt verbatim in a code block so the user can inspect what the agent was directed to act on. Add nothing beyond one lead-in line.

## `/aide run`

Submit the saved optimized prompt from `pending_transform.md` as the user's actual request and execute it.

Do not modify saved text in any mode. Optimizations are applied automatically at submit time — `show`/`run` exist for inspection and for re-running after a `*`-bypassed or expired turn.
