"""Repo/config lints (extension doc §18). All T0, all class D-patch.

Entry point: run_config_lints(repo_paths, sessions) -> list of finding dicts.
Findings carry a `repo` key plus a `suggested_patch` (text only, never
applied). render_config_report() turns them into config_report.md.
"""
import json
import re
from pathlib import Path

INSTRUCTION_FILENAMES = ("CLAUDE.md", "AGENTS.md", ".cursorrules")
IGNORE_WORTHY_DIRS = ("node_modules", "dist", "build", ".next", "coverage")
BLOAT_LINE_LIMIT = 200
DIRECTIVE_LIMIT = 20
EXCERPT_LEN = 160

# R41 — file-discovery commands inside instruction files.
SCAN_CMD_RE = re.compile(
    r"^.*\b(grep\s+-r|rg\s+|find\s+[~./]|ls\s+-R|tree)\b.*$",
    re.I | re.M,
)
# R32 — directive-overload markers.
DIRECTIVE_RE = re.compile(
    r"\b(IMPORTANT|CRITICAL|MUST|NEVER|DO\s+NOT|ALWAYS|STRICTLY|ABSOLUTELY"
    r"|UNDER\s+NO\s+CIRCUMSTANCE)\b"
    r"|^(Guidelines|Rules|Constraints|Standards|Instructions|Requirements):",
    re.M | re.X,
)
LIST_ITEM_RE = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+(.*)$", re.M)
MCP_TOOL_PREFIX_RE = re.compile(r"^mcp__([^_]+(?:_[^_]+)*?)__")


def _finding(check, repo, score, evidence, confidence, suggested_patch):
    return {
        "pattern": check,
        "repo": str(repo),
        "score": round(min(max(score, 0.0), 1.0), 3),
        "evidence": evidence,
        "attribution": "developer",
        "confidence": confidence,
        "alert_level": "passive_insight",
        "suggested_intervention": suggested_patch,
        "suggested_patch": suggested_patch,
        "intervention_class": "D-patch",
        "tier": "T0",
    }


def _read_text(path):
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _instruction_files(repo):
    files = []
    for name in INSTRUCTION_FILENAMES:
        path = repo / name
        if path.is_file():
            files.append(path)
    return files


# ------------------------------------------------------------------ checks

def lint_instruction_file_embedded_scans(repo):
    findings = []
    for path in _instruction_files(repo):
        matches = [match.group(0).strip()[:EXCERPT_LEN]
                   for match in SCAN_CMD_RE.finditer(_read_text(path))]
        if matches:
            findings.append(_finding(
                "instruction_file_embedded_scans", repo, 0.5,
                [f"file={path.name}", f"scan_command_count={len(matches)}"]
                + [f"example={s}" for s in matches[:3]],
                "low",
                f"{path.name} tells the agent to run repo-wide discovery "
                "commands on every session. Replace the scan with the "
                "distilled answer (the file list or map itself).",
            ))
    return findings


def lint_bloated_instruction_files(repo):
    findings = []
    for path in _instruction_files(repo):
        text = _read_text(path)
        line_count = text.count("\n") + 1 if text else 0
        directive_count = len(DIRECTIVE_RE.findall(text))
        if line_count <= BLOAT_LINE_LIMIT and directive_count <= DIRECTIVE_LIMIT:
            continue
        findings.append(_finding(
            "bloated_instruction_files", repo,
            0.5 + 0.2 * ((line_count > BLOAT_LINE_LIMIT)
                         + (directive_count > DIRECTIVE_LIMIT)),
            [f"file={path.name}", f"line_count={line_count}",
             f"directive_marker_count={directive_count}"],
            "medium",
            f"{path.name} has {line_count} lines and {directive_count} "
            "directive markers. Long instruction files get skimmed, not "
            "followed. Cut to the constraints that actually change agent "
            "behavior; move reference material to docs/.",
        ))
    return findings


def lint_verbose_verification_checklists(repo):
    findings = []
    for path in _instruction_files(repo):
        text = _read_text(path)
        list_items = "\n".join(match.group(1) for match in LIST_ITEM_RE.finditer(text))
        directive_count = len(DIRECTIVE_RE.findall(list_items))
        if directive_count <= DIRECTIVE_LIMIT:
            continue
        findings.append(_finding(
            "verbose_verification_checklists", repo, 0.5,
            [f"file={path.name}",
             f"directive_markers_in_list_items={directive_count}"],
            "low",
            f"{path.name} encodes verification as {directive_count} MUST/NEVER "
            "checklist items. Convert the checkable ones into hooks or CI "
            "gates; prose directives don't enforce themselves.",
        ))
    return findings


def lint_incomplete_ignore_configuration(repo):
    if (repo / ".claudeignore").is_file():
        return []
    present = [name for name in IGNORE_WORTHY_DIRS if (repo / name).is_dir()]
    if not present:
        return []
    return [_finding(
        "incomplete_ignore_configuration", repo, 0.6,
        ["claudeignore_present=False", f"heavy_dirs={','.join(present)}"],
        "medium",
        "Repo contains " + ", ".join(f"{d}/" for d in present)
        + " but no .claudeignore. Add one so searches and reads skip "
        "generated content:\n\n```\n"
        + "\n".join(f"{d}/" for d in present) + "\n```",
    )]


def _configured_mcp_servers(repo, home):
    servers = {}
    for path in (home / ".claude" / "settings.json",
                 repo / ".claude" / "settings.json",
                 repo / ".mcp.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        mcp = data.get("mcpServers")
        if isinstance(mcp, dict):
            for name in mcp:
                servers[name] = str(path)
    return servers


def _invoked_mcp_servers(sessions):
    invoked = set()
    for features, _metadata in sessions:
        for event in features.tool_events:
            match = MCP_TOOL_PREFIX_RE.match(event.name)
            if match:
                invoked.add(match.group(1))
    return invoked


def lint_always_on_mcp_servers(repo, sessions, home=None):
    configured = _configured_mcp_servers(repo, home or Path.home())
    if not configured:
        return []
    invoked = _invoked_mcp_servers(sessions)
    unused = sorted(name for name in configured
                    if not any(name.startswith(hit) or hit.startswith(name)
                               for hit in invoked))
    if not unused:
        return []
    return [_finding(
        "always_on_mcp_servers", repo, 0.5,
        [f"configured_servers={len(configured)}",
         f"never_invoked={','.join(unused)}"],
        "medium",
        "MCP servers " + ", ".join(unused) + " are configured but were never "
        "invoked in the scanned sessions. Every always-on server adds its "
        "tool schemas to every prompt. Disable them or scope them to the "
        "projects that use them.",
    )]


def lint_broken_hook_configuration(repo, sessions):
    samples = []
    session_ids = set()
    for features, metadata in sessions:
        if metadata.get("project") != str(repo):
            continue
        for sample in features.hook_error_samples:
            session_ids.add(features.session_id)
            if len(samples) < 3:
                samples.append(sample[:EXCERPT_LEN])
    if not samples:
        return []
    return [_finding(
        "broken_hook_configuration", repo,
        0.6 + min(len(session_ids), 4) * 0.05,
        [f"sessions_with_hook_errors={len(session_ids)}"]
        + [f"example={s}" for s in samples],
        "high",
        "Hooks are erroring (invalid JSON / non-zero exit) on this repo. A "
        "broken hook silently blocks writes or pollutes every turn. Fix or "
        "remove the hook command shown in the evidence.",
    )]


# ------------------------------------------------------------------- entry

def run_config_lints(repo_paths, sessions, home=None):
    """repo_paths: unique cwd values from session metadata that exist on
    disk. sessions: (SessionFeatures, metadata) pairs for MCP/hook joins.
    home is injectable for tests; defaults to the real home directory."""
    findings = []
    for repo in repo_paths:
        repo = Path(repo)
        if not repo.is_dir():
            continue
        findings.extend(lint_instruction_file_embedded_scans(repo))
        findings.extend(lint_bloated_instruction_files(repo))
        findings.extend(lint_verbose_verification_checklists(repo))
        findings.extend(lint_incomplete_ignore_configuration(repo))
        findings.extend(lint_always_on_mcp_servers(repo, sessions, home=home))
        findings.extend(lint_broken_hook_configuration(repo, sessions))
    return findings


def render_config_report(findings, generated_at):
    lines = [
        "# AIDE Config Report",
        "",
        f"_Generated {generated_at}. Class D-patch findings: repo/config "
        "fixes that no prompt-time nudge can deliver. Nothing here is "
        "applied automatically._",
        "",
    ]
    if not findings:
        lines.append("No config issues found in the scanned repos.")
        return "\n".join(lines)

    by_repo = {}
    for finding in findings:
        by_repo.setdefault(finding["repo"], []).append(finding)

    for repo, group in sorted(by_repo.items()):
        lines += [f"## {repo}", ""]
        for finding in group:
            lines += [
                f"### `{finding['pattern']}`",
                "",
                f"- Confidence: `{finding['confidence']}`",
                "- Evidence:",
            ]
            lines += [f"  - {item}" for item in finding["evidence"]]
            lines += ["", "Suggested patch:", "", finding["suggested_patch"], ""]
    return "\n".join(lines)
