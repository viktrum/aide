#!/usr/bin/env python3
"""PreCompact hook — write durable memory snapshot before context compaction."""
import json
import os
import sys
import time
from pathlib import Path

DATA_DIR = Path(os.environ.get("CLAUDE_JUDGE_HOME", Path.home() / ".claude-judge"))
COMPACT_MEMORY_DIR = DATA_DIR / "compact-memory"
TAIL_BYTES = 256_000


def read_tail_text(transcript_path):
    try:
        p = Path(transcript_path)
        size = p.stat().st_size
        with open(p, "rb") as f:
            if size > TAIL_BYTES:
                f.seek(size - TAIL_BYTES)
                f.readline()
            return f.read().decode("utf-8", errors="replace")
    except (OSError, TypeError):
        return ""


def extract_summary(raw):
    lines = []
    for line in raw.splitlines()[-40:]:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "user":
            continue
        content = (entry.get("message") or {}).get("content")
        if isinstance(content, str) and content.strip():
            lines.append(content.strip()[:200])
    return "\n".join(lines[-8:])


def main():
    if os.environ.get("AIDE_JUDGE_BYPASS"):  # optimizer CLI child — no recursion
        sys.exit(0)
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, TypeError):
        sys.exit(0)

    session_id = payload.get("session_id", "unknown")
    transcript = payload.get("transcript_path")
    raw = read_tail_text(transcript)
    summary = extract_summary(raw)

    try:
        COMPACT_MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)
        path = COMPACT_MEMORY_DIR / f"compact-{safe}-{ts}.md"
        path.write_text(
            f"# Compact snapshot {ts}\n\n"
            f"session: {session_id}\n\n"
            f"## Recent user prompts\n\n{summary or '(no recent prompts)'}\n")
        print(json.dumps({
            "systemMessage": f"[judge/compact] Snapshot written: {path}",
        }))
    except OSError:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
