# AIDE Real-time Judge — Prototype (V1 + V2)

Workable prototype of the AIDE real-time judge. Everything runs locally; the only LLM access is through your own Claude Code CLI (`claude -p --model haiku`) — no external APIs, no new keys, nothing leaves your machine.

**V1:** `UserPromptSubmit` — `notice`, `inject`, `transform`, `block`  
**V2:** `PreToolUse`, `PostToolUse`/`PostToolUseFailure`, `SessionStart`, `PreCompact`, `Stop`, `/aide` command

## The transform channel ("prompt optimised")

The core UX: you press enter, your prompt goes through, and when AIDE flags a
known bad pattern the agent is directed to act on an **optimized rewrite** of
your prompt instead. You see exactly one extra line:

```
✦ prompt optimised — raw error dump structured into a bug report (R21). Prefix with * to bypass AIDE.
```

Mechanics (hooks cannot literally replace a typed prompt — there is no
`updatedPrompt` field on `UserPromptSubmit`): the judge emits an authoritative
`additionalContext` packet containing `<optimized_prompt>` and a directive
that the optimized version wins where it differs from the raw prompt, plus a
one-line `systemMessage`. Nothing blocks; nothing needs resubmitting.

| Rule | Rewrite |
|------|---------|
| R2 | Repeat of a failed prompt → diagnose-first retry packet |
| R21 | Raw error dump → structured `<error_report>` bug report |
| R6 | Bundled asks → ordered task plan with per-task verification |
| R13 | Stacked questions → numbered answer checklist |
| R5 | Vague opener → scoped request with explicit unknowns |

Structure is always deterministic (templates + session signals, <150ms).
Wording can optionally be polished by Haiku for R5/R6 (`optimizer.llm`).
The rewrite is also saved to `pending_transform.md` — `/aide` shows it,
`/aide run` re-runs it.

**Optimizer config** (`rulebook.json` → `"optimizer": {...}`, env `AIDE_OPTIMIZER_LLM` overrides):

| Key | Default | Meaning |
|-----|---------|---------|
| `llm` | `auto` | `auto` = API polish when `ANTHROPIC_API_KEY` is set, else deterministic; `cli` = Haiku via your claude CLI (adds seconds on flagged prompts); `off` = never |
| `model` | haiku | Model for the lexical polish |
| `timeout_s` | `6` | Hard LLM budget; on timeout the deterministic skeleton ships |

LLM rewrites are validated (file paths from the original must survive, no
runaway length) and fall back to the deterministic skeleton on any failure.
The spawned CLI session sets `AIDE_JUDGE_BYPASS=1` so AIDE's own hooks exit
immediately — no recursion.


## File map

```
judge/
  prompt_judge.py      UserPromptSubmit — tier-1 rules R1–R22 + rulebook + fatigue budget
  optimizer.py         Transform channel — optimized-prompt builders + Haiku polish
  session_state.py     Shared per-session counters (web chain, bash retries)
  posttool_record.py   PostToolUse + PostToolUseFailure recorder
  pretool_gate.py      PreToolUse deny gate (web chain, retry spiral, destructive-under-auto)
  session_start.py     SessionStart resume recap (R11 dedup)
  precompact.py        PreCompact memory snapshot writer
  stop_verify.py       Stop hook one-shot verification nudge
  commands/aide.md     /aide — inspect or re-run the last prompt optimization
  test_prompt_judge.py V1 + V2 unit tests
  test_optimizer.py    Transform builder + rewrite-validation tests
  test_hooks_v2.py     V2 hook test entry point

miner/
  mine.py              Offline scan → findings, rulebook, escalation ladders
  rulebook_compiler.py Compiles cross-session findings → rulebook v2 + escalation_meta
  web_export.py        dashboard_data.json (includes escalation)
  ...
```

Runtime data: `~/.claude-judge/`

```
~/.claude-judge/
  runs/2026-07-08_235329/   # timestamped mine archive (never overwritten)
  latest.json               # pointer to newest run
  latest → runs/...         # symlink (when supported)
  rulebook.json             # published copy for live judge hooks
  telemetry.jsonl           # live hook telemetry (append-only)
  session_marks.json        # per-session judge state
  session_state/            # V2 hook counters
  compact-memory/           # PreCompact snapshots
  pending_transform.md      # /aide resubmit buffer
```

List archives: `python3 miner/mine.py --list-runs`
Label a specific run: `python3 miner/label.py --run-id 2026-07-08_235329`

## Install

### 1. Mine your history

```bash
cd prototype
python3 miner/mine.py              # default: 2000 sessions; --since 30 for last month
python3 miner/label.py             # --dry-run to inspect first
```

Live progress prints to **stderr** (stage banners + `[n/total]` session counters). Use `--quiet` to suppress. The final baseline summary still prints to stdout.

**Label backends** (when Claude quota is exhausted):

```bash
# Gemini via agy: agy --model <slug> -p "prompt"
JUDGE_LABEL_BACKEND=agy JUDGE_LABEL_MODEL=gemini-3.1-pro-high python3 miner/label.py

# GPT via Codex CLI (uses `codex exec`, not `codex -p`)
JUDGE_LABEL_BACKEND=codex JUDGE_LABEL_MODEL=gpt-5.5-codex python3 miner/label.py

# Or flags:
python3 miner/label.py --backend agy --model gemini-3.1-pro-high
python3 miner/label.py --backend codex --model gpt-5.5-codex
```

Run `agy models` / `codex --help` for exact model slugs on your install. Validation still rejects hallucinated patterns regardless of backend.

**Label batching:** segments are sent to the LLM in batches of 10 (configurable via `JUDGE_LABEL_BATCH_SIZE`). Progress prints to stderr; patterns found in different batches are merged automatically.

**3-model quality compare** (one-time, all via agy):

```bash
cd prototype
python3 miner/label_compare.py
# outputs: ~/.claude-judge/runs/<latest>/label_compare/
#   gemini-3-1-pro-high/
#   claude-sonnet-4-6-thinking/
#   claude-opus-4-6-thinking/
#   compare_manifest.json
```

### 2. Register all hooks

**One command** (merges into existing `~/.claude/settings.json`, backs up first):

```bash
python3 prototype/judge/install_hooks.py
```

Manual install — replace `/ABSOLUTE/PATH/TO/prototype` with your checkout path:

**`~/.claude/settings.json`** (global) or project `.claude/settings.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 /ABSOLUTE/PATH/TO/prototype/judge/prompt_judge.py",
            "timeout": 15
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "Bash|WebFetch|WebSearch",
        "hooks": [
          {
            "type": "command",
            "command": "python3 /ABSOLUTE/PATH/TO/prototype/judge/pretool_gate.py",
            "timeout": 5
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Bash|WebFetch|WebSearch",
        "hooks": [
          {
            "type": "command",
            "command": "python3 /ABSOLUTE/PATH/TO/prototype/judge/posttool_record.py",
            "timeout": 5
          }
        ]
      }
    ],
    "PostToolUseFailure": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python3 /ABSOLUTE/PATH/TO/prototype/judge/posttool_record.py",
            "timeout": 5
          }
        ]
      }
    ],
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 /ABSOLUTE/PATH/TO/prototype/judge/session_start.py",
            "timeout": 10
          }
        ]
      }
    ],
    "PreCompact": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 /ABSOLUTE/PATH/TO/prototype/judge/precompact.py",
            "timeout": 10
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 /ABSOLUTE/PATH/TO/prototype/judge/stop_verify.py",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

### 3. Install `/aide` command

Copy or symlink:

```bash
mkdir -p ~/.claude/commands
cp prototype/judge/commands/aide.md ~/.claude/commands/aide.md
```

Every transform's optimized prompt is saved to `~/.claude-judge/pending_transform.md`. Type `/aide` to inspect it, `/aide run` to re-run it (useful after a `*`-bypassed turn).

### 4. Bypass

Prefix any prompt with `*` to skip the judge for that turn (power-user escape hatch).

### 5. Background re-mine (SessionStart)

`scheduled_mine.py` runs on every new session (async). If `~/.claude-judge/latest.json` is older than **24 hours**, it spawns:

```bash
python3 miner/mine.py --since 7 --quiet
```

in the background, then publishes an updated `rulebook.json`. Log: `~/.claude-judge/scheduled_mine.log`.

Tune with env vars (set in `~/.claude/settings.json` → `env` if needed):

| Variable | Default | Meaning |
|----------|---------|---------|
| `AIDE_MINE_ENABLED` | `1` | `0` to disable |
| `AIDE_MINE_INTERVAL_HOURS` | `24` | Min hours between runs |
| `AIDE_MINE_SINCE_DAYS` | `7` | Only scan sessions modified in last N days |

Re-run `python3 prototype/judge/install_hooks.py` after pulling updates to register the hook.

## What it does

### V1 — UserPromptSubmit

| Rule | Channel | Behavior |
|------|---------|----------|
| R1 | notice | New task with ≥40% context carried |
| R2 | transform → notice | Repeat of failed prompt (notice after 2 overrides) |
| R3 | notice + inject | Error/correction streak |
| R4 | notice | Missing CLAUDE.md (once/session) |
| R5 | transform | Vague opener → scoped request |
| R6 | transform | Mega-prompt decomposition → ordered task plan |
| R7 | inject | `<verification>` packet |
| R9 | notice | Unverified ship |
| R10 | notice | Cache gone cold |
| R11 | notice | Stale resumption (deduped with SessionStart + compaction) |
| R12 | notice | Marathon session |
| R13 | transform | Question stacking → numbered checklist |
| R14–R16 | notice/inject | Mined class-C patterns |
| R17 | notice + inject | Retry spiral retrospective |
| R18 | inject | Webfetch chain steer |
| R19 | inject | Repo context dumping steer |
| R20 | inject | Context stuffing focus anchor |
| R21 | transform | Error without repro → structured bug report |
| R22 | notice | Edit-before-plan redirect |
| Fatigue | — | Max 1 notice/prompt, 3/session; inject merge into `<carry_forward>` |

### V2 — Other hooks

| Hook | Behavior |
|------|----------|
| PreToolUse | Deny web chain ≥12, bash retry spiral ≥3, destructive-under-auto |
| PostToolUse | Increment web chain; reset on non-web tools |
| PostToolUseFailure | Track repeated bash failures by stderr signature |
| SessionStart | Resume recap; sets `r11_delivered` to dedup R11 |
| SessionStart (async) | `scheduled_mine.py` — incremental `mine.py` if last run >24h |
| PreCompact | Write `compact-memory/compact-*.md` snapshot |
| Stop | One-shot verify nudge if edits but no tests (respects `stop_hook_active`) |
| `/aide` | Inspect (`show`) or re-run (`run`) the last optimization |

### Progressive escalation (Phase 3)

Miner compiles `escalation` ladders per pattern into `rulebook.json`. Telemetry overrides advance start rung on re-mine. Reports (`change_first.md`, `findings_report.md`, `dashboard_data.json`) include escalation lines.

## Tests

```bash
cd prototype/judge
python3 -m unittest test_prompt_judge.py test_hooks_v2.py -v
```

## Known limitations

- Tier-2 LLM judge not in hot path.
- Thresholds calibrated from your own `baseline.json` after first mine.
- `pretool_gate` uses session_state counters, not transcript (avoids stale transcript_path).
