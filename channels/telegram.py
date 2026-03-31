import threading
import time

import requests

_running = False
_last_message = ""
_msg_lock = threading.Lock()
_connected = False

TG_BOT_TOKEN = ""
TG_CHAT_ID = ""
_POLL_TIMEOUT_S = 20


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


def _api_url(method):
    return f"https://api.telegram.org/bot{TG_BOT_TOKEN}/{method}"


def _poll_loop():
    global _connected
    offset = 0
    while _running:
        if not str(TG_BOT_TOKEN or "").strip():
            _connected = False
            time.sleep(1.0)
            continue
        try:
            params = {"timeout": _POLL_TIMEOUT_S}
            if offset > 0:
                params["offset"] = offset
            response = requests.get(_api_url("getUpdates"), params=params, timeout=_POLL_TIMEOUT_S + 5)
            payload = response.json()
            if not isinstance(payload, dict) or not payload.get("ok"):
                _connected = False
                time.sleep(1.0)
                continue
            _connected = True
            updates = payload.get("result")
            if not isinstance(updates, list):
                continue
            for item in updates:
                if not isinstance(item, dict):
                    continue
                update_id = item.get("update_id")
                if isinstance(update_id, int):
                    offset = max(offset, update_id + 1)
                msg = item.get("message") or item.get("edited_message")
                if not isinstance(msg, dict):
                    continue
                chat = msg.get("chat") or {}
                chat_id = str(chat.get("id", "")).strip()
                wanted_chat = str(TG_CHAT_ID or "").strip()
                if wanted_chat and chat_id != wanted_chat:
                    continue
                text = str(msg.get("text") or "").strip()
                if not text:
                    continue
                user = msg.get("from") or {}
                sender = str(user.get("username") or "").strip()
                if not sender:
                    sender = str(user.get("first_name") or "").strip()
                if not sender:
                    sender = "telegram-user"
                _set_last(f"{sender}: {text}")
        except Exception:
            _connected = False
            time.sleep(1.0)


def start_telegram(bot_token, chat_id):
    global _running, TG_BOT_TOKEN, TG_CHAT_ID
    TG_BOT_TOKEN = str(bot_token or "").strip()
    TG_CHAT_ID = str(chat_id or "").strip()
    _running = True
    t = threading.Thread(target=_poll_loop, daemon=True)
    t.start()
    return t


def stop_telegram():
    global _running
    _running = False


def is_connected():
    return _connected


def send_message(text):
    global _connected
    token = str(TG_BOT_TOKEN or "").strip()
    chat_id = str(TG_CHAT_ID or "").strip()
    if not token or not chat_id:
        return
    body = {"chat_id": chat_id, "text": str(text or "").replace("\\n", "\n")}
    try:
        response = requests.post(_api_url("sendMessage"), json=body, timeout=15)
        payload = response.json()
        _connected = isinstance(payload, dict) and bool(payload.get("ok"))
    except Exception:
        _connected = False


def get_config():
    return {
        "running": bool(_running),
        "connected": bool(_connected),
        "chat_id": str(TG_CHAT_ID or ""),
        "bot_token_set": bool(str(TG_BOT_TOKEN or "").strip()),
    }
