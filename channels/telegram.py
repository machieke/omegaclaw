import threading
import time
import os
import json

import requests

_running = False
_last_message = ""
_msg_lock = threading.Lock()
_connected = False
_last_poll_issue = ""
_reply_chat_id_hint = ""

TG_BOT_TOKEN = ""
TG_CHAT_ID = ""
_POLL_TIMEOUT_S = 20


def _log(msg):
    print(f"[telegram] {msg}", flush=True)


def _set_last(msg, chat_id=""):
    global _last_message, _reply_chat_id_hint
    with _msg_lock:
        if _last_message == "":
            _last_message = msg
        else:
            _last_message = _last_message + " | " + msg
        chat_id_text = str(chat_id or "").strip()
        if chat_id_text:
            _reply_chat_id_hint = chat_id_text


def _set_poll_issue(issue):
    global _last_poll_issue
    text = str(issue or "").strip()
    if text == _last_poll_issue:
        return
    _last_poll_issue = text
    if text:
        _log(f"poll issue: {text}")


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
            _set_poll_issue("missing bot token")
            time.sleep(1.0)
            continue
        try:
            params = {
                "timeout": _POLL_TIMEOUT_S,
                "allowed_updates": json.dumps(
                    ["message", "edited_message", "channel_post", "edited_channel_post"]
                ),
            }
            if offset > 0:
                params["offset"] = offset
            response = requests.get(_api_url("getUpdates"), params=params, timeout=_POLL_TIMEOUT_S + 5)
            payload = response.json()
            if not isinstance(payload, dict):
                _connected = False
                _set_poll_issue("invalid getUpdates payload")
                time.sleep(1.0)
                continue
            if not payload.get("ok"):
                _connected = False
                code = payload.get("error_code")
                desc = payload.get("description")
                _set_poll_issue(f"getUpdates failed code={code} desc={desc}")
                time.sleep(1.0)
                continue
            _set_poll_issue("")
            _connected = True
            updates = payload.get("result")
            if not isinstance(updates, list):
                _set_poll_issue("updates result is not a list")
                continue
            wanted_chat = str(TG_CHAT_ID or "").strip()
            for item in updates:
                if not isinstance(item, dict):
                    continue
                update_id = item.get("update_id")
                if isinstance(update_id, int):
                    offset = max(offset, update_id + 1)
                update_kind = ""
                msg = item.get("message")
                if isinstance(msg, dict):
                    update_kind = "message"
                if not isinstance(msg, dict):
                    msg = item.get("edited_message")
                    if isinstance(msg, dict):
                        update_kind = "edited_message"
                if not isinstance(msg, dict):
                    msg = item.get("channel_post")
                    if isinstance(msg, dict):
                        update_kind = "channel_post"
                if not isinstance(msg, dict):
                    msg = item.get("edited_channel_post")
                    if isinstance(msg, dict):
                        update_kind = "edited_channel_post"
                if not isinstance(msg, dict):
                    continue
                text = str(msg.get("text") or "").strip()
                chat = msg.get("chat") or {}
                chat_id = str(chat.get("id", "")).strip()
                if wanted_chat and chat_id != wanted_chat:
                    if text:
                        _log(f"chat mismatch {update_kind} chat_id={chat_id} configured={wanted_chat}; using source chat")
                if not text:
                    continue
                user = msg.get("from") or {}
                if bool(user.get("is_bot")):
                    continue
                sender = str(user.get("username") or "").strip()
                if not sender:
                    sender = str(user.get("first_name") or "").strip()
                if not sender:
                    sender_chat = msg.get("sender_chat") or {}
                    sender = str(sender_chat.get("username") or "").strip()
                    if not sender:
                        sender = str(sender_chat.get("title") or "").strip()
                if not sender:
                    sender = "telegram-user"
                _set_last(f"{sender}: {text}", chat_id=chat_id)
                _log(f"received {update_kind} from {sender}: {text[:120]}")
        except Exception:
            _connected = False
            _set_poll_issue("poll exception")
            time.sleep(1.0)


def start_telegram(bot_token, chat_id):
    global _running, TG_BOT_TOKEN, TG_CHAT_ID
    TG_BOT_TOKEN = str(bot_token or "").strip()
    TG_CHAT_ID = str(chat_id or "").strip()
    _running = True
    _log(f"starting poller chat_id={TG_CHAT_ID}")
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
    with _msg_lock:
        hint = str(_reply_chat_id_hint or "").strip()
    chat_id = hint or str(TG_CHAT_ID or "").strip()
    if not token or not chat_id:
        return
    body = {"chat_id": chat_id, "text": str(text or "").replace("\\n", "\n")}
    try:
        response = requests.post(_api_url("sendMessage"), json=body, timeout=15)
        payload = response.json()
        _connected = isinstance(payload, dict) and bool(payload.get("ok"))
        if not _connected:
            print(f"[telegram] send failed: {payload}", flush=True)
        else:
            result = payload.get("result") or {}
            chat = result.get("chat") or {}
            chat_id = str(chat.get("id", "")).strip()
            chat_title = str(chat.get("title") or "").strip()
            chat_username = str(chat.get("username") or "").strip()
            msg_id = result.get("message_id")
            _log(
                f"sent message target={body.get('chat_id')} chat_id={chat_id} "
                f"title={chat_title} username={chat_username} message_id={msg_id}"
            )
    except Exception:
        _connected = False
        _log("send failed")


def get_config():
    token = str(TG_BOT_TOKEN or os.getenv("TG_BOT_TOKEN", "")).strip()
    chat_id = str(TG_CHAT_ID or os.getenv("TG_CHAT_ID", "")).strip()
    return {
        "running": bool(_running),
        "connected": bool(_connected),
        "chat_id": chat_id,
        "bot_token_set": bool(token),
    }
