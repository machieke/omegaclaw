import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

try:
    import httpx
except Exception:
    httpx = None

_ENSURED_MODELS = set()
_PREWARMED_MODELS = set()
_OLLAMA_CLIENT = None
_OLLAMA_CLIENT_BASE_URL = ""
_OLLAMA_CLIENT_LOCK = threading.Lock()
_PREWARM_LOCK = threading.Lock()
_PREWARM_THREAD_STARTED = False
_SKILL_START_RE = re.compile(
    r"^\s*\(\s*(\(\s*)?(remember|query|pin|shell|read-file|write-file|append-file|send|search|metta|join-channel|join|leave-channel|leave)\b"
)
_SEND_STR_RE = re.compile(r'(\(\s*send\s+")((?:\\.|[^"\\])*)(")', re.DOTALL)
_META_REPLY_RE = re.compile(
    r"(continuous loop|self-chosen long-term goals|initial memory state|current goal|task context|"
    r"follow the instructions|use send commands to keep people engaged)",
    re.IGNORECASE,
)
_LIKE_STMT_RE = re.compile(r"^\s*i\s+like\s+(.+?)\s*[\.\!\?]*\s*$", re.IGNORECASE)
_LIKE_QUERY_RE = re.compile(r"^\s*what\s+do\s+i\s+like(?:\s+to\s+eat)?\s*\??\s*$", re.IGNORECASE)
_DETAIL_REQUEST_RE = re.compile(
    r"\b(recipe|ingredients|steps?|instructions?|how to|how do i|explain|why|guide|tutorial|"
    r"compare|pros|cons|plan|list|write|draft|example|code|script)\b",
    re.IGNORECASE,
)
_RECIPE_REQUEST_RE = re.compile(r"\b(recipe|ingredients|cupcake|cake|cookies?|bake)\b", re.IGNORECASE)
_LONG_FORM_REQUEST_RE = re.compile(
    r"\b(long form|long-form|longer|detailed|in detail|step by step|full answer|full recipe|complete|elaborate|expanded)\b",
    re.IGNORECASE,
)
_CREATIVE_REQUEST_RE = re.compile(r"\b(joke|story|poem|riddle|haiku|funny)\b", re.IGNORECASE)
_EXPLICIT_SEARCH_RE = re.compile(r"^\s*(search(?:\s+for)?|look\s+up)\b", re.IGNORECASE)
_LIVE_LOOKUP_RE = re.compile(r"\b(weather|temperature|forecast|right now|today|latest|current)\b", re.IGNORECASE)
_USE_MODEL_RE = re.compile(r"^\s*use\s+model\s+(.+?)\s*$", re.IGNORECASE)
_CURRENT_MODEL_RE = re.compile(
    r"^\s*(?:which|what)\s+(?:is\s+)?(?:the\s+)?(?:current|active)\s+model\??\s*$"
    r"|^\s*(?:which|what)\s+model\s+(?:are\s+you\s+using|is\s+active)\??\s*$"
    r"|^\s*(?:current|active)\s+model\??\s*$",
    re.IGNORECASE,
)
_JOIN_CHANNEL_REQ_RE = re.compile(r"^join(?:-channel|\s+channel)?\s+(.+?)$", re.IGNORECASE)
_LEAVE_CHANNEL_REQ_RE = re.compile(r"^leave(?:-channel|\s+channel)?\s+(.+?)$", re.IGNORECASE)
_MODEL_LIST_RE = re.compile(
    r"(?:\b(?:list|show)\s+(?:ollama\s+)?models?\b|\bollama\s+list\b|\bwhat\s+models?\b)",
    re.IGNORECASE,
)
_SEARCH_RESULT_ITEM_RE = re.compile(r"\(TITLE:\s*(.*?)\s+SNIPPET:\s*(.*?)\)\s*", re.DOTALL)
_ASYNC_DISPATCH_LOCK = threading.Lock()
_ASYNC_DISPATCH_EXECUTOR = None
_ASYNC_DISPATCH_WORKERS = 0
_SENDER_LOCKS_LOCK = threading.Lock()
_SENDER_LOCKS = {}
_ACTIVE_CHAT_MODEL_LOCK = threading.Lock()
_ACTIVE_CHAT_MODEL = ""


def _balanced_parentheses(text):
    depth = 0
    in_string = False
    escaped = False

    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return False

    return depth == 0 and not in_string


def _looks_like_skill_output(text):
    stripped = str(text or "").strip()
    if not stripped:
        return False
    if not stripped.startswith("(") or not stripped.endswith(")"):
        return False
    if not _balanced_parentheses(stripped):
        return False
    return _SKILL_START_RE.match(stripped) is not None


def _shorten_text(text, max_chars):
    compact = " ".join(str(text or "").split())
    if len(compact) <= max_chars:
        return compact
    if max_chars <= 3:
        return compact[:max_chars]
    return compact[: max_chars - 3] + "..."


def _condense_detail_text(text, max_chars):
    compact = " ".join(str(text or "").split())
    if not compact:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", compact)
    picked = []
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        candidate = " ".join(picked + [sentence]).strip()
        if len(candidate) > max_chars:
            break
        picked.append(sentence)
        if len(picked) >= 2:
            break
    if picked:
        return " ".join(picked).strip()
    return _shorten_text(compact, max_chars)


def _trim_incomplete_tail(text):
    compact = " ".join(str(text or "").split()).strip()
    if not compact:
        return ""
    if compact[-1:] in {".", "!", "?"}:
        return compact
    last_punct = max(compact.rfind("."), compact.rfind("!"), compact.rfind("?"))
    if last_punct >= 40:
        return compact[: last_punct + 1].strip()
    fallback_break = max(compact.rfind(","), compact.rfind(";"), compact.rfind(":"))
    if fallback_break >= 60:
        return compact[:fallback_break].strip()
    if compact.endswith('"') and compact.count('"') % 2 == 1:
        return compact[:-1].rstrip()
    return compact


def _clamp_send_payloads(skill_text, max_send_chars):
    def repl(match):
        payload = match.group(2)
        try:
            decoded = json.loads(f'"{payload}"')
        except Exception:
            decoded = payload
        shortened = _shorten_text(decoded, max_send_chars)
        reescaped = json.dumps(shortened)[1:-1]
        return f"{match.group(1)}{reescaped}{match.group(3)}"

    return _SEND_STR_RE.sub(repl, skill_text)


def balance_parentheses(s):
    s = s.strip()
    left = 0
    while left < len(s) and s[left] == '(':
        left += 1
    right = 0
    while right < len(s) and s[len(s) - 1 - right] == ')':
        right += 1
    core = s[left:len(s) - right if right else len(s)].strip()
    return f"(({core}))"


def _strip_user_prefix(msg):
    text = str(msg or "").strip()
    if ": " in text:
        head, tail = text.split(": ", 1)
        if head and " " not in head:
            return tail.strip()
    return text


def _split_sender_and_text(msg):
    text = str(msg or "").strip()
    if ": " in text:
        head, tail = text.split(": ", 1)
        if head and " " not in head:
            return head.strip(), tail.strip()
    return "", text


def _trim_user_text(text):
    msg = " ".join(str(text or "").split()).strip()
    if not msg:
        return ""
    max_words = max(1, _int_env("OLLAMA_DIRECT_MAX_USER_WORDS", 24))
    words = msg.split()
    if len(words) > max_words:
        msg = " ".join(words[:max_words]).strip()
    max_chars = max(32, _int_env("OLLAMA_DIRECT_MAX_USER_CHARS", 320))
    if len(msg) > max_chars:
        msg = msg[:max_chars].rstrip()
    return msg


def _extract_user_parts(user_msg):
    text = str(user_msg or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in text.split(" | ") if p.strip()]
    return parts if parts else [text]


def _decode_special_tokens(text):
    return (
        str(text or "")
        .replace("_apostrophe_", "'")
        .replace("_quote_", '"')
        .replace("_newline_", "\n")
    )


def _normalize_subject_key(text):
    key = " ".join(str(text or "").replace("’", "'").split()).strip().lower()
    if key.endswith("'s"):
        key = key[:-2].strip()
    if key.endswith("s'"):
        key = key[:-2].strip()
    return key


def _parse_named_fact(text):
    raw = " ".join(_decode_special_tokens(text).replace("’", "'").split()).strip()
    if not raw:
        return None
    lower = raw.lower()
    if lower.startswith("remember "):
        raw = raw[9:].strip()
        lower = raw.lower()
    if not lower.startswith("my "):
        return None
    body = raw[3:].strip()
    body_lower = body.lower()
    for marker in (" is called ", " is named ", " name is ", " was called ", " was named ", " name was "):
        idx = body_lower.find(marker)
        if idx <= 0:
            continue
        subject = _normalize_subject_key(body[:idx].strip(" '\""))
        value = body[idx + len(marker) :].strip(" '\"").rstrip(".!?")
        if subject and value:
            return subject, value
    return None


def _parse_named_query(text):
    raw = " ".join(_decode_special_tokens(text).replace("’", "'").split()).strip().rstrip(".!?")
    if not raw:
        return ""
    lower = raw.lower()
    prefixes = ("what is my ", "what's my ", "what was my ")
    for prefix in prefixes:
        if not lower.startswith(prefix):
            continue
        tail = lower[len(prefix) :].strip()
        if tail.endswith("'s name"):
            return _normalize_subject_key(tail[: -len("'s name")])
        for pronoun in ("his", "her", "their", "its"):
            marker = f" {pronoun} name"
            if tail.endswith(marker):
                return _normalize_subject_key(tail[: -len(marker)])
        if tail.endswith(" name"):
            return _normalize_subject_key(tail[: -len(" name")])
        return ""
    for alt in ("what is the name of my ", "what was the name of my "):
        if lower.startswith(alt):
            return _normalize_subject_key(lower[len(alt) :])
    return ""


def _parse_status_fact(text):
    raw = " ".join(_decode_special_tokens(text).replace("’", "'").split()).strip().rstrip(".!?")
    if not raw:
        return None
    lower = raw.lower()
    if lower.startswith("remember "):
        raw = raw[9:].strip()
        lower = raw.lower()
    if not lower.startswith("my "):
        return None
    body = raw[3:].strip()
    body_lower = body.lower()
    for marker in (" is ", " was "):
        idx = body_lower.find(marker)
        if idx <= 0:
            continue
        subject = _normalize_subject_key(body[:idx].strip(" '\""))
        status_raw = body[idx + len(marker) :].strip().lower()
        status_raw = status_raw.rstrip(".!?")
        if status_raw == "not alive":
            status_raw = "dead"
        if status_raw in {"alive", "dead"} and subject:
            return subject, status_raw
    return None


def _parse_status_query(text):
    raw = " ".join(_decode_special_tokens(text).replace("’", "'").split()).strip().rstrip(".!?")
    if not raw:
        return None
    lower = raw.lower()
    if not lower.startswith("is my "):
        return None
    tail = lower[len("is my ") :].strip()
    for asked in ("alive", "dead"):
        marker = f" {asked}"
        if not tail.endswith(marker):
            continue
        subject = _normalize_subject_key(tail[: -len(marker)])
        if subject.endswith(" still"):
            subject = _normalize_subject_key(subject[: -len(" still")])
        if subject:
            return subject, asked
    return None


def _is_detailed_request(text):
    return _DETAIL_REQUEST_RE.search(str(text or "")) is not None


def _is_recipe_request(text):
    return _RECIPE_REQUEST_RE.search(str(text or "")) is not None


def _is_long_form_request(text):
    return _LONG_FORM_REQUEST_RE.search(str(text or "")) is not None


def _is_creative_request(text):
    return _CREATIVE_REQUEST_RE.search(str(text or "")) is not None


def _max_send_chars_for_user_msg(user_msg):
    base_chars = _int_env("METTACLAW_MAX_SEND_CHARS", 280)
    detail_chars = _int_env("METTACLAW_DETAIL_MAX_SEND_CHARS", 700)
    long_chars = _int_env("METTACLAW_LONG_FORM_MAX_SEND_CHARS", 1800)
    creative_chars = _int_env("METTACLAW_CREATIVE_MAX_SEND_CHARS", 700)
    chosen = base_chars
    for part in _extract_user_parts(user_msg):
        msg = _trim_user_text(_strip_user_prefix(part))
        if not msg:
            continue
        if _is_long_form_request(msg) or _is_recipe_request(msg):
            chosen = max(chosen, long_chars)
            continue
        if _is_creative_request(msg):
            chosen = max(chosen, creative_chars)
            continue
        if _is_detailed_request(msg):
            chosen = max(chosen, detail_chars)
    if chosen < 64:
        return 64
    return chosen


def _load_chroma_backend():
    try:
        import lib_chromadb

        return lib_chromadb
    except Exception:
        pass
    here = os.path.dirname(__file__)
    if here and here not in sys.path:
        sys.path.append(here)
    try:
        import lib_chromadb

        return lib_chromadb
    except Exception:
        return None


def _load_websearch_backend():
    try:
        import websearch

        return websearch
    except Exception:
        pass
    here = os.path.dirname(__file__)
    channels_dir = os.path.join(os.path.dirname(here), "channels")
    if channels_dir and channels_dir not in sys.path:
        sys.path.append(channels_dir)
    try:
        import websearch

        return websearch
    except Exception:
        return None


def _load_channel_backend(module_name):
    try:
        return __import__(module_name)
    except Exception:
        pass
    here = os.path.dirname(__file__)
    channels_dir = os.path.join(os.path.dirname(here), "channels")
    if channels_dir and channels_dir not in sys.path:
        sys.path.append(channels_dir)
    try:
        return __import__(module_name)
    except Exception:
        return None


def _load_irc_backend():
    return _load_channel_backend("irc")


def _load_mattermost_backend():
    return _load_channel_backend("mattermost")


def _search_query_from_message(msg):
    text = str(msg or "").strip()
    if not text:
        return None
    lower = text.lower()
    if lower.startswith("search for "):
        return text[11:].strip() or None
    if lower.startswith("search "):
        return text[7:].strip() or None
    if lower.startswith("look up "):
        return text[8:].strip() or None
    if _LIVE_LOOKUP_RE.search(text):
        loc_match = re.search(r"\b(?:in|for)\s+(.+?)(?:\?|$)", text, re.IGNORECASE)
        location = " ".join((loc_match.group(1) if loc_match else text).split()).strip()
        if "temperature" in lower:
            return f"current temperature in {location} accuweather"
        if "weather" in lower or "forecast" in lower:
            return f"current weather in {location} accuweather"
        return text
    return None


def _available_model_names():
    try:
        tags = _get_json("/api/tags")
    except Exception:
        return []
    raw_models = tags.get("models")
    names = []
    if isinstance(raw_models, list):
        for item in raw_models:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if name:
                names.append(name)
    return names


def _model_base_name(model_name):
    return str(model_name or "").strip().lower().split(":", 1)[0]


def _resolve_requested_model(requested, names):
    req = str(requested or "").strip().lower()
    if not req or not names:
        return ""
    for name in names:
        if name.lower() == req:
            return name
    for name in names:
        if _model_base_name(name) == req:
            return name
    prefix_matches = []
    for name in names:
        full = name.lower()
        base = _model_base_name(name)
        if full.startswith(req) or base.startswith(req):
            prefix_matches.append(name)
    if prefix_matches:
        prefix_matches.sort(key=len)
        return prefix_matches[0]
    contains_matches = []
    for name in names:
        full = name.lower()
        base = _model_base_name(name)
        if req in full or req in base:
            contains_matches.append(name)
    if contains_matches:
        contains_matches.sort(key=len)
        return contains_matches[0]
    return ""


def _active_chat_model_default(model_hint=""):
    env_model = str(os.getenv("OLLAMA_MODEL", "") or "").strip()
    hint = str(model_hint or "").strip()
    return env_model or hint or "llama3.1:8b"


def _get_active_chat_model(model_hint=""):
    global _ACTIVE_CHAT_MODEL
    with _ACTIVE_CHAT_MODEL_LOCK:
        if not _ACTIVE_CHAT_MODEL:
            _ACTIVE_CHAT_MODEL = _active_chat_model_default(model_hint)
        return _ACTIVE_CHAT_MODEL


def _set_active_chat_model(model_name):
    global _ACTIVE_CHAT_MODEL
    chosen = str(model_name or "").strip()
    if not chosen:
        return
    with _ACTIVE_CHAT_MODEL_LOCK:
        _ACTIVE_CHAT_MODEL = chosen


def _parse_join_channel_target(text):
    msg = _decode_special_tokens(text).strip()
    while msg.startswith("(") and msg.endswith(")") and len(msg) > 2:
        inner = msg[1:-1].strip()
        if not inner:
            break
        msg = inner
    match = _JOIN_CHANNEL_REQ_RE.match(msg)
    if match is None:
        return ""
    channel = match.group(1).strip().strip('"').strip("'")
    if not channel:
        return ""
    if " " in channel:
        channel = channel.split()[0].strip()
    if not channel:
        return ""
    if not channel.startswith("#"):
        channel = f"#{channel.lstrip('#')}"
    return channel


def _parse_leave_channel_target(text):
    msg = _decode_special_tokens(text).strip()
    while msg.startswith("(") and msg.endswith(")") and len(msg) > 2:
        inner = msg[1:-1].strip()
        if not inner:
            break
        msg = inner
    match = _LEAVE_CHANNEL_REQ_RE.match(msg)
    if match is None:
        return ""
    channel = match.group(1).strip().strip('"').strip("'")
    if not channel:
        return ""
    if " " in channel:
        channel = channel.split()[0].strip()
    if not channel:
        return ""
    if not channel.startswith("#"):
        channel = f"#{channel.lstrip('#')}"
    return channel


def _join_channel_skill_from_user_message(user_msg):
    replies = []
    irc_backend = _load_irc_backend()
    for part in _extract_user_parts(user_msg):
        channel = _parse_join_channel_target(_strip_user_prefix(part))
        if not channel:
            continue
        joined = False
        if irc_backend is not None and hasattr(irc_backend, "join_channel"):
            try:
                joined = bool(irc_backend.join_channel(channel))
            except Exception:
                joined = False
        if joined:
            replies.append(f"Joined {channel}.")
        else:
            replies.append(f"Joining {channel}.")
        if len(replies) >= 3:
            break
    if not replies:
        return None
    cmds = " ".join(f"(send {json.dumps(reply)})" for reply in replies)
    return f"({cmds})"


def _leave_channel_skill_from_user_message(user_msg):
    replies = []
    irc_backend = _load_irc_backend()
    for part in _extract_user_parts(user_msg):
        channel = _parse_leave_channel_target(_strip_user_prefix(part))
        if not channel:
            continue
        left = False
        if irc_backend is not None and hasattr(irc_backend, "leave_channel"):
            try:
                left = bool(irc_backend.leave_channel(channel))
            except Exception:
                left = False
        if left:
            replies.append(f"Leaving {channel}.")
        else:
            replies.append(f"Leave requested for {channel}.")
        if len(replies) >= 3:
            break
    if not replies:
        return None
    cmds = " ".join(f"(send {json.dumps(reply)})" for reply in replies)
    return f"({cmds})"


def _model_control_skill_from_user_message(user_msg, max_send_chars):
    replies = []
    names = []
    for part in _extract_user_parts(user_msg):
        msg = _decode_special_tokens(_strip_user_prefix(part)).strip()
        if not msg:
            continue
        use_match = _USE_MODEL_RE.match(msg)
        if use_match:
            requested = use_match.group(1).strip().strip('"').strip("'").rstrip(".!?")
            if not names:
                names = _available_model_names()
            resolved = _resolve_requested_model(requested, names)
            if resolved:
                _set_active_chat_model(resolved)
                replies.append(_shorten_text(f"Using model {resolved}.", max_send_chars))
            else:
                if names:
                    preview = ", ".join(names[:8])
                    reply = f"Model '{requested}' not found. Available: {preview}"
                else:
                    reply = f"Model '{requested}' not found, and I couldn't fetch the Ollama model list."
                replies.append(_shorten_text(reply, max_send_chars))
            continue
        if _CURRENT_MODEL_RE.match(msg):
            current = _get_active_chat_model("")
            replies.append(_shorten_text(f"Current model is {current}.", max_send_chars))
    if not replies:
        return None
    cmds = " ".join(f"(send {json.dumps(reply)})" for reply in replies[:3])
    return f"({cmds})"


def _model_list_skill_from_user_message(user_msg, max_send_chars):
    replies = []
    for part in _extract_user_parts(user_msg):
        msg = _trim_user_text(_strip_user_prefix(part))
        if not _MODEL_LIST_RE.search(msg):
            continue
        try:
            names = _available_model_names()
            if names:
                reply = "Available models: " + ", ".join(names)
            else:
                reply = "No models are currently available in Ollama."
        except Exception:
            reply = "I couldn't list Ollama models right now."
        replies.append(_shorten_text(reply, max_send_chars))
        if len(replies) >= 3:
            break
    if not replies:
        return None
    cmds = " ".join(f"(send {json.dumps(reply)})" for reply in replies)
    return f"({cmds})"


def _search_items(raw):
    text = str(raw or "")
    items = []
    for match in _SEARCH_RESULT_ITEM_RE.finditer(text):
        title = " ".join(match.group(1).split()).strip()
        snippet = " ".join(match.group(2).split()).strip()
        if title or snippet:
            items.append((title, snippet))
    return items


def _best_search_snippet(query, raw):
    items = _search_items(raw)
    if not items:
        return ""
    query_text = str(query or "").lower()
    wants_weather = any(term in query_text for term in ("weather", "temperature", "forecast"))
    best_score = -1
    best_item = items[0]
    for title, snippet in items:
        text = f"{title} {snippet}".lower()
        score = 0
        if wants_weather:
            if "current weather" in text or "temperature" in text or "forecast" in text:
                score += 4
            if "fahrenheit" in text or "celsius" in text or "°" in text:
                score += 2
            if "accuweather" in text or "weather.com" in text or "national weather service" in text:
                score += 1
        if "current" in text or "right now" in text:
            score += 1
        if score > best_score:
            best_score = score
            best_item = (title, snippet)
    title, snippet = best_item
    if title and snippet:
        return f"{title}: {snippet}"
    return title or snippet


def _tool_skill_from_user_message(user_msg, max_send_chars):
    join_cmd = _join_channel_skill_from_user_message(user_msg)
    if join_cmd is not None:
        return join_cmd
    leave_cmd = _leave_channel_skill_from_user_message(user_msg)
    if leave_cmd is not None:
        return leave_cmd
    model_control = _model_control_skill_from_user_message(user_msg, max_send_chars)
    if model_control is not None:
        return model_control
    model_list = _model_list_skill_from_user_message(user_msg, max_send_chars)
    if model_list is not None:
        return model_list
    backend = _load_websearch_backend()
    if backend is None:
        return None
    replies = []
    for part in _extract_user_parts(user_msg):
        msg = _trim_user_text(_strip_user_prefix(part))
        query = _search_query_from_message(msg)
        if query is None:
            continue
        try:
            raw = backend.search(query, 5)
        except Exception:
            raw = ""
        snippet = _best_search_snippet(query, raw)
        if not snippet:
            snippet = "I couldn't fetch search results right now."
        replies.append(_shorten_text(snippet, max_send_chars))
        if len(replies) >= 3:
            break
    if not replies:
        return None
    cmds = " ".join(f"(send {json.dumps(reply)})" for reply in replies)
    return f"({cmds})"


def _remember_user_likes(user_msg):
    backend = _load_chroma_backend()
    if backend is None:
        return
    for part in _extract_user_parts(user_msg):
        sender, msg = _split_sender_and_text(part)
        if not sender:
            continue
        match = _LIKE_STMT_RE.match(msg)
        if match:
            liked = " ".join(match.group(1).split())
            if liked:
                content = f"{sender} likes {liked}"
                try:
                    emb = ollama_embedding(content)
                    backend.remember(content, emb, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"))
                except Exception:
                    pass
        parsed = _parse_named_fact(msg)
        if parsed is not None:
            subject, value = parsed
            content = f"{sender}::named::{subject}::{value}"
            try:
                emb = ollama_embedding(f"{sender} named {subject}")
                backend.remember(content, emb, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"))
            except Exception:
                pass
        status_parsed = _parse_status_fact(msg)
        if status_parsed is not None:
            subject, status = status_parsed
            content = f"{sender}::status::{subject}::{status}"
            try:
                emb = ollama_embedding(f"{sender} status {subject}")
                backend.remember(content, emb, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"))
            except Exception:
                pass


def _recall_user_likes(user_msg, max_send_chars):
    backend = _load_chroma_backend()
    if backend is None:
        return None
    for part in _extract_user_parts(user_msg):
        sender, msg = _split_sender_and_text(part)
        if not sender:
            continue
        status_query = _parse_status_query(msg)
        if status_query is not None:
            status_subject, asked_state = status_query
            query_text = f"{sender} status {status_subject}"
            try:
                emb = ollama_embedding(query_text)
                rows = backend.query(emb, 12)
            except Exception:
                return None
            prefix = f"{sender}::status::{status_subject}::"
            known_state = ""
            for row in rows:
                if not isinstance(row, list) or len(row) < 2:
                    continue
                content = str(row[1] or "").strip()
                if not content.lower().startswith(prefix.lower()):
                    continue
                known_state = content[len(prefix) :].strip().lower()
                if known_state in {"alive", "dead"}:
                    break
            if known_state in {"alive", "dead"}:
                if asked_state == "alive":
                    answer = f"Yes, your {status_subject} is alive." if known_state == "alive" else f"No, your {status_subject} is dead."
                else:
                    answer = f"Yes, your {status_subject} is dead." if known_state == "dead" else f"No, your {status_subject} is alive."
                reply = _shorten_text(answer, max_send_chars)
                return f"((send {json.dumps(reply)}))"
            unknown_reply = _shorten_text("I don't know.", max_send_chars)
            return f"((send {json.dumps(unknown_reply)}))"
        named_subject = _parse_named_query(msg)
        if named_subject:
            query_text = f"{sender} named {named_subject}"
            try:
                emb = ollama_embedding(query_text)
                rows = backend.query(emb, 12)
            except Exception:
                return None
            prefix = f"{sender}::named::{named_subject}::"
            found = False
            for row in rows:
                if not isinstance(row, list) or len(row) < 2:
                    continue
                content = str(row[1] or "").strip()
                if not content.lower().startswith(prefix.lower()):
                    continue
                value = content[len(prefix) :].strip()
                if not value:
                    continue
                found = True
                reply = _shorten_text(f"Your {named_subject} is called {value}.", max_send_chars)
                return f"((send {json.dumps(reply)}))"
            if not found:
                unknown_reply = _shorten_text("I don't know.", max_send_chars)
                return f"((send {json.dumps(unknown_reply)}))"
        if _LIKE_QUERY_RE.match(msg):
            query_text = f"{sender} likes"
            try:
                emb = ollama_embedding(query_text)
                rows = backend.query(emb, 8)
            except Exception:
                return None
            sender_prefix = f"{sender} likes "
            for row in rows:
                if not isinstance(row, list) or len(row) < 2:
                    continue
                content = str(row[1] or "").strip()
                if not content.lower().startswith(sender_prefix.lower()):
                    continue
                liked = content[len(sender_prefix) :].strip()
                if not liked:
                    continue
                reply = _shorten_text(f"You said you like {liked}.", max_send_chars)
                return f"((send {json.dumps(reply)}))"
    return None


def _extract_send_payloads(skill_text):
    payloads = []
    for match in _SEND_STR_RE.finditer(str(skill_text or "")):
        payload = match.group(2)
        try:
            decoded = json.loads(f'"{payload}"')
        except Exception:
            decoded = payload
        payloads.append(str(decoded))
    return payloads


def _is_meta_skill_output(skill_text):
    payloads = _extract_send_payloads(skill_text)
    for payload in payloads:
        if _META_REPLY_RE.search(payload):
            return True
    return False


def _direct_answer_from_ollama(user_text, max_send_chars):
    msg = _trim_user_text(_strip_user_prefix(user_text))
    if not msg:
        return None
    recipe_mode = _is_recipe_request(msg)
    creative_mode = _is_creative_request(msg)
    long_form_mode = _is_long_form_request(msg)
    detail_mode = _is_detailed_request(msg) or recipe_mode or creative_mode or long_form_mode
    long_form_mode = long_form_mode or recipe_mode
    if detail_mode:
        if long_form_mode:
            if creative_mode:
                direct_cap = _int_env("OLLAMA_LONG_FORM_CREATIVE_NUM_PREDICT", 220)
                if direct_cap <= 0:
                    direct_cap = 220
                prompt_prefix = str(
                    os.getenv(
                        "OLLAMA_LONG_FORM_CREATIVE_PROMPT_PREFIX",
                        "Tell a full, complete joke/story with setup and payoff. End cleanly:",
                    )
                    or ""
                ).strip()
            else:
                direct_cap = _int_env("OLLAMA_LONG_FORM_NUM_PREDICT", 128)
                if direct_cap <= 0:
                    direct_cap = 128
                prompt_prefix = str(
                    os.getenv(
                        "OLLAMA_LONG_FORM_PROMPT_PREFIX",
                        "Provide a complete, practical answer with clear structure and concrete details:",
                    )
                    or ""
                ).strip()
        elif creative_mode:
            direct_cap = _int_env("OLLAMA_CREATIVE_NUM_PREDICT", 64)
            if direct_cap <= 0:
                direct_cap = 64
            prompt_prefix = str(
                os.getenv(
                    "OLLAMA_CREATIVE_PROMPT_PREFIX",
                    "Tell a complete short joke/story with a clear ending:",
                )
                or ""
            ).strip()
        else:
            direct_cap = _int_env("OLLAMA_DETAIL_NUM_PREDICT", 24)
            if direct_cap <= 0:
                direct_cap = 24
            prompt_prefix = str(
                os.getenv(
                    "OLLAMA_DETAIL_PROMPT_PREFIX",
                    "Answer briefly with practical details:",
                )
                or ""
            ).strip()
    else:
        direct_cap = _int_env("OLLAMA_DIRECT_NUM_PREDICT", 12)
        if direct_cap <= 0:
            direct_cap = 12
        prompt_prefix = str(os.getenv("OLLAMA_DIRECT_PROMPT_PREFIX", "One-word answer if possible:") or "").strip()
    prompt = f"{prompt_prefix} {msg}".strip() if prompt_prefix else msg
    try:
        started = time.perf_counter()
        raw = ollama_chat(os.getenv("OLLAMA_MODEL", ""), prompt, min(max_send_chars, direct_cap), "low")
        _trace_perf(
            f"direct_answer ms={int((time.perf_counter() - started) * 1000)} "
            f"prompt_chars={len(prompt)} detail_mode={detail_mode} "
            f"long_form_mode={long_form_mode} creative_mode={creative_mode}"
        )
    except Exception:
        return None
    text = str(raw or "")
    text = text.replace("_newline_", "\n").replace("_apostrophe_", "'").replace("_quote_", '"')
    if detail_mode:
        if long_form_mode:
            text = _trim_incomplete_tail(_shorten_text(text, max_send_chars))
        else:
            text = _condense_detail_text(text, max_send_chars)
    else:
        text = _shorten_text(text, max_send_chars)
    return text if text else None


def _direct_skill_from_user_message(user_msg, max_send_chars):
    max_replies = _int_env("OLLAMA_DIRECT_MAX_REPLIES", 1)
    if max_replies <= 0:
        max_replies = 1
    if max_replies > 8:
        max_replies = 8
    parts = _extract_user_parts(user_msg)
    if not parts:
        return None
    parts = parts[:max_replies]
    worker_pool_size = _int_env("METTACLAW_WORKER_POOL_SIZE", 2)
    if worker_pool_size <= 0:
        worker_pool_size = 1
    if worker_pool_size > 8:
        worker_pool_size = 8
    workers = min(worker_pool_size, len(parts))
    replies_by_index = [None] * len(parts)
    _trace_perf(f"direct_skill parts={len(parts)} max_replies={max_replies} workers={workers}")
    if workers <= 1:
        for idx, part in enumerate(parts):
            replies_by_index[idx] = _direct_answer_from_ollama(part, max_send_chars)
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="direct-skill") as pool:
            future_map = {
                pool.submit(_direct_answer_from_ollama, part, max_send_chars): idx for idx, part in enumerate(parts)
            }
            for future in as_completed(future_map):
                idx = future_map[future]
                try:
                    replies_by_index[idx] = future.result()
                except Exception:
                    replies_by_index[idx] = None
    replies = [reply for reply in replies_by_index if reply]
    if not replies:
        return None
    cmds = " ".join(f"(send {json.dumps(reply)})" for reply in replies)
    return f"({cmds})"


def _is_true(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _trace_perf_enabled():
    return _is_true(os.getenv("TRACE_PERFORMANCE", "false"))


def _trace_perf(msg):
    if _trace_perf_enabled():
        print(f"[helper] [perf] {msg}", flush=True)


def _async_dispatch_enabled():
    return _is_true(os.getenv("METTACLAW_ASYNC_DISPATCH", "true"))


def _async_dispatch_workers():
    workers = _int_env("METTACLAW_ASYNC_DISPATCH_WORKERS", 4)
    if workers <= 0:
        workers = 1
    if workers > 16:
        workers = 16
    return workers


def _get_async_dispatch_executor():
    global _ASYNC_DISPATCH_EXECUTOR, _ASYNC_DISPATCH_WORKERS
    workers = _async_dispatch_workers()
    with _ASYNC_DISPATCH_LOCK:
        if _ASYNC_DISPATCH_EXECUTOR is None or _ASYNC_DISPATCH_WORKERS != workers:
            if _ASYNC_DISPATCH_EXECUTOR is not None:
                try:
                    _ASYNC_DISPATCH_EXECUTOR.shutdown(wait=False)
                except Exception:
                    pass
            _ASYNC_DISPATCH_EXECUTOR = ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="async-dispatch"
            )
            _ASYNC_DISPATCH_WORKERS = workers
        return _ASYNC_DISPATCH_EXECUTOR


def _dispatch_text_to_channels(text, reply_channel=""):
    msg = " ".join(str(text or "").split()).strip()
    if not msg:
        return False
    sent = False
    irc_backend = _load_irc_backend()
    if irc_backend is not None:
        try:
            connected = True
            if hasattr(irc_backend, "is_connected"):
                connected = bool(irc_backend.is_connected())
            if connected:
                if reply_channel and hasattr(irc_backend, "send_message"):
                    irc_backend.send_message(msg, reply_channel)
                else:
                    irc_backend.send_message(msg)
                sent = True
        except Exception:
            pass
    if sent:
        return True
    mattermost_backend = _load_mattermost_backend()
    if mattermost_backend is not None:
        try:
            mattermost_backend.send_message(msg)
            sent = True
        except Exception:
            pass
    return sent


def _dispatch_skill_output(skill_text, max_send_chars, reply_channel=""):
    sent_count = 0
    for payload in _extract_send_payloads(skill_text):
        if _dispatch_text_to_channels(_shorten_text(payload, max_send_chars), reply_channel=reply_channel):
            sent_count += 1
    return sent_count


def _sender_dispatch_lock(user_msg):
    sender, _ = _split_sender_and_text(user_msg)
    key = (sender or "_").strip().lower()
    with _SENDER_LOCKS_LOCK:
        lock = _SENDER_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _SENDER_LOCKS[key] = lock
        return lock


def _async_dispatch_one_message(user_msg, reply_channel=""):
    msg = str(user_msg or "").strip()
    if not msg:
        return
    sender_lock = _sender_dispatch_lock(msg)
    with sender_lock:
        started = time.perf_counter()
        max_send_chars = _max_send_chars_for_user_msg(msg)
        tool = _tool_skill_from_user_message(msg, max_send_chars)
        if tool is not None:
            candidate = tool
            path = "tool_search"
        else:
            direct = _direct_skill_from_user_message(msg, max_send_chars)
            if direct is not None:
                candidate = direct
                path = "direct"
            else:
                candidate = ""
                path = "empty"
        normalized = normalize_skill_output(
            candidate, msg, True, max_send_chars=max_send_chars, skip_async_guard=True
        )
        sent_count = _dispatch_skill_output(normalized, max_send_chars, reply_channel=reply_channel)
        _trace_perf(
            f"async_dispatch path={path} sent={sent_count} "
            f"msg_chars={len(msg)} ms={int((time.perf_counter() - started) * 1000)}"
        )


def _enqueue_async_dispatch(user_msg):
    parts = _extract_user_parts(user_msg)
    if not parts:
        return 0
    max_jobs = _int_env("METTACLAW_ASYNC_DISPATCH_MAX_BATCH", 8)
    if max_jobs <= 0:
        max_jobs = 1
    if max_jobs > 16:
        max_jobs = 16
    batch = []
    for part in parts:
        msg = str(part or "").strip()
        if not msg:
            continue
        batch.append(msg)
        if len(batch) >= max_jobs:
            break
    if not batch:
        return 0

    reply_channels = []
    irc_backend = _load_irc_backend()
    if irc_backend is not None and hasattr(irc_backend, "consume_batch_channels"):
        try:
            reply_channels = list(irc_backend.consume_batch_channels(len(batch)))
        except Exception:
            reply_channels = []
    if len(reply_channels) < len(batch):
        reply_channels.extend([""] * (len(batch) - len(reply_channels)))
    items = list(zip(batch, reply_channels))

    def run_batch(pairs):
        for item, reply_channel in pairs:
            _async_dispatch_one_message(item, reply_channel=reply_channel)

    executor = _get_async_dispatch_executor()
    executor.submit(run_batch, items)
    return len(batch)


def normalize_skill_output(s, user_msg="", msg_new=False, max_send_chars=None, skip_async_guard=False):
    if max_send_chars is None:
        max_send_chars = _max_send_chars_for_user_msg(user_msg)
    try:
        max_send_chars = int(max_send_chars)
    except Exception:
        max_send_chars = 280
    if max_send_chars < 64:
        max_send_chars = 64

    if not _is_true(msg_new):
        return "()"

    if not skip_async_guard and _async_dispatch_enabled() and _llm_mode() in {"fast", "user", "short"}:
        return "()"

    memory_reply = _recall_user_likes(user_msg, max_send_chars)
    if memory_reply is not None:
        return memory_reply
    _remember_user_likes(user_msg)

    text = str(s or "").strip()

    # If the model already emitted one of the supported skill commands,
    # keep it and just normalize parenthesis framing.
    if _looks_like_skill_output(text):
        normalized = _clamp_send_payloads(balance_parentheses(text), max_send_chars)
        if not _is_meta_skill_output(normalized):
            return normalized

    # Re-ask Ollama directly from the user message if the primary output is
    # malformed, empty, or meta/self-referential.
    direct = _direct_skill_from_user_message(user_msg, max_send_chars)
    if direct is not None:
        return direct

    if not text:
        return "()"

    plain = text.replace("\r\n", "\n").replace("\r", "\n")
    plain = plain.replace("_newline_", "\n").replace("_apostrophe_", "'").replace("_quote_", '"')
    return f"((send {json.dumps(_shorten_text(plain, max_send_chars))}))"


def _ollama_base_url():
    return os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")


def _float_env(name, default):
    raw = os.getenv(name, str(default)).strip()
    try:
        return float(raw)
    except ValueError:
        return float(default)


def _int_env(name, default):
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError:
        return int(default)


def _ollama_timeout_s():
    return max(1.0, _float_env("OLLAMA_TIMEOUT_S", 180))


def _ollama_connect_timeout_s():
    return max(0.1, _float_env("OLLAMA_CONNECT_TIMEOUT_S", 5))


def _ollama_read_timeout_s():
    return max(0.1, _float_env("OLLAMA_READ_TIMEOUT_S", _ollama_timeout_s()))


def _ollama_chat_stream_enabled():
    return _is_true(os.getenv("OLLAMA_CHAT_STREAM", "true"))


def _ollama_prewarm_enabled():
    return _is_true(os.getenv("OLLAMA_PREWARM", "true"))


def _ollama_prewarm_prompt():
    return os.getenv("OLLAMA_PREWARM_PROMPT", "respond with: ready")


def _ollama_keep_alive():
    raw = os.getenv("OLLAMA_KEEP_ALIVE", "-1").strip()
    if not raw:
        return -1
    try:
        return int(raw)
    except ValueError:
        return raw


def _normalize_think(value):
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"", "false", "off", "none", "0"}:
        return False
    if text in {"true", "on", "1"}:
        return True
    if text in {"low", "medium", "high"}:
        return text
    return False


def _ollama_think(effort):
    env_think = os.getenv("OLLAMA_THINK", "").strip()
    if env_think:
        return _normalize_think(env_think)
    effort_text = str(effort or "").strip().lower()
    if effort_text == "high":
        return "low"
    return False


def _effective_num_predict(max_tokens):
    try:
        requested = int(max_tokens)
    except Exception:
        requested = 128
    requested = max(1, requested)
    cap = _int_env("OLLAMA_NUM_PREDICT_CAP", 96)
    if cap > 0:
        requested = min(requested, cap)
    return requested


def _build_ollama_options(num_predict):
    options = {"num_predict": int(num_predict)}
    num_ctx = _int_env("OLLAMA_NUM_CTX", 0)
    if num_ctx > 0:
        options["num_ctx"] = num_ctx
    temperature_raw = os.getenv("OLLAMA_TEMPERATURE", "").strip()
    if temperature_raw:
        try:
            options["temperature"] = float(temperature_raw)
        except ValueError:
            pass
    return options


def _get_ollama_client():
    global _OLLAMA_CLIENT, _OLLAMA_CLIENT_BASE_URL
    if httpx is None:
        return None
    base_url = _ollama_base_url()
    with _OLLAMA_CLIENT_LOCK:
        if _OLLAMA_CLIENT is None or _OLLAMA_CLIENT_BASE_URL != base_url:
            if _OLLAMA_CLIENT is not None:
                try:
                    _OLLAMA_CLIENT.close()
                except Exception:
                    pass
            timeout = httpx.Timeout(
                timeout=_ollama_timeout_s(),
                connect=_ollama_connect_timeout_s(),
                read=_ollama_read_timeout_s(),
            )
            limits = httpx.Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=120.0)
            _OLLAMA_CLIENT = httpx.Client(base_url=base_url, timeout=timeout, limits=limits)
            _OLLAMA_CLIENT_BASE_URL = base_url
        return _OLLAMA_CLIENT


def _request_json(method, path, payload=None, timeout=180):
    client = _get_ollama_client()
    if client is not None:
        try:
            response = client.request(method, path, json=payload, timeout=max(0.1, float(timeout)))
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text
            raise RuntimeError(f"Ollama HTTP {exc.response.status_code} at {exc.request.url}: {detail}") from exc
        except httpx.RequestError as exc:
            raise RuntimeError(f"Ollama connection failed at {exc.request.url}: {exc}") from exc

    url = _ollama_base_url() + path
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Ollama HTTP {exc.code} at {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Ollama connection failed at {url}: {exc}") from exc
    return json.loads(body)


def _post_json(path, payload, timeout=180):
    return _request_json("POST", path, payload=payload, timeout=timeout)


def _get_json(path, timeout=180):
    return _request_json("GET", path, payload=None, timeout=timeout)


def _prewarm_chat_model(model):
    if not _ollama_prewarm_enabled():
        return
    chosen = str(model).strip()
    if not chosen:
        return
    key = f"chat::{chosen}"
    with _PREWARM_LOCK:
        if key in _PREWARMED_MODELS:
            return
        payload = {
            "model": chosen,
            "messages": [{"role": "user", "content": _ollama_prewarm_prompt()}],
            "stream": False,
            "keep_alive": _ollama_keep_alive(),
            "think": _normalize_think(os.getenv("OLLAMA_PREWARM_THINK", "false")),
            "options": _build_ollama_options(8),
        }
        try:
            _post_json("/api/chat", payload, timeout=60)
            _PREWARMED_MODELS.add(key)
        except Exception:
            pass


def _prewarm_embed_model(model):
    if not _ollama_prewarm_enabled():
        return
    chosen = str(model).strip()
    if not chosen:
        return
    key = f"embed::{chosen}"
    with _PREWARM_LOCK:
        if key in _PREWARMED_MODELS:
            return
        try:
            _post_json("/api/embed", {"model": chosen, "input": "warmup"}, timeout=60)
            _PREWARMED_MODELS.add(key)
        except Exception:
            pass


def _background_prewarm():
    # Best-effort warmup to hide first-user latency after container start.
    chat_model = str(os.getenv("OLLAMA_MODEL", "") or "").strip()
    embed_model = str(os.getenv("OLLAMA_EMBED_MODEL", "") or "").strip()
    if chat_model:
        for _ in range(30):
            try:
                _ensure_ollama_model(chat_model)
                _prewarm_chat_model(chat_model)
                break
            except Exception:
                time.sleep(2)
    if embed_model:
        for _ in range(10):
            try:
                _ensure_ollama_model(embed_model)
                _prewarm_embed_model(embed_model)
                break
            except Exception:
                time.sleep(2)


def _start_background_prewarm():
    global _PREWARM_THREAD_STARTED
    if not _ollama_prewarm_enabled():
        return
    with _PREWARM_LOCK:
        if _PREWARM_THREAD_STARTED:
            return
        _PREWARM_THREAD_STARTED = True
    thread = threading.Thread(target=_background_prewarm, daemon=True, name="ollama-prewarm")
    thread.start()


def _chat_via_stream(payload, timeout=180):
    client = _get_ollama_client()
    if client is None:
        clone = dict(payload)
        clone["stream"] = False
        res = _post_json("/api/chat", clone, timeout=timeout)
        msg = (res.get("message") or {}).get("content")
        if isinstance(msg, str):
            return msg
        fallback = res.get("response")
        if isinstance(fallback, str):
            return fallback
        return str(res)

    chunks = []
    done_event = None
    try:
        with client.stream("POST", "/api/chat", json=payload, timeout=max(0.1, float(timeout))) as response:
            response.raise_for_status()
            for raw_line in response.iter_lines():
                line = str(raw_line).strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                msg = event.get("message")
                if isinstance(msg, dict):
                    piece = msg.get("content")
                    if isinstance(piece, str) and piece:
                        chunks.append(piece)
                if bool(event.get("done")):
                    done_event = event
                    break
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text
        raise RuntimeError(f"Ollama HTTP {exc.response.status_code} at {exc.request.url}: {detail}") from exc
    except httpx.RequestError as exc:
        raise RuntimeError(f"Ollama connection failed at {exc.request.url}: {exc}") from exc

    text = "".join(chunks).strip()
    if isinstance(done_event, dict):
        def _ms(raw):
            try:
                return int(float(raw) / 1_000_000.0)
            except (TypeError, ValueError):
                return None

        _trace_perf(
            "ollama_chat "
            f"load_ms={_ms(done_event.get('load_duration'))} "
            f"prompt_eval_ms={_ms(done_event.get('prompt_eval_duration'))} "
            f"eval_ms={_ms(done_event.get('eval_duration'))} "
            f"done_reason={done_event.get('done_reason')}"
        )
    if text:
        return text
    if isinstance(done_event, dict):
        fallback = done_event.get("response")
        if isinstance(fallback, str):
            return fallback
        message = done_event.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                return content
        return str(done_event)
    return ""


def _auto_pull_enabled():
    return os.getenv("OLLAMA_AUTO_PULL", "true").strip().lower() in {"1", "true", "yes", "on"}


def _model_exists(model):
    wanted = str(model).strip().lower()
    if not wanted:
        return True
    tags = _get_json("/api/tags")
    models = tags.get("models")
    if not isinstance(models, list):
        return False
    wanted_base = wanted.split(":", 1)[0]
    for item in models:
        name = str(item.get("name", "")).strip().lower()
        if not name:
            continue
        if name == wanted:
            return True
        if name.split(":", 1)[0] == wanted_base:
            return True
    return False


def _ensure_ollama_model(model):
    chosen = str(model).strip()
    if not chosen:
        return
    if chosen in _ENSURED_MODELS:
        return
    if _model_exists(chosen):
        _ENSURED_MODELS.add(chosen)
        return
    if not _auto_pull_enabled():
        raise RuntimeError(
            f"Ollama model '{chosen}' is not available. "
            "Start Ollama with that model or enable OLLAMA_AUTO_PULL=true."
        )
    _post_json("/api/pull", {"model": chosen, "stream": False}, timeout=3600)
    _ENSURED_MODELS.add(chosen)


def _llm_mode():
    return str(os.getenv("METTACLAW_LLM_MODE", "fast") or "").strip().lower()


def generate_skill_candidate(model, prompt, max_tokens, effort, user_msg=""):
    _start_background_prewarm()
    if _llm_mode() in {"fast", "user", "short"}:
        if _async_dispatch_enabled():
            queued = _enqueue_async_dispatch(user_msg)
            _trace_perf(f"generate_skill_candidate path=async_enqueued queued={queued}")
            return ""
        max_send_chars = _max_send_chars_for_user_msg(user_msg)
        tool = _tool_skill_from_user_message(user_msg, max_send_chars)
        if tool is not None:
            _trace_perf("generate_skill_candidate path=tool_search")
            return tool
        direct = _direct_skill_from_user_message(user_msg, max_send_chars)
        if direct is not None:
            _trace_perf("generate_skill_candidate path=direct")
            return direct
        _trace_perf("generate_skill_candidate path=empty_user")
        return ""
    return ollama_chat(model, prompt, max_tokens, effort)


def ollama_chat(model, prompt, max_tokens, effort):
    _start_background_prewarm()
    chosen_model = _get_active_chat_model(model)
    _ensure_ollama_model(chosen_model)
    _prewarm_chat_model(chosen_model)
    think = _ollama_think(effort)
    num_predict = _effective_num_predict(max_tokens)
    payload = {
        "model": chosen_model,
        "messages": [{"role": "user", "content": str(prompt)}],
        "stream": _ollama_chat_stream_enabled(),
        "keep_alive": _ollama_keep_alive(),
        "think": think,
        "options": _build_ollama_options(num_predict),
    }
    if payload["stream"]:
        return _chat_via_stream(payload, timeout=_ollama_timeout_s())

    res = _post_json("/api/chat", payload, timeout=_ollama_timeout_s())
    def _ms(raw):
        try:
            return int(float(raw) / 1_000_000.0)
        except (TypeError, ValueError):
            return None

    _trace_perf(
        "ollama_chat "
        f"load_ms={_ms(res.get('load_duration'))} "
        f"prompt_eval_ms={_ms(res.get('prompt_eval_duration'))} "
        f"eval_ms={_ms(res.get('eval_duration'))} "
        f"done_reason={res.get('done_reason')}"
    )
    msg = (res.get("message") or {}).get("content")
    if isinstance(msg, str):
        return msg
    fallback = res.get("response")
    if isinstance(fallback, str):
        return fallback
    return str(res)


def ollama_embedding(text):
    _start_background_prewarm()
    model = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
    _ensure_ollama_model(model)
    _prewarm_embed_model(model)
    # Prefer /api/embed (newer), fall back to /api/embeddings.
    try:
        res = _post_json(
            "/api/embed",
            {
                "model": model,
                "input": str(text),
            },
            timeout=_ollama_timeout_s(),
        )
        embeddings = res.get("embeddings")
        if isinstance(embeddings, list) and embeddings:
            return embeddings[0]
    except RuntimeError:
        pass

    res = _post_json(
        "/api/embeddings",
        {
            "model": model,
            "prompt": str(text),
        },
        timeout=_ollama_timeout_s(),
    )
    embedding = res.get("embedding")
    if isinstance(embedding, list):
        return embedding
    raise RuntimeError(f"Ollama embedding response missing embedding vector: {res}")


try:
    _start_background_prewarm()
except Exception:
    pass
