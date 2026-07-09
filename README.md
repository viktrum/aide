# aide

A real-time coach for Claude Code. One command, zero setup.

## Problem

I use Claude Code all day. I kept making the same expensive moves: resubmitting a prompt that just failed, pasting a stack trace with no context, carrying 100k tokens of dead history into a new task. Nothing ever told me. So I built AIDE. It watches how you work, catches your personal mistake patterns, and intervenes at the exact moment you're about to repeat one.

## How it looks

You press Enter like normal. When AIDE catches something, you see one line:

```
✦ prompt optimised: raw error dump structured into a bug report (R21). Prefix with * to bypass AIDE.
```

```
[judge] Resuming after ~26h idle with ~30k carried tokens. This looks like a
NEW task in an old shell. /clear or /compact first unless you need the history.
```

```
[judge] `npm test` ran 4x since your last prompt. Put the missing environment
context into CLAUDE.md so the agent stops probing.
```

## Install

```
npx agentaide
```

That's it. Your next Claude Code session is coached.

No Node? Same thing via curl:

```
curl -fsSL https://raw.githubusercontent.com/viktrum/aide/main/install.sh | bash
```

## What it does

- **Optimises weak prompts silently.** Blind retries, raw error dumps, mega-prompts, and vague openers get rewritten into structured requests the agent acts on. You see "prompt optimised", nothing else.
- **Saves tokens.** Nudges you to `/clear` stale sessions, stops runaway web-fetch chains, catches context stuffing.
- **Stops spirals.** Denies the same failing command the 4th time, blocks destructive bash under auto-accept.
- **Verifies before you ship.** Flags "commit/deploy" when no tests have run all session.
- **Learns your patterns** *(optional)*. Mines your session history into a personal rulebook, so interventions get more yours over time.

Works out of the box with built-in rules. No LLM calls in the hot path. Every intervention is deterministic and under 150ms.

## Commands

```
/aide status                 # health check
/aide feedback <what happened>  # tell AIDE an intervention was wrong (or great)
/aide show                   # inspect the last prompt optimization
*<prompt>                    # bypass AIDE for one prompt
```

## Personalize (optional)

Mine your own history into a rulebook of patterns generic rules can't know:

```
cd ~/.aide/prototype
python3 miner/mine.py        # scan your sessions (local, minutes)
python3 miner/label.py       # name your patterns via your own claude/gemini/codex CLI
```

Re-mining runs automatically in the background after that (every 24h, incremental).

## Uninstall

```
npx agentaide --uninstall
```

## Privacy

Everything stays local. AIDE reads `~/.claude/` session files on your machine, writes its state to `~/.claude-judge/`, and sends nothing anywhere. The optional mining step uses your own LLM CLI (`claude`, `agy`, or `codex`). Your keys, your quota, your machine.

## Docs

Full user guide and replication reference: [docs/AIDE_USER_GUIDE.md](docs/AIDE_USER_GUIDE.md)

## License

MIT
