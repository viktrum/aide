#!/usr/bin/env python3
"""Shared per-session state for V2 hooks (stdlib only).

State is keyed by session_id from hook payloads. Hooks never parse the
transcript in PreToolUse — counters live here to avoid stale transcript_path
issues (anthropics/claude-code#58682).
"""
import json
import os
import time
from pathlib import Path

DATA_DIR = Path(os.environ.get("CLAUDE_JUDGE_HOME", Path.home() / ".claude-judge"))
STATE_DIR = DATA_DIR / "session_state"
CONFIG_PATH = DATA_DIR / "config.json"


def intervention_scope():
    """'prompt' (default): coaching on prompts only, blast radius limited to
    what you typed. 'full': also enables tool gates and stop verification.
    Env AIDE_SCOPE overrides ~/.claude-judge/config.json."""
    env = os.environ.get("AIDE_SCOPE")
    if env:
        return env.strip().lower()
    try:
        return str(json.loads(CONFIG_PATH.read_text()).get("scope", "prompt")).strip().lower()
    except (OSError, json.JSONDecodeError, AttributeError):
        return "prompt"
COMPACT_MEMORY_DIR = DATA_DIR / "compact-memory"
PRUNE_AGE_SEC = 7 * 24 * 3600
COMPACT_PRUNE_AGE_SEC = 30 * 24 * 3600


def _path(session_id):
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (session_id or "unknown"))
    return STATE_DIR / f"{safe}.json"


def _maybe_prune():
    """Delete state files older than 7 days (at most once per hour)."""
    try:
        stamp = DATA_DIR / ".state_prune_ts"
        now = time.time()
        if stamp.exists() and now - float(stamp.read_text().strip() or 0) < 3600:
            return
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        for p in STATE_DIR.glob("*.json"):
            try:
                if now - p.stat().st_mtime > PRUNE_AGE_SEC:
                    p.unlink()
            except OSError:
                pass
        if COMPACT_MEMORY_DIR.is_dir():
            for p in COMPACT_MEMORY_DIR.glob("compact-*.md"):
                try:
                    if now - p.stat().st_mtime > COMPACT_PRUNE_AGE_SEC:
                        p.unlink()
                except OSError:
                    pass
        stamp.write_text(str(now))
    except OSError:
        pass


def load(session_id):
    _maybe_prune()
    try:
        return json.loads(_path(session_id).read_text())
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def save(session_id, state):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        state["updated_at"] = round(time.time(), 3)
        _path(session_id).write_text(json.dumps(state))
    except OSError:
        pass


def turn_reset(session_id, turn, research_intent=False):
    """Open a new user turn: zero per-turn counters, stamp research intent."""
    state = load(session_id)
    state.update({
        "turn": turn,
        "web_chain": 0,
        "research_intent": bool(research_intent),
    })
    state.setdefault("bash", {})
    save(session_id, state)
    return state
