import threading
import time

import requests

_running = False
_last_message = ""
_msg_lock = threading.Lock()
_connected = False

SLACK_BOT_TOKEN = ""
SLACK_CHANNEL_ID = ""
_last_seen_ts = ""
_POLL_INTERVAL_S = 2.0
_user_cache = {}


def _set_last(msg):
    global _last_message
    with _msg_lock:
        if _last_message == "":
            _last_message = msg
        else:
            _last_message = _last_message + " | " + msg


def getLastMessage():
    global _last_message
    with _msg_lock:
        tmp = _last_message
        _last_message = ""
        return tmp


def _headers():
    return {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-Type": "application/json; charset=utf-8",
    }


def _conversations_history():
    return "https://slack.com/api/conversations.history"


def _chat_post_message():
    return "https://slack.com/api/chat.postMessage"


def _users_info():
    return "https://slack.com/api/users.info"


def _ts_float(value):
    try:
        return float(str(value))
    except Exception:
        return 0.0


def _user_name(user_id):
    uid = str(user_id or "").strip()
    if not uid:
        return "slack-user"
    cached = _user_cache.get(uid)
    if cached:
        return cached
    try:
        response = requests.get(
            _users_info(),
            headers=_headers(),
            params={"user": uid},
            timeout=10,
        )
        payload = response.json()
        if isinstance(payload, dict) and payload.get("ok"):
            user = payload.get("user") or {}
            profile = user.get("profile") or {}
            name = str(profile.get("display_name") or "").strip()
            if not name:
                name = str(user.get("real_name") or "").strip()
            if not name:
                name = str(user.get("name") or "").strip()
            if name:
                _user_cache[uid] = name
                return name
    except Exception:
        pass
    return "slack-user"


def _poll_loop():
    global _connected, _last_seen_ts
    while _running:
        token = str(SLACK_BOT_TOKEN or "").strip()
        channel_id = str(SLACK_CHANNEL_ID or "").strip()
        if not token or not channel_id:
            _connected = False
            time.sleep(1.0)
            continue
        try:
            response = requests.get(
                _conversations_history(),
                headers=_headers(),
                params={"channel": channel_id, "limit": 25},
                timeout=15,
            )
            payload = response.json()
            if not isinstance(payload, dict) or not payload.get("ok"):
                _connected = False
                time.sleep(_POLL_INTERVAL_S)
                continue
            _connected = True
            messages = payload.get("messages")
            if not isinstance(messages, list) or not messages:
                time.sleep(_POLL_INTERVAL_S)
                continue
            items = sorted(
                [item for item in messages if isinstance(item, dict)],
                key=lambda item: _ts_float(item.get("ts")),
            )
            newest_ts = str(items[-1].get("ts", "")).strip()
            if not _last_seen_ts:
                _last_seen_ts = newest_ts
                time.sleep(_POLL_INTERVAL_S)
                continue
            baseline = _ts_float(_last_seen_ts)
            for item in items:
                ts = _ts_float(item.get("ts"))
                if ts <= baseline:
                    continue
                if str(item.get("subtype") or "").strip() == "bot_message":
                    continue
                if str(item.get("bot_id") or "").strip():
                    continue
                text = str(item.get("text") or "").strip()
                if not text:
                    continue
                sender = _user_name(item.get("user"))
                _set_last(f"{sender}: {text}")
            _last_seen_ts = newest_ts or _last_seen_ts
        except Exception:
            _connected = False
        time.sleep(_POLL_INTERVAL_S)


def start_slack(bot_token, channel_id):
    global _running, SLACK_BOT_TOKEN, SLACK_CHANNEL_ID, _last_seen_ts, _user_cache
    SLACK_BOT_TOKEN = str(bot_token or "").strip()
    SLACK_CHANNEL_ID = str(channel_id or "").strip()
    _last_seen_ts = ""
    _user_cache = {}
    _running = True
    t = threading.Thread(target=_poll_loop, daemon=True)
    t.start()
    return t


def stop_slack():
    global _running
    _running = False


def is_connected():
    return _connected


def send_message(text):
    global _connected
    token = str(SLACK_BOT_TOKEN or "").strip()
    channel_id = str(SLACK_CHANNEL_ID or "").strip()
    if not token or not channel_id:
        return
    payload = {"channel": channel_id, "text": str(text or "").replace("\\n", "\n")}
    try:
        response = requests.post(_chat_post_message(), headers=_headers(), json=payload, timeout=15)
        body = response.json()
        _connected = isinstance(body, dict) and bool(body.get("ok"))
    except Exception:
        _connected = False


def get_config():
    return {
        "running": bool(_running),
        "connected": bool(_connected),
        "channel_id": str(SLACK_CHANNEL_ID or ""),
        "bot_token_set": bool(str(SLACK_BOT_TOKEN or "").strip()),
    }
