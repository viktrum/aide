import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path


VERIFY_RE = re.compile(
    r"\b("
    r"pytest|"
    r"npm\s+(?:run\s+)?(?:test|build|lint|typecheck)|"
    r"pnpm\s+(?:run\s+)?(?:test|build|lint|typecheck)|"
    r"go\s+test|"
    r"cargo\s+(?:test|check)|"
    r"jest|vitest|playwright(?:\s+test)?|"
    r"mvn|rspec|tox|make\s+test|"
    r"lint|typecheck|tsc"
    r")\b",
    re.I,
)
PLAN_RE = re.compile(
    r"/(plan|architect|ask|think|spec|goal)\b|"
    r"\b(plan|approach|strategy|steps|breakdown|before\s+(coding|editing)|"
    r"do\s+not\s+edit\s+yet)\b",
    re.I,
)
REVIEW_RE = re.compile(
    r"/review\b|\b(review|diff|inspect|show\s+changes|fresh\s+review|second\s+pass)\b",
    re.I,
)
CLEAR_RE = re.compile(
    r"/clear\b|/compact\b|\b(clear\s+context|compact\s+context|new\s+session|"
    r"fresh\s+session|fork)\b",
    re.I,
)
TYPO_RE = re.compile(
    r"\b(i\s+meant|typo|mistyped|misspelled|my\s+bad|mb|sry\s+meant)\b",
    re.I,
)
STATUS_ACK_RE = re.compile(
    r"^\s*(go\s+ahead|continue|cont|status\s*\??|proceed|yes|yep|ok(?:ay)?|"
    r"sure|next|carry\s+on|keep\s+going|lgtm|approved?|"
    r"sounds\s+good|great|thanks?|done\s*\??)\s*[.!?]*\s*$",
    re.I,
)
VAGUE_RE = re.compile(
    r"^\s*(fix\s+(this|it)|make\s+it\s+work|improve\s+(this|it)|"
    r"clean\s+(this|it)\s+up|handle\s+(this|it)|do\s+it|"
    r"take\s+care\s+of\s+it|make\s+it\s+better|optimize\s+(this|it)|"
    r"refactor\s+(this|it)|check\s+(this|it)|look\s+into\s+(this|it))"
    r"\s*[.!?]*\s*$",
    re.I,
)
VAGUE_NEGATIVE_RE = re.compile(
    r"\b(file|function|component|module|test|expected|actual|error|constraint|"
    r"do\s+not|only|success|done\s+when|validate|verify|run|input|output|"
    r"example|acceptance\s+criteria)\b",
    re.I,
)
IMPERATIVE_RE = re.compile(
    r"\b(implement|code|build|add|change|modify|edit|refactor|rewrite|create|"
    r"delete|remove|rename|migrate|integrate|fix|run|test|verify|review|"
    r"check|inspect|read)\b",
    re.I,
)
TASK_SPLIT_RE = re.compile(
    r"\b(and then|after that|then also|also|additionally|plus)\b|;|"
    r"\n\s*[-*]|\n\s*\d+[.)]",
    re.I,
)
FILE_REF_RE = re.compile(
    r"[\w./\\-]+\.(py|js|ts|tsx|jsx|go|rs|java|kt|kts|swift|rb|php|cs|"
    r"cpp|c|h|hpp|md|json|yaml|yml|toml|css|scss|html|sql|sh|bash|zsh)\b"
    r"|/[\w./-]{3,}",
    re.I,
)
ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff]")
REPEATED_CHAR_RE = re.compile(r"([^\s])\1{2,}")
CONTINUATION_SUMMARY_RE = re.compile(
    r"^\s*this session is being continued from a previous conversation",
    re.I,
)
# R33 — exact literal Claude Code writes on user interrupts.
INTERRUPT_RE = re.compile(r"\[Request interrupted by user( for tool use)?\]")
# R35 — slash commands are logged as XML-tagged user events.
SLASH_CMD_RE = re.compile(r"<command-name>(/[\w-]+)</command-name>")
# R36 — session handoff ritual language.
HANDOFF_RE = re.compile(
    r"\b(next\s+session\s+(prompt|handoff|handover|brief)|"
    r"session\s+hand(-|\s)?(off|over)|"
    r"continuation\s+prompt|"
    r"prepare\s+for\s+next\s+session|"
    r"what'?s\s+done,?\s+what'?s\s+(left|pending|remaining)|"
    r"update\s+all\s+docs.{0,40}next\s+session)\b",
    re.I,
)
# R40 — rate limit / quota interruption.
RATE_LIMIT_RE = re.compile(
    r"\b(rate\s+limit(ed)?|usage\s+limit\s+(reached|hit)|5-?hour\s+limit|"
    r"quota\s+(exceeded|reached)|limit\s+resets?\s+at|out\s+of\s+(credits|usage))\b",
    re.I,
)
# R38 — background task completion notification marker.
TASK_NOTIFICATION_RE = re.compile(r"<task-notification>")
# Web research tools (built-in and MCP browser/search servers).
WEB_TOOL_RE = re.compile(r"^(WebFetch|WebSearch)$|^mcp__.*(fetch|search|browser)", re.I)
# Lexical error scan for tool results that lack a structured is_error flag.
RESULT_ERROR_RE = re.compile(r"\b(error|failed)\b", re.I)
# Broken hook infrastructure markers (config lint 18.7).
HOOK_ERROR_RE = re.compile(
    r"hook\b.{0,80}?(returned invalid JSON|failed|non-zero exit|error)"
    r"|returned invalid JSON",
    re.I | re.S,
)
HOOK_ERROR_SAMPLE_CAP = 5

READ_TOOLS = {"Read", "Grep", "Glob", "LS"}
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
DEFAULT_CONTEXT_WINDOW = 200_000


class MalformedSessionEntry(ValueError):
    pass


def _validate_entry_shape(path, entry_index, entry):
    line_no = entry_index + 1
    if not isinstance(entry, dict):
        raise MalformedSessionEntry(
            f"{path}:{line_no}: expected JSON object, "
            f"got {type(entry).__name__}"
        )
    if "message" in entry and not isinstance(entry["message"], dict):
        raise MalformedSessionEntry(
            f"{path}:{line_no}: expected message object, "
            f"got {type(entry['message']).__name__}"
        )


@dataclass
class UserPromptFeature:
    entry_index: int
    turn_index: int
    text: str
    text_norm: str
    word_count: int
    imperative_count: int
    has_file_ref: bool
    is_vague: bool
    has_plan_marker: bool
    has_verify_marker: bool
    has_review_marker: bool
    has_clear_marker: bool
    has_typo_marker: bool
    is_status_ack: bool
    question_mark_count: int = 0
    has_handoff_marker: bool = False
    prefix_signature: str = ""
    timestamp: str = ""


@dataclass
class ToolEventFeature:
    entry_index: int
    name: str
    input_text: str
    output_text: str
    is_error: bool
    is_read: bool
    is_edit: bool
    is_verification: bool
    output_chars: int
    tool_id: str = ""


@dataclass
class SessionFeatures:
    session_id: str
    path: str
    user_prompts: list[UserPromptFeature] = field(default_factory=list)
    tool_events: list[ToolEventFeature] = field(default_factory=list)
    total_tokens: int = 0
    max_context_tokens: int = 0
    context_window: int = DEFAULT_CONTEXT_WINDOW
    file_read_count: int = 0
    file_edit_count: int = 0
    files_read_before_first_edit: int = 0
    tool_output_chars_before_first_edit: int = 0
    verification_command_count: int = 0
    review_marker_count: int = 0
    planning_marker_count: int = 0
    clear_or_compact_marker_count: int = 0
    typo_marker_count: int = 0
    # v3 fields (see the AIDE design notes §15)
    first_timestamp: str = ""
    last_timestamp: str = ""
    interrupt_count: int = 0
    interrupt_entry_indexes: list[int] = field(default_factory=list)
    compaction_continuation_count: int = 0
    slash_commands: list[str] = field(default_factory=list)
    permission_modes: list[str] = field(default_factory=list)
    away_summary_count: int = 0
    api_error_count: int = 0
    is_subagent: bool = False
    sidechain_event_count: int = 0
    model_ids: list[str] = field(default_factory=list)
    cache_read_series: list[int] = field(default_factory=list)
    bash_commands: list[dict] = field(default_factory=list)
    web_tool_runs: list[int] = field(default_factory=list)
    rate_limit_marker_seen: bool = False
    hook_error_samples: list[str] = field(default_factory=list)

    @property
    def user_prompt_count(self):
        return len(self.user_prompts)

    @property
    def context_pct(self):
        if not self.context_window:
            return 0.0
        return self.max_context_tokens / self.context_window

    @property
    def wall_clock_minutes(self):
        if not self.first_timestamp or not self.last_timestamp:
            return 0.0
        try:
            from datetime import datetime
            start = datetime.fromisoformat(self.first_timestamp.replace("Z", "+00:00"))
            end = datetime.fromisoformat(self.last_timestamp.replace("Z", "+00:00"))
        except ValueError:
            return 0.0
        return max(0.0, (end - start).total_seconds() / 60)

    @property
    def auto_mode_active(self):
        return bool(self.permission_modes) and self.permission_modes[-1] == "auto"

    @property
    def clear_command_count(self):
        return self.slash_commands.count("/clear")

    @property
    def compact_command_count(self):
        return self.slash_commands.count("/compact")

    @property
    def model_switch_count(self):
        return self.slash_commands.count("/model")


def normalize_for_detection(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = ZERO_WIDTH_RE.sub("", text)
    text = text.lower()
    text = text.replace("’", "'").replace("`", "'")
    text = REPEATED_CHAR_RE.sub(r"\1\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _as_int(value):
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _json_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _content_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if content.get("type") == "tool_result":
            return ""
        return content.get("text", "") if content.get("type") == "text" else ""
    if not isinstance(content, list):
        return ""
    if any(isinstance(item, dict) and item.get("type") == "tool_result"
           for item in content):
        return ""
    chunks = []
    for item in content:
        if isinstance(item, str):
            chunks.append(item)
        elif isinstance(item, dict) and item.get("type") == "text":
            chunks.append(item.get("text", ""))
    return " ".join(chunk for chunk in chunks if chunk)


def _tool_result_content_text(raw):
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        chunks = []
        for item in raw:
            if isinstance(item, str):
                chunks.append(item)
            elif isinstance(item, dict):
                chunks.append(item.get("text") or item.get("content") or "")
        return "\n".join(chunk for chunk in chunks if chunk)
    return _json_text(raw)


def _tool_results(content):
    if isinstance(content, dict):
        items = [content]
    elif isinstance(content, list):
        items = content
    else:
        return
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "tool_result":
            continue
        yield {
            "tool_use_id": item.get("tool_use_id") or item.get("id") or "",
            "text": _tool_result_content_text(item.get("content", "")),
            "is_error": bool(item.get("is_error")),
        }


def _iter_tool_uses(entry):
    if entry.get("type") != "assistant":
        return
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, dict):
        items = [content]
    elif isinstance(content, list):
        items = content
    else:
        return
    for item in items:
        if isinstance(item, dict) and item.get("type") == "tool_use":
            yield item


def _usage_tokens(entry):
    if entry.get("type") != "assistant":
        return 0, 0
    usage = (entry.get("message") or {}).get("usage") or {}
    input_tokens = _as_int(usage.get("input_tokens"))
    output_tokens = _as_int(usage.get("output_tokens"))
    cache_read = _as_int(usage.get("cache_read_input_tokens"))
    cache_create = _as_int(usage.get("cache_creation_input_tokens"))
    total_tokens = input_tokens + output_tokens + cache_read + cache_create
    context_tokens = input_tokens + cache_read + cache_create
    return total_tokens, context_tokens


def _context_window(entry):
    model = ((entry.get("message") or {}).get("model") or "").lower()
    if "1m" in model or "[1m]" in model:
        return 1_000_000
    return DEFAULT_CONTEXT_WINDOW


def _is_meta_entry(entry, text):
    message = entry.get("message") or {}
    if any(entry.get(key) for key in ("isMeta", "is_meta", "meta")):
        return True
    if any(message.get(key) for key in ("isMeta", "is_meta", "meta")):
        return True
    if CONTINUATION_SUMMARY_RE.search(text):
        return True
    return text.strip().startswith("<")


def _real_user_prompt_text(entry):
    if entry.get("type") != "user":
        return ""
    content = (entry.get("message") or {}).get("content")
    text = _content_text(content).strip()
    if not text or _is_meta_entry(entry, text):
        return ""
    return text


def _imperative_count(text_norm, is_status_ack):
    if is_status_ack:
        return 0
    verb_count = len(IMPERATIVE_RE.findall(text_norm))
    split_count = len([
        part for part in TASK_SPLIT_RE.split(text_norm)
        if isinstance(part, str) and len(part.split()) > 3
    ])
    return max(1, verb_count, split_count)


def _prompt_feature(entry_index, turn_index, text, timestamp=""):
    text_norm = normalize_for_detection(text)
    is_status_ack = bool(STATUS_ACK_RE.match(text_norm))
    is_vague = (
        not is_status_ack
        and bool(VAGUE_RE.match(text_norm))
        and not bool(VAGUE_NEGATIVE_RE.search(text_norm))
    )
    stripped = text.strip()
    return UserPromptFeature(
        entry_index=entry_index,
        turn_index=turn_index,
        text=stripped,
        text_norm=text_norm,
        word_count=len(re.findall(r"[a-z0-9']+", text_norm)),
        imperative_count=_imperative_count(text_norm, is_status_ack),
        has_file_ref=bool(FILE_REF_RE.search(text)),
        is_vague=is_vague,
        has_plan_marker=bool(PLAN_RE.search(text_norm)),
        has_verify_marker=bool(VERIFY_RE.search(text_norm)),
        has_review_marker=bool(REVIEW_RE.search(text_norm)),
        has_clear_marker=bool(CLEAR_RE.search(text_norm)),
        has_typo_marker=bool(TYPO_RE.search(text_norm)),
        is_status_ack=is_status_ack,
        question_mark_count=stripped.count("?"),
        has_handoff_marker=bool(HANDOFF_RE.search(text_norm)),
        prefix_signature=re.sub(r"\d+", "#", stripped[:30]),
        timestamp=timestamp,
    )


def _attach_tool_result(event_queue, events_by_id, result):
    tool_use_id = result["tool_use_id"]
    if tool_use_id:
        event = events_by_id.get(tool_use_id)
        if event is None:
            return
    else:
        while event_queue and event_queue[0].output_text:
            event_queue.pop(0)
        event = event_queue[0] if event_queue else None
    if event is None:
        return
    event.output_text = result["text"]
    event.is_error = result["is_error"]
    event.output_chars = len(result["text"])


def build_session_features(path) -> SessionFeatures:
    path = Path(path)
    features = SessionFeatures(session_id=path.stem, path=str(path))
    features.is_subagent = (
        path.name.startswith("agent-") or path.parent.name == "subagents"
    )

    first_edit_seen = False
    event_queue = []
    events_by_id = {}
    last_result_was_error = False
    web_run = 0

    def flush_web_run():
        nonlocal web_run
        if web_run:
            features.web_tool_runs.append(web_run)
            web_run = 0

    with open(path, encoding="utf-8", errors="replace") as handle:
        for entry_index, line in enumerate(handle):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            _validate_entry_shape(path, entry_index, entry)

            timestamp = entry.get("timestamp") or ""
            if timestamp:
                if not features.first_timestamp:
                    features.first_timestamp = timestamp
                features.last_timestamp = timestamp
            if entry.get("isSidechain"):
                features.sidechain_event_count += 1
                features.is_subagent = True

            entry_type = entry.get("type")
            if entry_type == "permission-mode":
                mode = entry.get("permissionMode")
                if mode:
                    features.permission_modes.append(mode)
                continue
            if entry_type == "system":
                subtype = entry.get("subtype")
                if subtype == "away_summary":
                    features.away_summary_count += 1
                elif subtype == "api_error":
                    features.api_error_count += 1
                system_text = _json_text(entry.get("content") or entry.get("message"))
                if (len(features.hook_error_samples) < HOOK_ERROR_SAMPLE_CAP
                        and HOOK_ERROR_RE.search(system_text)):
                    features.hook_error_samples.append(system_text[:300])
                continue

            step_tokens, context_tokens = _usage_tokens(entry)
            features.total_tokens += step_tokens
            features.max_context_tokens = max(features.max_context_tokens, context_tokens)
            if entry_type == "assistant":
                features.context_window = max(features.context_window, _context_window(entry))
                model = (entry.get("message") or {}).get("model")
                if model and model not in features.model_ids:
                    features.model_ids.append(model)
                usage = (entry.get("message") or {}).get("usage") or {}
                if usage:
                    features.cache_read_series.append(_as_int(usage.get("cache_read_input_tokens")))

            if entry_type == "user":
                raw_text = _content_text((entry.get("message") or {}).get("content")).strip()
                if raw_text:
                    if RATE_LIMIT_RE.search(raw_text):
                        features.rate_limit_marker_seen = True
                    if (len(features.hook_error_samples) < HOOK_ERROR_SAMPLE_CAP
                            and HOOK_ERROR_RE.search(raw_text)):
                        features.hook_error_samples.append(raw_text[:300])
                    if INTERRUPT_RE.search(raw_text):
                        features.interrupt_count += 1
                        features.interrupt_entry_indexes.append(entry_index)
                    elif CONTINUATION_SUMMARY_RE.search(raw_text):
                        features.compaction_continuation_count += 1
                    else:
                        tagged = SLASH_CMD_RE.findall(raw_text)
                        if tagged:
                            features.slash_commands.extend(tagged)
                        elif raw_text.startswith("/"):
                            features.slash_commands.append(raw_text.split()[0])
                        if not _is_meta_entry(entry, raw_text):
                            flush_web_run()
                            prompt = _prompt_feature(
                                entry_index, len(features.user_prompts), raw_text, timestamp)
                            features.user_prompts.append(prompt)
                            features.planning_marker_count += int(prompt.has_plan_marker)
                            features.review_marker_count += int(prompt.has_review_marker)
                            features.clear_or_compact_marker_count += int(prompt.has_clear_marker)
                            features.typo_marker_count += int(prompt.has_typo_marker)

            content = (entry.get("message") or {}).get("content")
            for result in _tool_results(content):
                if result["text"] and not first_edit_seen:
                    features.tool_output_chars_before_first_edit += len(result["text"])
                last_result_was_error = (
                    result["is_error"]
                    or bool(RESULT_ERROR_RE.search(result["text"][:200]))
                )
                _attach_tool_result(event_queue, events_by_id, result)

            for tool_use in _iter_tool_uses(entry):
                name = tool_use.get("name", "")
                tool_input = tool_use.get("input", {})
                input_text = _json_text(tool_input)
                is_read = name in READ_TOOLS
                is_edit = name in EDIT_TOOLS
                event = ToolEventFeature(
                    entry_index=entry_index,
                    name=name,
                    input_text=input_text,
                    output_text="",
                    is_error=False,
                    is_read=is_read,
                    is_edit=is_edit,
                    is_verification=bool(VERIFY_RE.search(input_text)),
                    output_chars=0,
                    tool_id=tool_use.get("id", ""),
                )
                features.tool_events.append(event)
                event_queue.append(event)
                if event.tool_id:
                    events_by_id[event.tool_id] = event
                features.file_read_count += int(is_read)
                features.file_edit_count += int(is_edit)
                features.verification_command_count += int(event.is_verification)
                if is_read and not first_edit_seen:
                    features.files_read_before_first_edit += 1
                if is_edit:
                    first_edit_seen = True

                if name == "Bash" and isinstance(tool_input, dict):
                    command = tool_input.get("command") or ""
                    if command:
                        features.bash_commands.append({
                            "entry_index": entry_index,
                            "norm": re.sub(r"\s+", " ", command).strip(),
                            "after_error": last_result_was_error,
                        })
                if WEB_TOOL_RE.search(name):
                    web_run += 1
                else:
                    flush_web_run()

    flush_web_run()
    return features
