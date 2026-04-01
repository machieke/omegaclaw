import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone

_SUPPORTED_COMMCHANNELS = ("irc", "mattermost", "telegram", "discord", "slack")
_AUTH_REGISTER_RE = re.compile(
    r"^\s*(?:auth(?:enticate)?|register)\s+(.+?)\s*$",
    re.IGNORECASE,
)

_IS_TRUE_FN = None
_INT_ENV_FN = None
_DECODE_SPECIAL_TOKENS_FN = None
_EXTRACT_USER_PARTS_FN = None
_SPLIT_SENDER_AND_TEXT_FN = None
_NORMALIZE_COMMCHANNEL_FN = None
_GET_ACTIVE_COMMCHANNEL_FN = None
_DISPATCH_TO_CHANNEL_FN = None

_AUTH_LOCK = threading.Lock()
_AUTH_SECRET = ""
_AUTH_REGISTERED_USER = ""
_AUTH_REGISTERED_CHANNEL = ""
_AUTH_REGISTERED_AT = ""
_AUTH_NOTICE_POSTED = set()
_AUTH_HINTED_SENDERS = {}


def configure(
    is_true_fn=None,
    int_env_fn=None,
    decode_special_tokens_fn=None,
    extract_user_parts_fn=None,
    split_sender_and_text_fn=None,
    normalize_commchannel_fn=None,
    get_active_commchannel_fn=None,
    dispatch_to_channel_fn=None,
):
    global _IS_TRUE_FN
    global _INT_ENV_FN
    global _DECODE_SPECIAL_TOKENS_FN
    global _EXTRACT_USER_PARTS_FN
    global _SPLIT_SENDER_AND_TEXT_FN
    global _NORMALIZE_COMMCHANNEL_FN
    global _GET_ACTIVE_COMMCHANNEL_FN
    global _DISPATCH_TO_CHANNEL_FN
    _IS_TRUE_FN = is_true_fn
    _INT_ENV_FN = int_env_fn
    _DECODE_SPECIAL_TOKENS_FN = decode_special_tokens_fn
    _EXTRACT_USER_PARTS_FN = extract_user_parts_fn
    _SPLIT_SENDER_AND_TEXT_FN = split_sender_and_text_fn
    _NORMALIZE_COMMCHANNEL_FN = normalize_commchannel_fn
    _GET_ACTIVE_COMMCHANNEL_FN = get_active_commchannel_fn
    _DISPATCH_TO_CHANNEL_FN = dispatch_to_channel_fn


def _fallback_is_true(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _is_true(value):
    if callable(_IS_TRUE_FN):
        try:
            return bool(_IS_TRUE_FN(value))
        except Exception:
            pass
    return _fallback_is_true(value)


def _fallback_int_env(name, default):
    raw = str(os.getenv(name, str(default)) or "").strip()
    try:
        return int(raw)
    except Exception:
        return int(default)


def _int_env(name, default):
    if callable(_INT_ENV_FN):
        try:
            return int(_INT_ENV_FN(name, default))
        except Exception:
            pass
    return _fallback_int_env(name, default)


def _decode_special_tokens(text):
    if callable(_DECODE_SPECIAL_TOKENS_FN):
        try:
            return str(_DECODE_SPECIAL_TOKENS_FN(text))
        except Exception:
            pass
    return (
        str(text or "")
        .replace("_apostrophe_", "'")
        .replace("_quote_", '"')
        .replace("_newline_", "\n")
    )


def _extract_user_parts(text):
    if callable(_EXTRACT_USER_PARTS_FN):
        try:
            parts = _EXTRACT_USER_PARTS_FN(text)
            if isinstance(parts, (list, tuple)):
                return [str(p or "").strip() for p in parts if str(p or "").strip()]
        except Exception:
            pass
    raw = str(text or "").strip()
    if not raw:
        return []
    parts = [p.strip() for p in raw.split(" | ") if p.strip()]
    return parts if parts else [raw]


def _split_sender_and_text(msg):
    if callable(_SPLIT_SENDER_AND_TEXT_FN):
        try:
            sender, body = _SPLIT_SENDER_AND_TEXT_FN(msg)
            return str(sender or "").strip(), str(body or "").strip()
        except Exception:
            pass
    text = str(msg or "").strip()
    if ": " in text:
        head, tail = text.split(": ", 1)
        if head and " " not in head:
            return head.strip(), tail.strip()
    return "", text


def _normalize_commchannel(name):
    if callable(_NORMALIZE_COMMCHANNEL_FN):
        try:
            value = str(_NORMALIZE_COMMCHANNEL_FN(name) or "").strip().lower()
            if value in _SUPPORTED_COMMCHANNELS:
                return value
        except Exception:
            pass
    channel = str(name or "").strip().lower()
    if channel in _SUPPORTED_COMMCHANNELS:
        return channel
    return ""


def _get_active_commchannel():
    if callable(_GET_ACTIVE_COMMCHANNEL_FN):
        try:
            value = str(_GET_ACTIVE_COMMCHANNEL_FN() or "").strip().lower()
            if value in _SUPPORTED_COMMCHANNELS:
                return value
        except Exception:
            pass
    fallback = str(os.getenv("METTACLAW_COMMCHANNEL", "irc") or "").strip().lower()
    return fallback if fallback in _SUPPORTED_COMMCHANNELS else "irc"


def _dispatch_to_channel(channel, msg):
    if callable(_DISPATCH_TO_CHANNEL_FN):
        try:
            return bool(_DISPATCH_TO_CHANNEL_FN(channel, msg))
        except Exception:
            return False
    return False


def _auth_log(msg):
    print(f"[auth] {msg}", flush=True)


def _normalize_sender(sender):
    return str(sender or "").strip().lower()


def _auth_enabled():
    return _is_true(os.getenv("METTACLAW_AUTH_REQUIRED", "true"))


def _registered_sender():
    with _AUTH_LOCK:
        return str(_AUTH_REGISTERED_USER or "").strip()


def _auth_notice_text():
    return "Authentication required. Post startup secret as: auth <secret>"


def _unwrap_secret_token(token):
    text = str(token or "").strip()
    if not text:
        return ""
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"', "`"}:
        text = text[1:-1].strip()
    text = text.rstrip(".,!?;:")
    return text


def _extract_registration_token(text):
    msg = _decode_special_tokens(text).strip()
    if not msg:
        return ""
    while msg.startswith("(") and msg.endswith(")") and len(msg) > 2:
        inner = msg[1:-1].strip()
        if not inner:
            break
        msg = inner
    match = _AUTH_REGISTER_RE.match(msg)
    if match is not None:
        return _unwrap_secret_token(match.group(1))
    return _unwrap_secret_token(msg)


def bootstrap_auth_state():
    global _AUTH_SECRET
    if not _auth_enabled():
        return
    with _AUTH_LOCK:
        if _AUTH_REGISTERED_USER:
            return
        if _AUTH_SECRET:
            return
        configured = str(os.getenv("METTACLAW_AUTH_SECRET", "") or "").strip()
        if configured:
            _AUTH_SECRET = configured
        else:
            secret_bytes = _int_env("METTACLAW_AUTH_SECRET_BYTES", 12)
            if secret_bytes < 8:
                secret_bytes = 8
            if secret_bytes > 64:
                secret_bytes = 64
            _AUTH_SECRET = secrets.token_hex(secret_bytes)
        _auth_log("startup one-time secret generated")
        _auth_log("post the secret (or: auth <secret>) in a commchannel to register the controlling user")
        _auth_log(f"startup secret: {_AUTH_SECRET}")


def announce_auth_notice(channel):
    target = _normalize_commchannel(channel)
    if not target:
        return False
    if not _auth_enabled():
        return False
    bootstrap_auth_state()
    with _AUTH_LOCK:
        if _AUTH_REGISTERED_USER:
            return False
        if target in _AUTH_NOTICE_POSTED:
            return True
    sent = _dispatch_to_channel(target, _auth_notice_text())
    if sent:
        with _AUTH_LOCK:
            _AUTH_NOTICE_POSTED.add(target)
        _auth_log(f"posted auth notice in channel={target}")
    return sent


def _should_send_auth_hint(sender):
    cooldown_s = _int_env("METTACLAW_AUTH_HINT_COOLDOWN_S", 45)
    if cooldown_s < 0:
        cooldown_s = 0
    now = time.monotonic()
    key = _normalize_sender(sender) or "_"
    with _AUTH_LOCK:
        last = float(_AUTH_HINTED_SENDERS.get(key, 0.0))
        if now - last < float(cooldown_s):
            return False
        _AUTH_HINTED_SENDERS[key] = now
        return True


def _register_authenticated_user(sender, channel):
    global _AUTH_REGISTERED_USER, _AUTH_REGISTERED_CHANNEL, _AUTH_REGISTERED_AT, _AUTH_SECRET
    user = str(sender or "").strip()
    if not user:
        return False
    target = _normalize_commchannel(channel)
    if not target:
        target = _get_active_commchannel()
    with _AUTH_LOCK:
        _AUTH_REGISTERED_USER = user
        _AUTH_REGISTERED_CHANNEL = target
        _AUTH_REGISTERED_AT = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        _AUTH_SECRET = ""
    _dispatch_to_channel(target, f"Authenticated user registered: {user}.")
    _auth_log(f"registered authenticated user={user} channel={target} at={_AUTH_REGISTERED_AT}")
    return True


def filter_incoming_by_auth(raw_msg, source_channel):
    text = str(raw_msg or "").strip()
    if not text:
        return ""
    if not _auth_enabled():
        return text
    bootstrap_auth_state()

    accepted = []
    source = _normalize_commchannel(source_channel) or _get_active_commchannel()
    for part in _extract_user_parts(text):
        item = str(part or "").strip()
        if not item:
            continue
        sender, body = _split_sender_and_text(item)
        sender_name = str(sender or "").strip()
        if not sender_name:
            continue

        current = _registered_sender()
        if current:
            if _normalize_sender(sender_name) == _normalize_sender(current):
                accepted.append(item)
            else:
                _auth_log(f"ignored sender={sender_name} in channel={source} (registered={current})")
                if _should_send_auth_hint(sender_name):
                    _dispatch_to_channel(source, _auth_notice_text())
            continue

        candidate = _extract_registration_token(body)
        with _AUTH_LOCK:
            expected = str(_AUTH_SECRET or "").strip()
        if expected and candidate and candidate == expected:
            _register_authenticated_user(sender_name, source)
            continue

        _auth_log(f"ignored unauthenticated sender={sender_name} in channel={source}")
        if _should_send_auth_hint(sender_name):
            _dispatch_to_channel(source, _auth_notice_text())

    return " | ".join(accepted)
