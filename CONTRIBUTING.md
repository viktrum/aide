# Contributing

AIDE is early. The most valuable contribution right now is evidence:

- **False fires** — an intervention triggered when it shouldn't have. Run `/aide feedback <what happened>` locally, then open an issue with the rule ID from the message (e.g. R21) and what you expected.
- **Missed patterns** — an expensive mistake AIDE should have caught. Describe the moment; transcripts stay yours, redact freely.
- **Portability** — AIDE is tested on macOS/Linux with Python 3.9+. Platform issues are welcome.

## Code

```
cd prototype/judge && python3 -m unittest test_prompt_judge.py test_hooks_v2.py test_optimizer.py
cd ../miner && python3 -m unittest discover -p "test_*.py"
```

All 150+ tests must pass. Hooks are stdlib-only by design — no dependencies, exit 0 on every error path, <150ms budget on the prompt path. PRs that add pip dependencies to the hot path will be declined.
