import json
import os
import re
import threading

_PERSONA_META_RE = re.compile(r'\(persona-meta\s+"((?:\\.|[^"\\])*)"\s+"((?:\\.|[^"\\])*)"\s*\)')
_PERSONA_SECTION_RE = re.compile(
    r'\(persona-section\s+"((?:\\.|[^"\\])*)"\s+"((?:\\.|[^"\\])*)"\s+"((?:\\.|[^"\\])*)"\s*\)'
)

_PERSONA_LIST_RE = re.compile(r"^\s*(?:persona\s+list|list\s+personas?)\s*\??\s*$", re.IGNORECASE)
_PERSONA_CURRENT_RE = re.compile(
    r"^\s*(?:persona\s+current|current\s+persona|show\s+current\s+persona)\s*\??\s*$",
    re.IGNORECASE,
)
_PERSONA_USE_RE = re.compile(
    r"^\s*(?:persona\s+use|use\s+persona)\s+([a-z0-9._-]+)\s*\??\s*$",
    re.IGNORECASE,
)

_PERSONA_LOCK = threading.Lock()
_PERSONA_CACHE = {}
_PERSONA_SECTION_ORDER = ("context", "role", "methodology", "rules", "output")


def _project_root_dir():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def normalize_persona_id(value):
    text = str(value or "").strip().lower()
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"[^a-z0-9._-]", "", text)
    return text


def persona_enabled():
    raw = str(os.getenv("METTACLAW_PERSONA_ENABLED", "true") or "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _decode_escaped_literal(raw):
    token = str(raw or "")
    try:
        return json.loads(f'"{token}"')
    except Exception:
        return token.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")


def _resolve_persona_path(persona_id="", project_root_dir=""):
    root = str(project_root_dir or "").strip() or _project_root_dir()
    pid = normalize_persona_id(persona_id)
    if not pid:
        pid = normalize_persona_id(os.getenv("METTACLAW_PERSONA_ID", "default"))
    if not pid:
        pid = "default"
    configured = str(os.getenv("METTACLAW_PERSONA_PATH", "") or "").strip()
    if configured:
        if "{persona_id}" in configured:
            configured = configured.replace("{persona_id}", pid)
        path = configured
    else:
        path = os.path.join(root, "personas", f"{pid}.metta")
    if not os.path.isabs(path):
        path = os.path.abspath(os.path.join(root, path))
    return pid, path


def load_persona_spec(persona_id="", project_root_dir=""):
    requested_id, path = _resolve_persona_path(persona_id, project_root_dir=project_root_dir)
    try:
        mtime = float(os.path.getmtime(path))
    except Exception:
        return {}
    with _PERSONA_LOCK:
        cached = _PERSONA_CACHE.get(path)
        if isinstance(cached, dict) and float(cached.get("mtime", 0.0)) == mtime:
            spec = cached.get("spec") or {}
            if isinstance(spec, dict):
                if not requested_id or str(spec.get("id") or "").strip().lower() == requested_id:
                    return {
                        "id": str(spec.get("id") or "").strip(),
                        "title": str(spec.get("title") or "").strip(),
                        "sections": dict(spec.get("sections") or {}),
                    }
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return {}

    spec = {
        "id": str(requested_id or "").strip(),
        "title": "",
        "sections": {},
    }
    for match in _PERSONA_META_RE.finditer(text):
        meta_id = normalize_persona_id(_decode_escaped_literal(match.group(1)))
        title = str(_decode_escaped_literal(match.group(2)) or "").strip()
        if not meta_id:
            continue
        if requested_id and meta_id != requested_id:
            continue
        spec["id"] = meta_id
        if title:
            spec["title"] = title
        break

    for match in _PERSONA_SECTION_RE.finditer(text):
        entry_id = normalize_persona_id(_decode_escaped_literal(match.group(1)))
        key = str(_decode_escaped_literal(match.group(2)) or "").strip().lower()
        value = str(_decode_escaped_literal(match.group(3)) or "").strip()
        if not entry_id or not key or not value:
            continue
        if requested_id and entry_id != requested_id:
            continue
        if spec.get("id") and entry_id != spec["id"]:
            continue
        if not spec.get("id"):
            spec["id"] = entry_id
        spec["sections"][key] = value

    if not spec["sections"]:
        return {}
    if not spec.get("id"):
        spec["id"] = requested_id
    if not spec["title"]:
        spec["title"] = spec["id"].replace("-", " ").strip().title()

    out = {
        "id": str(spec.get("id") or "").strip(),
        "title": str(spec.get("title") or "").strip(),
        "sections": dict(spec.get("sections") or {}),
    }
    with _PERSONA_LOCK:
        _PERSONA_CACHE[path] = {
            "mtime": mtime,
            "spec": out,
        }
    return out


def _persona_section_names():
    raw = str(os.getenv("METTACLAW_PERSONA_SECTIONS", "") or "").strip().lower()
    if not raw:
        return list(_PERSONA_SECTION_ORDER)
    names = []
    for item in raw.split(","):
        key = str(item or "").strip().lower()
        if not key:
            continue
        if key not in names:
            names.append(key)
    return names if names else list(_PERSONA_SECTION_ORDER)


def active_persona_id():
    pid = normalize_persona_id(os.getenv("METTACLAW_PERSONA_ID", "default"))
    return pid or "default"


def available_persona_ids(project_root_dir=""):
    root = str(project_root_dir or "").strip() or _project_root_dir()
    personas_root = os.path.join(root, "personas")
    ids = []
    try:
        files = sorted(os.listdir(personas_root))
    except Exception:
        files = []
    for name in files:
        if not str(name).lower().endswith(".metta"):
            continue
        stem = normalize_persona_id(os.path.splitext(name)[0])
        if stem and stem not in ids:
            ids.append(stem)
        spec = load_persona_spec(stem, project_root_dir=root)
        sid = normalize_persona_id((spec or {}).get("id"))
        if sid and sid not in ids:
            ids.append(sid)
    active = active_persona_id()
    if active and active not in ids:
        ids.append(active)
    return ids


def switch_persona(persona_id, project_root_dir=""):
    target = normalize_persona_id(persona_id)
    if not target:
        return False, ""
    spec = load_persona_spec(target, project_root_dir=project_root_dir)
    if not spec:
        return False, ""
    resolved = normalize_persona_id(spec.get("id")) or target
    os.environ["METTACLAW_PERSONA_ID"] = resolved
    return True, resolved


def build_system_prompt(base_prompt="", project_root_dir=""):
    base = str(base_prompt or "").strip()
    if not persona_enabled():
        return base

    requested_id = normalize_persona_id(os.getenv("METTACLAW_PERSONA_ID", "default"))
    spec = load_persona_spec(requested_id, project_root_dir=project_root_dir)
    if not spec:
        return base

    persona_id = str(spec.get("id") or requested_id or "").strip()
    title = str(spec.get("title") or "").strip()
    sections = dict(spec.get("sections") or {})
    if not sections:
        return base

    segments = []
    label = ""
    if title and persona_id:
        label = f"{title} ({persona_id})"
    elif title:
        label = title
    elif persona_id:
        label = persona_id
    if label:
        segments.append(f"PERSONA: {label}")

    for key in _persona_section_names():
        value = str(sections.get(key) or "").strip()
        if not value:
            continue
        tag = re.sub(r"[^a-z0-9_-]", "", key.replace(" ", "-").lower()) or "section"
        segments.append(f"<persona-{tag}>\n{value}\n</persona-{tag}>")

    max_chars_raw = str(os.getenv("METTACLAW_PERSONA_MAX_CHARS", "0") or "").strip()
    try:
        max_chars = int(max_chars_raw)
    except Exception:
        max_chars = 0

    selected = []
    for segment in segments:
        candidate = "\n".join(selected + [segment]).strip()
        if max_chars > 0 and len(candidate) > max_chars:
            break
        selected.append(segment)

    overlay = "\n".join(selected).strip()
    if not overlay:
        return base
    if not base:
        return overlay
    return f"{base}\n\n{overlay}"


def persona_control_skill_from_user_message(
    user_msg,
    max_send_chars,
    extract_user_parts_fn,
    split_sender_and_text_fn,
    get_active_commchannel_fn,
    decode_special_tokens_fn,
    policy_decide_for_actor_fn,
    policy_denied_reply_fn,
    shorten_text_fn,
    json_quote_fn,
    project_root_dir="",
):
    replies = []
    root = str(project_root_dir or "").strip() or _project_root_dir()
    for part in extract_user_parts_fn(user_msg):
        sender, body = split_sender_and_text_fn(part)
        actor = str(sender or "").strip() or "unknown"
        commchannel = get_active_commchannel_fn()
        msg = decode_special_tokens_fn(body).strip()
        if not msg:
            continue
        if _PERSONA_LIST_RE.match(msg):
            decision = policy_decide_for_actor_fn("list-personas", actor, commchannel, target=msg)
            if not decision.allowed:
                replies.append(policy_denied_reply_fn(decision, max_send_chars, actor=actor))
                if len(replies) >= 3:
                    break
                continue
            names = available_persona_ids(project_root_dir=root)
            current = active_persona_id()
            if names:
                reply = f"Available personas: {', '.join(names)}. Current persona: {current}."
            else:
                reply = f"No persona files found. Current persona: {current}."
            replies.append(shorten_text_fn(reply, max_send_chars))
            if len(replies) >= 3:
                break
            continue
        if _PERSONA_CURRENT_RE.match(msg):
            decision = policy_decide_for_actor_fn("current-persona", actor, commchannel, target=msg)
            if not decision.allowed:
                replies.append(policy_denied_reply_fn(decision, max_send_chars, actor=actor))
                if len(replies) >= 3:
                    break
                continue
            current = active_persona_id()
            spec = load_persona_spec(current, project_root_dir=root)
            title = str((spec or {}).get("title") or "").strip()
            if title:
                reply = f"Current persona is {current} ({title})."
            else:
                reply = f"Current persona is {current}."
            replies.append(shorten_text_fn(reply, max_send_chars))
            if len(replies) >= 3:
                break
            continue
        use_match = _PERSONA_USE_RE.match(msg)
        if use_match is None:
            continue
        decision = policy_decide_for_actor_fn("use-persona", actor, commchannel, target=msg)
        if not decision.allowed:
            replies.append(policy_denied_reply_fn(decision, max_send_chars, actor=actor))
            if len(replies) >= 3:
                break
            continue
        target = normalize_persona_id(use_match.group(1))
        ok, resolved = switch_persona(target, project_root_dir=root)
        if ok and resolved:
            spec = load_persona_spec(resolved, project_root_dir=root)
            title = str((spec or {}).get("title") or "").strip()
            if title:
                reply = f"Switched persona to {resolved} ({title})."
            else:
                reply = f"Switched persona to {resolved}."
            replies.append(shorten_text_fn(reply, max_send_chars))
        else:
            names = available_persona_ids(project_root_dir=root)
            if names:
                reply = f"Persona '{target}' not found. Available: {', '.join(names)}."
            else:
                reply = f"Persona '{target}' not found."
            replies.append(shorten_text_fn(reply, max_send_chars))
        if len(replies) >= 3:
            break
    if not replies:
        return None
    cmds = " ".join(f"(send {json_quote_fn(reply)})" for reply in replies[:3])
    return f"({cmds})"
