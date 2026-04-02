import asyncio
import json
import os
import re
import threading
import time
import uuid
from collections import deque

try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
except Exception:
    FastAPI = None
    WebSocket = None
    WebSocketDisconnect = Exception

try:
    import uvicorn
except Exception:
    uvicorn = None

_running = False
_connected = False

_message_queue = deque()
_msg_lock = threading.Lock()

_clients = {}
_client_user = {}
_clients_lock = threading.Lock()

_server_loop = None
_server_loop_lock = threading.Lock()
_server_thread = None
_server = None

_host = "0.0.0.0"
_port = 8012
_default_user = "vibe-user"

_response_counter = 0
_response_lock = threading.Lock()


def _log(msg):
    print(f"[vibe-voice] {msg}", flush=True)


def _normalize_user(user):
    text = str(user or "").strip().lower()
    if not text:
        text = str(_default_user or "vibe-user").strip().lower()
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"[^a-z0-9._-]", "", text)
    if not text:
        text = "vibe-user"
    return text


def _normalize_text(text):
    cleaned = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    cleaned = "\n".join(line.strip() for line in cleaned.split("\n"))
    return cleaned.strip()


def _set_last(sender, text):
    user = _normalize_user(sender)
    body = _normalize_text(text)
    if not body:
        return
    with _msg_lock:
        _message_queue.append(f"{user}: {body}")


def getLastMessage():
    batch = []
    with _msg_lock:
        while _message_queue:
            batch.append(_message_queue.popleft())
    return " | ".join(batch)


def _next_response_id():
    global _response_counter
    with _response_lock:
        _response_counter += 1
        return _response_counter


def _set_server_loop(loop):
    global _server_loop
    with _server_loop_lock:
        _server_loop = loop


def _get_server_loop():
    with _server_loop_lock:
        return _server_loop


async def _broadcast_json(payload):
    stale = []
    with _clients_lock:
        items = list(_clients.items())
    for client_id, websocket in items:
        try:
            await websocket.send_json(payload)
        except Exception:
            stale.append(client_id)
    if stale:
        with _clients_lock:
            for client_id in stale:
                _clients.pop(client_id, None)
                _client_user.pop(client_id, None)
            global _connected
            _connected = bool(_clients)


def _broadcast_from_sync(payload, timeout_s=2.5):
    loop = _get_server_loop()
    if loop is None:
        return False
    future = asyncio.run_coroutine_threadsafe(_broadcast_json(payload), loop)
    try:
        future.result(timeout=max(0.2, float(timeout_s)))
        return True
    except Exception:
        return False


def send_message(text):
    message = str(text or "").replace("\\n", "\n")
    message = _normalize_text(message)
    if not message:
        return
    response_id = _next_response_id()
    ts_ms = int(time.time() * 1000)
    payload = {
        "event": "assistant_message",
        "response_id": response_id,
        "text": message,
        "ts_ms": ts_ms,
        "channel": "vibe-voice",
    }
    _broadcast_from_sync(payload)


def is_connected():
    return _connected


def _effective_host(host):
    value = str(host or os.getenv("VIBE_VOICE_HOST", _host)).strip()
    return value or "0.0.0.0"


def _effective_port(port):
    raw = str(port or os.getenv("VIBE_VOICE_PORT", _port)).strip()
    try:
        value = int(raw)
    except Exception:
        value = 8012
    if value < 1:
        value = 8012
    return value


def _effective_default_user(user):
    return _normalize_user(user or os.getenv("VIBE_VOICE_DEFAULT_USER", _default_user))


def _create_app():
    if FastAPI is None:
        return None

    app = FastAPI(title="MeTTaClaw vibe-voice")

    @app.on_event("startup")
    async def _startup():
        _set_server_loop(asyncio.get_running_loop())

    @app.on_event("shutdown")
    async def _shutdown():
        _set_server_loop(None)
        with _clients_lock:
            _clients.clear()
            _client_user.clear()
            global _connected
            _connected = False

    @app.get("/health")
    async def _health():
        return {
            "status": "ok",
            "channel": "vibe-voice",
            "connected": bool(is_connected()),
        }

    @app.websocket("/ws/vibe")
    async def _ws_vibe(websocket: WebSocket):
        global _connected
        await websocket.accept()
        client_id = uuid.uuid4().hex[:10]
        with _clients_lock:
            _clients[client_id] = websocket
            _client_user[client_id] = _normalize_user(_default_user)
            _connected = True

        await websocket.send_json(
            {
                "event": "ready",
                "session_id": client_id,
                "channel": "vibe-voice",
                "transport": "websocket",
                "tts": "browser-speechsynthesis",
            }
        )
        _log(f"client connected id={client_id}")

        try:
            while True:
                incoming = await websocket.receive()
                msg_type = str(incoming.get("type") or "")
                if msg_type == "websocket.disconnect":
                    break

                raw_text = ""
                if incoming.get("text") is not None:
                    raw_text = str(incoming.get("text") or "")
                elif incoming.get("bytes") is not None:
                    # Binary audio frames can be sent by clients based on realtime demos.
                    # This channel currently uses browser-side STT and ignores raw bytes.
                    continue
                if not raw_text:
                    continue

                try:
                    payload = json.loads(raw_text)
                except Exception:
                    payload = {"event": "user_text", "text": raw_text}

                event = str(payload.get("event") or "").strip().lower()
                if event in {"ping"}:
                    await websocket.send_json({"event": "pong", "ts_ms": int(time.time() * 1000)})
                    continue

                if event == "set_user":
                    user = _normalize_user(payload.get("user"))
                    with _clients_lock:
                        _client_user[client_id] = user
                    await websocket.send_json({"event": "user_updated", "user": user})
                    continue

                if event in {"user_text", "final_transcript", "transcript"}:
                    text = _normalize_text(payload.get("text"))
                    if not text:
                        continue
                    with _clients_lock:
                        user = _normalize_user(payload.get("user") or _client_user.get(client_id) or _default_user)
                        _client_user[client_id] = user
                    _set_last(user, text)
                    await _broadcast_json(
                        {
                            "event": "final_transcript",
                            "user": user,
                            "text": text,
                            "ts_ms": int(time.time() * 1000),
                        }
                    )
                    continue

                if event == "partial_transcript":
                    text = _normalize_text(payload.get("text"))
                    if not text:
                        continue
                    with _clients_lock:
                        user = _normalize_user(payload.get("user") or _client_user.get(client_id) or _default_user)
                    await _broadcast_json(
                        {
                            "event": "partial_transcript",
                            "user": user,
                            "text": text,
                            "ts_ms": int(time.time() * 1000),
                        }
                    )
                    continue
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            _log(f"websocket loop error id={client_id}: {exc}")
        finally:
            with _clients_lock:
                _clients.pop(client_id, None)
                _client_user.pop(client_id, None)
                _connected = bool(_clients)
            _log(f"client disconnected id={client_id}")

    return app


def start_vibe_voice(host="", port=0, default_user=""):
    global _running, _server_thread, _server, _host, _port, _default_user

    if _running and _server_thread is not None and _server_thread.is_alive():
        return _server_thread

    if FastAPI is None or uvicorn is None:
        raise RuntimeError("vibe-voice backend requires fastapi and uvicorn")

    _host = _effective_host(host)
    _port = _effective_port(port)
    _default_user = _effective_default_user(default_user)

    app = _create_app()
    if app is None:
        raise RuntimeError("failed to initialize vibe-voice app")

    config = uvicorn.Config(
        app,
        host=_host,
        port=_port,
        log_level=str(os.getenv("VIBE_VOICE_LOG_LEVEL", "warning")),
        proxy_headers=True,
    )
    _server = uvicorn.Server(config)
    _running = True

    def _run():
        global _running
        try:
            _server.run()
        finally:
            _running = False
            _set_server_loop(None)

    _server_thread = threading.Thread(target=_run, name="vibe-voice-server", daemon=True)
    _server_thread.start()
    _log(f"server starting host={_host} port={_port} default_user={_default_user}")
    return _server_thread


def stop_vibe_voice():
    global _running
    _running = False
    if _server is not None:
        try:
            _server.should_exit = True
        except Exception:
            pass


def get_config():
    with _clients_lock:
        client_count = len(_clients)
    thread_alive = bool(_server_thread is not None and _server_thread.is_alive())
    return {
        "running": bool(_running and thread_alive),
        "connected": bool(_connected),
        "host": str(_host),
        "port": int(_port),
        "default_user": str(_default_user),
        "client_count": int(client_count),
    }
