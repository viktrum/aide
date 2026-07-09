#!/usr/bin/env python3
"""Prompt optimizer — builds the transform channel's optimized prompt.

When the judge flags a prompt, this module produces the rewritten request the
agent should act on instead of the raw prompt. Claude Code hooks cannot
literally replace the typed message (no updatedPrompt field exists on
UserPromptSubmit), so the rewrite is delivered as an authoritative
additionalContext packet; the user sees exactly one systemMessage line:
"prompt optimised".

Structure is always deterministic — templates filled from session signals.
Wording inside the structure can optionally be polished by a small LLM
(Haiku) for the rules where lexical quality matters (R5 vague opener,
R6 bundled asks). The LLM path is bounded by a hard timeout, validated
against the original prompt (file refs must survive), and always falls
back to the deterministic skeleton.

Stdlib only.
"""
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request

# Order decides which rule's rewrite wins when several transform rules fire.
TRANSFORM_PRIORITY = ["R2", "R21", "R6", "R13", "R5"]
# Structural-only rules never call the LLM; these two benefit from wording.
LEXICAL_RULES = {"R5", "R6"}

LABELS = {
    "R2": "repeat of a failed prompt rewritten as a diagnose-first retry",
    "R21": "raw error dump structured into a bug report",
    "R6": "bundled asks split into an ordered task plan",
    "R13": "stacked questions turned into an answer checklist",
    "R5": "vague opener scoped into an explicit request",
}

ASK_SPLIT_RE = re.compile(
    r"\b(?:and then|after that|then also|also|additionally|plus)\b|;|\n\s*[-*]|\n\s*\d+[.)]",
    re.I)
FILE_REF_RE = re.compile(
    r"[\w./\\-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|rb|md|json|yaml|yml|toml|css|html|sql|sh)\b"
    r"|/[\w./-]{3,}", re.I)

DEFAULT_API_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_CLI_MODEL = "haiku"
API_URL = "https://api.anthropic.com/v1/messages"

REWRITE_INSTRUCTIONS = """You rewrite a developer's prompt to a coding agent. Rules:
- Preserve the developer's intent exactly. Never invent files, APIs, error text, or requirements.
- Keep every file path, identifier, command, and error string from the raw prompt verbatim.
- Make the ask specific: goal, scope, constraints, and how to verify success.
- Mark genuinely unknown details as unknown instead of guessing; allow at most one targeted question back to the developer.
- Keep the tags and ordering of the structural skeleton.
- Output ONLY the rewritten prompt text. No preamble, no commentary, no code fences.

Raw prompt:
<raw>
{prompt}
</raw>

Structural skeleton to preserve:
<skeleton>
{skeleton}
</skeleton>"""


# ---------------------------------------------------------------- builders

def _build_r2(prompt, sig):
    error_line = sig.get("last_run_error_snippet") or (
        "the previous attempt produced tool errors")
    return (
        "<previous_attempt_summary>\n"
        f"A nearly identical prompt just failed. Last error: {error_line}\n"
        "</previous_attempt_summary>\n"
        "<request>\n"
        "Do not repeat the previous approach. First diagnose why the last "
        "attempt failed (root cause, not symptom). If required information is "
        "missing, ask ONE targeted question. Then apply the smallest fix and "
        "verify it.\n"
        "</request>\n"
        f"<original_prompt>\n{prompt}\n</original_prompt>")


def _build_r21(prompt, sig):
    return (
        "<error_report>\n"
        f"  <error>\n{prompt}\n  </error>\n"
        "  <trigger>unknown; infer from the code or ask one targeted question</trigger>\n"
        "  <expected>unknown</expected>\n"
        "  <runtime>unknown</runtime>\n"
        "</error_report>\n"
        "<request>\n"
        "Identify the root cause before editing. Consider null/undefined "
        "defaults, async state, missing data, and race conditions. Apply the "
        "smallest fix plus one verification step.\n"
        "</request>")


def _build_r6(prompt, sig):
    parts = [p.strip(" .,\n\t-*") for p in ASK_SPLIT_RE.split(prompt)]
    tasks = [p for p in parts if p and len(p.split()) > 3]
    if len(tasks) < 2:
        return None
    listing = "\n".join(f"{i}. {t}" for i, t in enumerate(tasks, 1))
    return (
        "Work through these tasks strictly one at a time:\n"
        f"{listing}\n"
        "After each task, run the relevant verification before starting the "
        "next. If priorities or ordering are unclear, confirm with the user "
        "before task 1.")


def _build_r13(prompt, sig):
    questions = [re.sub(r"\s+", " ", q).strip(" \n") for q in re.findall(r"[^?]*\?", prompt)]
    questions = [q for q in questions if len(q.split()) >= 2]
    if len(questions) < 2:
        return None
    listing = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1))
    return (
        "Answer every question below as a numbered checklist. Do not skip "
        "any:\n"
        f"{listing}")


def _build_r5(prompt, sig):
    return (
        f"<request>\n{prompt}\n</request>\n"
        "<missing_context>\n"
        "  files or areas involved: unknown\n"
        "  expected behavior: unknown\n"
        "  verification: unknown\n"
        "</missing_context>\n"
        "State your interpretation of the request in one line. If the scope is "
        "ambiguous, ask at most ONE targeted question; otherwise proceed with "
        "the smallest change that satisfies the request and verify it.")


BUILDERS = {
    "R2": _build_r2,
    "R21": _build_r21,
    "R6": _build_r6,
    "R13": _build_r13,
    "R5": _build_r5,
}


# ---------------------------------------------------------------- LLM polish

def _config(rulebook):
    cfg = (rulebook or {}).get("optimizer") or {}
    return {
        "llm": os.environ.get("AIDE_OPTIMIZER_LLM") or cfg.get("llm", "auto"),
        "model": cfg.get("model", ""),
        "timeout_s": float(cfg.get("timeout_s", 6)),
    }


def _valid_rewrite(prompt, skeleton, text):
    text = (text or "").strip()
    if not text or text.startswith("```"):
        return False
    if len(text) > max(3 * len(skeleton), 2000):
        return False
    for match in FILE_REF_RE.finditer(prompt):
        if match.group(0) not in text:
            return False
    return True


def _call_api(instructions, key, config):
    body = json.dumps({
        "model": config["model"] or DEFAULT_API_MODEL,
        "max_tokens": 1000,
        "messages": [{"role": "user", "content": instructions}],
    }).encode()
    req = urllib.request.Request(API_URL, data=body, headers={
        "content-type": "application/json",
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
    })
    try:
        with urllib.request.urlopen(req, timeout=config["timeout_s"]) as resp:
            data = json.loads(resp.read().decode())
        parts = data.get("content") or []
        return "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return None


def _call_cli(instructions, config):
    """Haiku via the user's own Claude Code CLI (subscription auth, no key).

    AIDE_JUDGE_BYPASS makes our own hooks exit immediately in the child
    session, preventing recursion.
    """
    try:
        proc = subprocess.run(
            ["claude", "-p", "--model", config["model"] or DEFAULT_CLI_MODEL],
            input=instructions, capture_output=True, text=True,
            timeout=config["timeout_s"],
            env={**os.environ, "AIDE_JUDGE_BYPASS": "1"},
        )
        return proc.stdout.strip() if proc.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _llm_polish(prompt, skeleton, config):
    """auto = API polish only when a key exists (fast); otherwise stay
    deterministic so the sub-150ms prompt path holds on default installs.
    cli = explicit opt-in to Haiku via the user's claude CLI; adds seconds
    on flagged prompts."""
    mode = config["llm"]
    if mode not in ("auto", "api", "cli"):
        return None
    instructions = REWRITE_INSTRUCTIONS.format(prompt=prompt, skeleton=skeleton)
    key = os.environ.get("ANTHROPIC_API_KEY")
    if mode in ("auto", "api"):
        if not key:
            return None
        text = _call_api(instructions, key, config)
    else:
        text = _call_cli(instructions, config)
    if text and _valid_rewrite(prompt, skeleton, text):
        return text.strip()
    return None


# ---------------------------------------------------------------- interface

def optimize(rule_id, prompt, sig, rulebook):
    """Build the optimized prompt for a fired rule. None = no transform."""
    builder = BUILDERS.get(rule_id)
    if not builder:
        return None
    skeleton = builder(prompt, sig or {})
    if not skeleton:
        return None
    started = time.time()
    text, method = skeleton, "deterministic"
    if rule_id in LEXICAL_RULES:
        polished = _llm_polish(prompt, skeleton, _config(rulebook))
        if polished:
            text, method = polished, "haiku"
    return {
        "rule": rule_id,
        "label": LABELS.get(rule_id, "prompt restructured"),
        "text": text,
        "method": method,
        "latency_ms": int((time.time() - started) * 1000),
    }


def transform_context(transform):
    """additionalContext packet directing the agent to act on the rewrite."""
    return (
        f"[aide] PROMPT OPTIMISED: {transform['label']}.\n"
        "The block below is the canonical version of the user's request this "
        "turn. Act on it as the user's actual request; where it differs from "
        "the raw prompt, the optimized version wins. Do not mention or "
        "restate the rewrite.\n"
        f"<optimized_prompt rule=\"{transform['rule']}\">\n"
        f"{transform['text']}\n"
        "</optimized_prompt>")


def transform_notice(transform):
    """The single user-visible line."""
    return (f"✦ prompt optimised: {transform['label']} "
            f"({transform['rule']}). Prefix with * to bypass AIDE.")
