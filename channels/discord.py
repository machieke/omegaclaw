import threading
import time

import requests

_running = False
_last_message = ""
_msg_lock = threading.Lock()
_connected = False

DISCORD_BOT_TOKEN = ""
DISCORD_CHANNEL_ID = ""
_last_seen_id = ""
_POLL_INTERVAL_S = 2.0


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
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "Content-Type": "application/json",
    }


def _channel_messages_url():
    return f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages"


def _id_int(value):
    try:
        return int(str(value))
    except Exception:
        return -1


def _poll_loop():
    global _connected, _last_seen_id
    while _running:
        token = str(DISCORD_BOT_TOKEN or "").strip()
        channel_id = str(DISCORD_CHANNEL_ID or "").strip()
        if not token or not channel_id:
            _connected = False
            time.sleep(1.0)
            continue
        try:
            response = requests.get(_channel_messages_url(), headers=_headers(), params={"limit": 25}, timeout=15)
            if response.status_code < 200 or response.status_code >= 300:
                _connected = False
                time.sleep(_POLL_INTERVAL_S)
                continue
            payload = response.json()
            if not isinstance(payload, list):
                _connected = False
                time.sleep(_POLL_INTERVAL_S)
                continue
            _connected = True
            if not payload:
                time.sleep(_POLL_INTERVAL_S)
                continue
            items = sorted(
                [item for item in payload if isinstance(item, dict)],
                key=lambda item: _id_int(item.get("id")),
            )
            newest_id = str(items[-1].get("id", "")).strip()
            if not _last_seen_id:
                _last_seen_id = newest_id
                time.sleep(_POLL_INTERVAL_S)
                continue
            baseline = _id_int(_last_seen_id)
            for item in items:
                msg_id = _id_int(item.get("id"))
                if msg_id <= baseline:
                    continue
                author = item.get("author") or {}
                if bool(author.get("bot")):
                    continue
                text = str(item.get("content") or "").strip()
                if not text:
                    continue
                sender = str(author.get("username") or "").strip() or "discord-user"
                _set_last(f"{sender}: {text}")
            _last_seen_id = newest_id or _last_seen_id
        except Exception:
            _connected = False
        time.sleep(_POLL_INTERVAL_S)


def start_discord(bot_token, channel_id):
    global _running, DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID, _last_seen_id
    DISCORD_BOT_TOKEN = str(bot_token or "").strip()
    DISCORD_CHANNEL_ID = str(channel_id or "").strip()
    _last_seen_id = ""
    _running = True
    t = threading.Thread(target=_poll_loop, daemon=True)
    t.start()
    return t


def stop_discord():
    global _running
    _running = False


def is_connected():
    return _connected


def send_message(text):
    global _connected
    token = str(DISCORD_BOT_TOKEN or "").strip()
    channel_id = str(DISCORD_CHANNEL_ID or "").strip()
    if not token or not channel_id:
        return
    payload = {"content": str(text or "").replace("\\n", "\n")}
    try:
        response = requests.post(_channel_messages_url(), headers=_headers(), json=payload, timeout=15)
        _connected = 200 <= response.status_code < 300
    except Exception:
        _connected = False


def get_config():
    return {
        "running": bool(_running),
        "connected": bool(_connected),
        "channel_id": str(DISCORD_CHANNEL_ID or ""),
        "bot_token_set": bool(str(DISCORD_BOT_TOKEN or "").strip()),
    }
