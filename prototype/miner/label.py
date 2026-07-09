#!/usr/bin/env python3
"""History miner, stage 2: LLM labeling via programmatic Claude.

Reads the shortlist from mine.py, asks an LLM (via local CLI — claude, agy, or
codex) to name recurring patterns, merges them into the rulebook the real-time
judge matches against, and regenerates the user guide.

Usage: python3 label.py [--dry-run] [--max-segments N]
       python3 label.py --backend agy --model gemini-2.5-pro
       JUDGE_LABEL_BACKEND=codex JUDGE_LABEL_MODEL=gpt-5.5-codex python3 label.py
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "judge"))
from judge_store import ROOT, resolve_run_dir, publish_artifacts, get_latest_run_dir, DETECTOR_VERSION

DATA_DIR = ROOT

# The labeling prompt lives in a file so it can be iterated without code
# changes. Falls back to the embedded LABEL_PROMPT if the file is absent.
PROMPT_FILE = Path(os.environ.get(
    "JUDGE_LABEL_PROMPT",
    Path(__file__).resolve().parents[2] / "prompts" / "pattern-miner.txt"))

# Signals the labeler may use in triggers — matches SIGNAL_SEMANTICS below.
# Context/resource signals are deliberately excluded: those belong to the
# deterministic R-rules, not mined patterns.
SIGNAL_VOCAB = [
    "consecutive_error_turns", "recent_corrections",
    "prompt_similarity_to_failed", "prompt_word_count", "prompt_has_file_ref",
    "prompt_imperative_asks", "session_has_test_evidence", "is_first_prompt",
    "turns", "prompt_mentions_internal_tool_names",
]

# Layout-only signals: describe prompt geometry, not segment-flag behavior or
# prompt content. Triggers using ONLY these cannot justify behavioral messages
# (e.g. "you retry without new info" from word_count alone).
LAYOUT_ONLY_SIGNALS = {
    "prompt_word_count", "is_first_prompt", "prompt_has_file_ref",
    "prompt_imperative_asks", "turns", "session_has_test_evidence",
    "prompt_question_marks",
}

# Kept for tests/docs — behavioral + content signals are never "layout only".
GENERIC_SHAPE_SIGNALS = LAYOUT_ONLY_SIGNALS | {
    "consecutive_error_turns", "recent_corrections", "prompt_similarity_to_failed",
}

SIGNAL_SEMANTICS = """- consecutive_error_turns: failing tool turns immediately before now (0-10)
- recent_corrections: correction-type messages in the last 5 prompts (0-5)
- prompt_similarity_to_failed: lexical similarity of the new prompt to a recently failed one (0.0-1.0)
- prompt_word_count: words in the new prompt (1-500)
- prompt_has_file_ref: prompt names a file or path (0/1)
- prompt_imperative_asks: distinct tasks bundled in one prompt (1-10)
- session_has_test_evidence: any test/build has run this session (0/1)
- is_first_prompt: first prompt of the session (0/1)
- turns: user prompts so far this session (0-200)
- prompt_mentions_internal_tool_names: prompt names internal tool APIs (0/1)"""

LABEL_PROMPT = """Your only job: find RECURRING mistakes in how ONE developer prompts their coding agent, using flagged transcript moments. You are conservative: a missed pattern is fine, an invented pattern is not.

INPUT — numbered segments, each auto-flagged by one signal:
- user_correction: the user had to correct the agent's previous output
- retry_loop: the user resubmitted a near-identical prompt
- error_streak: 3+ tool errors followed one prompt

RULES
1. A pattern needs >=2 supporting segments. Fewer = not a pattern.
2. Describe only what the evidence shows. Never infer habits beyond it.
3. Maximum 5 patterns. [] is a valid and respectable answer.
4. Every pattern cites its supporting segment indices.

Each pattern needs a TRIGGER a program will evaluate — conditions over these signals only (meaning, typical range):
%s

The "message" is shown at the exact moment the user is about to repeat the mistake. It must be second person, at most 2 sentences, name the specific behavior, and give the alternative action. Generic advice ("prompt better") is forbidden.

OUTPUT: only a JSON array matching this schema, no prose, no markdown fence:
[{"aggregation_key": "snake_case", "category": "decomposition|evaluation|system_design|recovery|taste|resource", "title": "...", "description": "...", "trigger": {"all": [{"signal": "...", "op": ">=", "value": 0}]}, "action": {"channel": "inject|stdout", "message": "..."}, "supporting_segments": [0]}]

EXAMPLE (a different developer)
Input: [0] retry_loop: "fix the auth test" -> "fix the auth test" (2 errors between) . [1] user_correction: "fix the auth test" -> "still failing, you changed the wrong file"
Output: [{"aggregation_key": "retry_without_new_information", "category": "recovery", "title": "Retries add no new information", "description": "When a fix fails, you resend the same request instead of saying what was wrong with the attempt.", "trigger": {"all": [{"signal": "prompt_similarity_to_failed", "op": ">=", "value": 0.8}, {"signal": "consecutive_error_turns", "op": ">=", "value": 1}]}, "action": {"channel": "inject", "message": "Your last attempt at this failed. Say what it got wrong (wrong file? wrong approach?) instead of repeating the request."}, "supporting_segments": [0, 1]}]

SEGMENTS:
%s
"""

VALID_OPS = {">=", ">", "<=", "<", "=="}
VALID_CATEGORIES = {"decomposition", "evaluation", "system_design", "recovery", "taste", "resource"}
VALID_CHANNELS = {"inject", "stdout"}
GENERIC_MSG_RE = None  # reserved: lexical check for generic advice
DEFAULT_BATCH_SIZE = 10
BATCH_NOTE = (
    "\nNOTE: This is batch {batch_num}/{total_batches} of the full shortlist "
    "({seg_start}-{seg_end} of {total_segments}). Only cite segment indices "
    "shown below ([0] through [{last_idx}]).\n"
)


def load_json(path, default):
    try:
        return json.loads(path.read_text()) if path.exists() else default
    except json.JSONDecodeError:
        return default


def explain_empty_shortlist(baseline_path, findings_path):
    baseline = load_json(baseline_path, {})
    generic_count = baseline.get("generic_findings", 0)

    print("Personalized shortlist is empty — no correction/retry/user-intervention segments to label.")
    print("That only means the pattern-miner has no personalized segments to process.")

    if generic_count:
        by_pattern = baseline.get("by_pattern", {})
        summary = ", ".join(f"{k}: {v}" for k, v in sorted(by_pattern.items()))
        print(f"Generic detector floor did find {generic_count} findings.")
        if summary:
            print(f"Generic findings by pattern: {summary}")
        print(f"Inspect generic findings at {findings_path}")
    else:
        print("No generic findings are recorded in baseline.json either.")


def uses_only_shape_signals(trigger):
    """True when trigger has no behavioral or content signal — layout-only."""
    conds = (trigger or {}).get("all") or []
    return bool(conds) and all(c.get("signal") in LAYOUT_ONLY_SIGNALS for c in conds)


def triggers_partition_pair(a, b):
    """True when two triggers differ only by complementary bounds on one signal."""
    ca = (a.get("trigger") or {}).get("all") or []
    cb = (b.get("trigger") or {}).get("all") or []
    if len(ca) != len(cb):
        return False
    differing = []
    for left, right in zip(sorted(ca, key=lambda c: c.get("signal", "")),
                           sorted(cb, key=lambda c: c.get("signal", ""))):
        if left.get("signal") != right.get("signal"):
            return False
        if left != right:
            differing.append(left.get("signal"))
    return len(differing) == 1


def validate_candidate(c, n_segments):
    """Never trust the model to enforce its own schema. Returns error or None."""
    key = c.get("aggregation_key", "")
    if not re.fullmatch(r"[a-z0-9_]{3,60}", key or ""):
        return f"bad aggregation_key: {key!r}"
    if c.get("category") not in VALID_CATEGORIES:
        return f"bad category: {c.get('category')!r}"
    support = c.get("supporting_segments")
    if (not isinstance(support, list) or len(support) < 2
            or not all(isinstance(i, int) and 0 <= i < n_segments for i in support)):
        return f"bad supporting_segments: {support!r}"
    conds = (c.get("trigger") or {}).get("all")
    if not isinstance(conds, list) or not conds:
        return "trigger missing"
    for cond in conds:
        if cond.get("signal") not in SIGNAL_VOCAB:
            return f"unknown signal: {cond.get('signal')!r}"
        if cond.get("op") not in VALID_OPS:
            return f"unknown op: {cond.get('op')!r}"
        if not isinstance(cond.get("value"), (int, float)):
            return f"non-numeric value: {cond.get('value')!r}"
    # Selectivity: every signal is non-negative, so a trigger whose conditions
    # are all ">= 0" / "> -x" style is always-true and therefore meaningless.
    if all(cond["op"] in (">=", ">") and cond["value"] <= 0 for cond in conds):
        return "trigger is always true (fails selectivity)"
    if uses_only_shape_signals(c.get("trigger")):
        return "trigger uses only layout signals (fails selectivity)"
    action = c.get("action") or {}
    if action.get("channel") not in VALID_CHANNELS:
        return f"bad channel: {action.get('channel')!r}"
    msg = action.get("message", "")
    if not (10 <= len(msg) <= 400):
        return f"message length {len(msg)} outside 10-400"
    return None


def call_llm(prompt, model=None, backend=None):
    """LLM via local CLI — no external API keys in this script.

    Backend selection (first match wins):
      JUDGE_LABEL_BACKEND env: claude | agy | codex
      --backend CLI flag
    Model:
      JUDGE_LABEL_MODEL env or --model flag
      Defaults: haiku (claude), gemini-2.5-pro (agy), gpt-5.5-codex (codex)
    """
    backend = (backend or os.environ.get("JUDGE_LABEL_BACKEND") or "claude").lower()
    if backend == "claude":
        model = model or os.environ.get("JUDGE_LABEL_MODEL") or "haiku"
        cmd = ["claude", "-p", prompt, "--model", model]
    elif backend == "agy":
        model = model or os.environ.get("JUDGE_LABEL_MODEL") or "gemini-2.5-pro"
        cmd = ["agy", "--model", model, "-p", prompt]
    elif backend == "codex":
        model = model or os.environ.get("JUDGE_LABEL_MODEL") or "gpt-5.5-codex"
        # codex exec: progress → stderr, final message → stdout (like claude -p)
        cmd = ["codex", "exec", "--model", model, "--ephemeral", prompt]
    else:
        raise ValueError(f"unknown JUDGE_LABEL_BACKEND: {backend!r} (use claude, agy, codex)")

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(
            f"{' '.join(cmd[:3])}… failed: {(result.stderr or result.stdout)[:500]}")
    return result.stdout


def call_claude(prompt, model="haiku"):
    """Backward-compatible wrapper."""
    return call_llm(prompt, model=model, backend="claude")


def build_label_prompt(batch_segments, batch_num, total_batches, seg_start, total_segments):
    numbered = "\n".join(f"[{i}] {json.dumps(s)}" for i, s in enumerate(batch_segments))
    note = BATCH_NOTE.format(
        batch_num=batch_num,
        total_batches=total_batches,
        seg_start=seg_start,
        seg_end=seg_start + len(batch_segments) - 1,
        total_segments=total_segments,
        last_idx=len(batch_segments) - 1,
    )
    if PROMPT_FILE.exists():
        body = PROMPT_FILE.read_text().replace("{segments}", numbered)
        return note + body
    return note + (LABEL_PROMPT % (SIGNAL_SEMANTICS, numbered))


def remap_candidate_indices(candidate, offset):
    """Batch-local supporting_segments → global shortlist indices."""
    out = dict(candidate)
    out["supporting_segments"] = [offset + i for i in candidate.get("supporting_segments", [])]
    return out


def consolidate_candidates(candidates):
    """Merge same aggregation_key across batches; union supporting_segments."""
    by_key = {}
    for c in candidates:
        key = c.get("aggregation_key")
        if not key:
            continue
        if key not in by_key:
            by_key[key] = dict(c)
            by_key[key]["supporting_segments"] = list(c.get("supporting_segments", []))
        else:
            merged = by_key[key]
            merged["supporting_segments"] = sorted(set(
                merged["supporting_segments"] + c.get("supporting_segments", [])))
            if len(c.get("description", "")) > len(merged.get("description", "")):
                merged["description"] = c["description"]
            if len((c.get("action") or {}).get("message", "")) > len(
                    (merged.get("action") or {}).get("message", "")):
                merged["action"] = c["action"]
    return list(by_key.values())


def label_in_batches(segments, backend, model, batch_size=DEFAULT_BATCH_SIZE):
    """Call LLM per batch; consolidate and validate globally."""
    batches = []
    for start in range(0, len(segments), batch_size):
        batches.append((start, segments[start:start + batch_size]))

    total_batches = len(batches)
    deferred = []

    for batch_num, (offset, batch) in enumerate(batches, start=1):
        print(f"  batch {batch_num}/{total_batches}: segments {offset}–"
              f"{offset + len(batch) - 1} ({len(batch)} items)…", file=sys.stderr, flush=True)
        prompt = build_label_prompt(batch, batch_num, total_batches, offset, len(segments))
        raw = call_llm(prompt, model=model, backend=backend)
        try:
            batch_candidates = extract_json_array(raw)
        except ValueError as exc:
            print(f"  batch {batch_num} parse error: {exc}", file=sys.stderr)
            continue

        for c in batch_candidates:
            err = validate_candidate(c, len(batch))
            if err:
                print(f"  batch {batch_num} rejected: {err}", file=sys.stderr)
                continue
            deferred.append(remap_candidate_indices(c, offset))

    consolidated = consolidate_candidates(deferred)
    valid = []
    for c in consolidated:
        support = sorted(set(c.get("supporting_segments", [])))
        c["supporting_segments"] = support
        if len(support) < 2:
            print(f"  deferred (needs >=2 global segments): {c.get('aggregation_key')}",
                  file=sys.stderr)
            continue
        err = validate_candidate(c, len(segments))
        if err:
            print(f"  rejected after merge: {err}", file=sys.stderr)
            continue
        if any(triggers_partition_pair(c, other) for other in valid):
            print(f"  rejected partition pair: {c.get('aggregation_key')}",
                  file=sys.stderr)
            continue
        valid.append(c)
    return valid


def extract_json_array(text):
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise ValueError(f"No JSON array in model output: {text[:300]}")
    return json.loads(m.group(0))


def merge_into_rulebook(candidates, segments, rulebook_path):
    book = {"patterns": []}
    if rulebook_path.exists():
        try:
            book = json.loads(rulebook_path.read_text())
        except json.JSONDecodeError:
            pass
    existing = {p.get("aggregation_key"): p for p in book.get("patterns", [])}

    for c in candidates:
        key = c.get("aggregation_key")
        if not key or not c.get("trigger"):
            continue
        support = c.get("supporting_segments", [])
        evidence = [{"session_id": segments[i]["session_id"],
                     "turn": segments[i]["turn"],
                     "excerpt": segments[i]["excerpt"]}
                    for i in support if 0 <= i < len(segments)][:5]
        if key in existing:
            p = existing[key]
            merged_evidence = list(p.get("evidence", []))
            seen = {(e.get("session_id"), e.get("turn")) for e in merged_evidence}
            for ev in evidence:
                key_ev = (ev.get("session_id"), ev.get("turn"))
                if key_ev not in seen:
                    merged_evidence.append(ev)
                    seen.add(key_ev)
            p["evidence"] = merged_evidence[:5]
            p["stats"]["occurrences"] = len(p["evidence"])
            p["stats"]["last_seen"] = time.strftime("%Y-%m-%d")
        else:
            existing[key] = {
                "id": f"pat_{key}",
                "aggregation_key": key,
                "category": c.get("category", "recovery"),
                "title": c.get("title", key),
                "description": c.get("description", ""),
                "trigger": c["trigger"],
                "action": c.get("action", {"channel": "inject", "message": c.get("description", "")}),
                "evidence": evidence,
                "stats": {"occurrences": len(support), "est_wasted_tokens": 0,
                          "last_seen": time.strftime("%Y-%m-%d")},
                "confidence": min(0.9, 0.3 + 0.1 * len(support)),
                "rights": {"blocking": False,
                           "unlock": {"min_occurrences": 5, "user_confirmed": True,
                                      "min_accept_rate": 0.6}},
                "telemetry": {"fired": 0, "accepted": 0, "ignored": 0},
                "user_status": "proposed",
            }

    book["patterns"] = list(existing.values())
    rulebook_path.write_text(json.dumps(book, indent=1))
    return book


def write_guide(book, baseline, guide_path):
    lines = ["# Your Claude Code Improvement Guide",
             "",
             f"_Generated {time.strftime('%Y-%m-%d')} from {baseline.get('sessions_scanned', '?')} local sessions. "
             "Nothing left your machine._",
             ""]
    if baseline:
        lines += [f"- Average tokens per session: **{baseline.get('avg_tokens_per_session', 0):,}**",
                  f"- 90th-percentile context peak: **{baseline.get('p90_context_peak', 0):,} tokens**",
                  f"- Flagged moments: **{baseline.get('segments_flagged', 0)}** "
                  f"({', '.join(f'{k}: {v}' for k, v in baseline.get('by_signal', {}).items())})",
                  ""]
    for p in sorted(book.get("patterns", []),
                    key=lambda p: -p["stats"]["occurrences"]):
        lines += [f"## {p['title']}  `{p['category']}`",
                  "",
                  p["description"],
                  "",
                  f"Seen **{p['stats']['occurrences']}x** · confidence {p['confidence']:.0%} · "
                  f"status: {p['user_status']}",
                  ""]
        for ev in p.get("evidence", [])[:2]:
            lines.append(f"> {ev['excerpt'][:200]}")
            lines.append("")
        lines += [f"**When it fires:** {p['action']['message']}", ""]
    lines += ["---",
              "",
              "To confirm or mute a pattern, edit `user_status` in "
              f"`{guide_path.parent / 'rulebook.json'}` (proposed → confirmed / muted). "
              "Confirmed patterns can earn blocking rights.", ""]
    guide_path.write_text("\n".join(lines))


def slugify_model(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def execute_label(
    run_dir,
    *,
    backend="agy",
    model=None,
    max_segments=60,
    batch_size=None,
    output_suffix=None,
    publish=True,
):
    """Run batched labeling; return summary dict. Used by label.py and label_compare.py."""
    shortlist = run_dir / "shortlist.json"
    baseline_path = run_dir / "baseline.json"
    suffix = f"-{output_suffix}" if output_suffix else ""
    if output_suffix:
        rulebook_path = run_dir / f"rulebook-label-{output_suffix}.json"
        guide_path = run_dir / f"guide-{output_suffix}.md"
        candidates_path = run_dir / f"label_candidates-{output_suffix}.json"
    else:
        rulebook_path = run_dir / "rulebook.json"
        guide_path = run_dir / "guide.md"
        candidates_path = run_dir / "label_candidates.json"

    data = json.loads(shortlist.read_text())
    segments = data["segments"][:max_segments]
    if not segments:
        raise ValueError("shortlist empty")

    batch_size = batch_size or int(os.environ.get("JUDGE_LABEL_BATCH_SIZE", DEFAULT_BATCH_SIZE))
    batch_size = max(1, min(batch_size, len(segments)))

    n_batches = (len(segments) + batch_size - 1) // batch_size
    print(f"Labeling {len(segments)} segments in {n_batches} batch(es) "
          f"(size {batch_size}) via `{backend}`"
          + (f" model={model}" if model else "") + "…", file=sys.stderr)

    candidates = label_in_batches(segments, backend, model, batch_size=batch_size)
    print(f"{len(candidates)} candidates passed validation.", file=sys.stderr)

    baseline = load_json(baseline_path, {})
    book = merge_into_rulebook(candidates, segments, rulebook_path)
    write_guide(book, baseline, guide_path)
    candidates_path.write_text(json.dumps({
        "backend": backend,
        "model": model,
        "segments": len(segments),
        "candidates": candidates,
        "patterns": book.get("patterns", []),
    }, indent=1))

    if publish and not output_suffix:
        publish_artifacts(run_dir, DATA_DIR)

    return {
        "run_dir": str(run_dir),
        "backend": backend,
        "model": model,
        "segments": len(segments),
        "candidates": len(candidates),
        "patterns": len(book.get("patterns", [])),
        "guide": str(guide_path),
        "rulebook": str(rulebook_path),
        "candidates_file": str(candidates_path),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="print the labeling prompt, skip the LLM call")
    ap.add_argument("--max-segments", type=int, default=60)
    ap.add_argument("--backend", choices=["claude", "agy", "codex"],
                    help="LLM CLI backend (default: JUDGE_LABEL_BACKEND or claude)")
    ap.add_argument("--model", help="Model id for the chosen backend")
    ap.add_argument("--run-id", help="Label a specific archived run (default: latest)")
    ap.add_argument("--output-suffix", help="Write guide/rulebook with suffix (no overwrite)")
    ap.add_argument("--no-publish", action="store_true",
                    help="Do not copy rulebook to ~/.claude-judge/ (compare runs)")
    args = ap.parse_args()

    try:
        run_dir = resolve_run_dir(args.run_id)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)

    shortlist = run_dir / "shortlist.json"
    baseline_path = run_dir / "baseline.json"
    findings_path = run_dir / "findings.json"

    if not shortlist.exists():
        print(f"No shortlist in {run_dir}. Run mine.py first.", file=sys.stderr)
        sys.exit(1)
    data = json.loads(shortlist.read_text())
    print(f"Run archive: {run_dir}")
    if isinstance(data, list):
        print("STALE SHORTLIST: generated by an old detector. "
              "Run mine.py again first.", file=sys.stderr)
        sys.exit(1)
    if data.get("detector_version", 0) < DETECTOR_VERSION:
        print(f"STALE SHORTLIST (detector v{data.get('detector_version')}, "
              f"current v{DETECTOR_VERSION}). Run mine.py again first.", file=sys.stderr)
        sys.exit(1)
    print(f"Shortlist generated {data.get('generated_at')} "
          f"(detector v{data['detector_version']})")
    segments = data["segments"][:args.max_segments]
    if not segments:
        explain_empty_shortlist(baseline_path, findings_path)
        sys.exit(0)

    batch_size = int(os.environ.get("JUDGE_LABEL_BATCH_SIZE", DEFAULT_BATCH_SIZE))
    batch_size = max(1, min(batch_size, len(segments)))

    if args.dry_run:
        prompt = build_label_prompt(
            segments[:batch_size], 1,
            (len(segments) + batch_size - 1) // batch_size,
            0, len(segments))
        print(prompt)
        print(f"\n(dry-run: would run {(len(segments) + batch_size - 1) // batch_size} "
              f"batches of up to {batch_size} segments)", file=sys.stderr)
        return

    backend = args.backend or os.environ.get("JUDGE_LABEL_BACKEND") or "claude"
    model = args.model or os.environ.get("JUDGE_LABEL_MODEL")
    summary = execute_label(
        run_dir,
        backend=backend,
        model=model,
        max_segments=args.max_segments,
        batch_size=batch_size,
        output_suffix=args.output_suffix,
        publish=not args.no_publish,
    )
    print(f"{summary['patterns']} patterns → {summary['rulebook']}")
    print(f"Guide → {summary['guide']}")
    if args.no_publish:
        print("(skipped publish to live judge — use without --no-publish to activate)")


if __name__ == "__main__":
    main()
