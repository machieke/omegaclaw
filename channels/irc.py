import os
import random
import socket
import threading
import time
from collections import deque

_running = False
_sock = None
_sock_lock = threading.Lock()
_message_queue = deque()
_batch_channel_queue = deque()
_msg_lock = threading.Lock()
_channel = None
_reply_channel_hint = ""
_server = "irc.libera.chat"
_port = 6667
_nick = "mettaclaw"
_connected = False
_trace_lock = threading.Lock()
_trace_next_id = 1
_trace_pending = deque()
_trace_active = []
_last_sent_lock = threading.Lock()
_last_sent = {"target": "", "msg": "", "ts_ms": 0}


def _log(msg):
    print(f"[irc] {msg}", flush=True)


def _trace_enabled():
    return os.getenv("TRACE_PERFORMANCE", "false").strip().lower() in {"1", "true", "yes", "on"}


def _async_dispatch_enabled():
    return os.getenv("METTACLAW_ASYNC_DISPATCH", "false").strip().lower() in {"1", "true", "yes", "on"}


def _now_ms():
    return int(time.perf_counter_ns() / 1_000_000)


def _wall_ms():
    return int(time.time() * 1000)


def _send(cmd):
    with _sock_lock:
        if _sock:
            _sock.sendall((cmd + "\r\n").encode())


def _irc_max_msg_len():
    raw = os.getenv("IRC_MAX_MSG_LEN", "360").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 360
    if value < 80:
        return 80
    if value > 430:
        return 430
    return value


def _chunk_delay_s():
    raw = os.getenv("IRC_CHUNK_DELAY_MS", "80").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 80
    if value < 0:
        value = 0
    return float(value) / 1000.0


def _receive_batch_size():
    raw = os.getenv("IRC_RECEIVE_BATCH_SIZE", "3").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 3
    reply_cap_raw = os.getenv("OLLAMA_DIRECT_MAX_REPLIES", "").strip()
    try:
        reply_cap = int(reply_cap_raw)
    except ValueError:
        reply_cap = 0
    if reply_cap > 0:
        if reply_cap > 8:
            reply_cap = 8
        if value > reply_cap:
            value = reply_cap
    if value < 1:
        return 1
    if value > 8:
        return 8
    return value


def _receive_coalesce_s():
    raw = os.getenv("IRC_RECEIVE_COALESCE_MS", "250").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 250
    if value < 0:
        value = 0
    if value > 2000:
        value = 2000
    return float(value) / 1000.0


def _dedupe_window_ms():
    raw = os.getenv("IRC_DEDUP_WINDOW_MS", "800").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 800
    if value < 0:
        return 0
    if value > 5000:
        return 5000
    return value


def _connect_timeout_s():
    raw = os.getenv("IRC_CONNECT_TIMEOUT_S", "20").strip()
    try:
        value = float(raw)
    except ValueError:
        value = 20.0
    if value < 3.0:
        return 3.0
    if value > 120.0:
        return 120.0
    return value


def _reconnect_delay_s():
    raw = os.getenv("IRC_RECONNECT_DELAY_S", "5").strip()
    try:
        value = float(raw)
    except ValueError:
        value = 5.0
    if value < 1.0:
        return 1.0
    if value > 120.0:
        return 120.0
    return value


def _split_outgoing_chunks(text, max_len):
    content = " ".join(str(text or "").split()).strip()
    if not content:
        return [""]
    if len(content) <= max_len:
        return [content]
    chunks = []
    current = ""
    for word in content.split(" "):
        if not word:
            continue
        candidate = word if not current else f"{current} {word}"
        if len(candidate) <= max_len:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(word) > max_len:
            chunks.append(word[:max_len])
            word = word[max_len:]
        current = word
    if current:
        chunks.append(current)
    return chunks or [content[:max_len]]


def _trace_on_message_received(msg):
    global _trace_next_id
    if not _trace_enabled():
        return
    with _trace_lock:
        trace_id = _trace_next_id
        _trace_next_id += 1
        recv_ms = _now_ms()
        recv_wall_ms = _wall_ms()
        _trace_pending.append(
            {
                "id": trace_id,
                "recv_ms": recv_ms,
                "recv_wall_ms": recv_wall_ms,
            }
        )
    _log(f"[perf] msg#{trace_id} received at {recv_wall_ms}ms text={msg[:120]}")


def _set_last(msg, channel=""):
    _trace_on_message_received(msg)
    with _msg_lock:
        _message_queue.append((msg, str(channel or "").strip()))


def getLastMessage():
    global _reply_channel_hint
    batch = []
    batch_channels = []
    batch_size = _receive_batch_size()
    with _msg_lock:
        while _message_queue and len(batch) < batch_size:
            msg, channel = _message_queue.popleft()
            batch.append(msg)
            batch_channels.append(channel)
    if batch and len(batch) < batch_size:
        deadline = time.perf_counter() + _receive_coalesce_s()
        while len(batch) < batch_size and time.perf_counter() < deadline:
            got_item = None
            with _msg_lock:
                if _message_queue:
                    got_item = _message_queue.popleft()
            if got_item is None:
                time.sleep(0.01)
                continue
            msg, channel = got_item
            batch.append(msg)
            batch_channels.append(channel)
    tmp = " | ".join(batch)
    if batch_channels:
        with _msg_lock:
            _batch_channel_queue.append(list(batch_channels))
    first_channel = ""
    for channel in batch_channels:
        if channel:
            first_channel = channel
            break
    _reply_channel_hint = first_channel
    if _trace_enabled():
        global _trace_active
        with _trace_lock:
            if batch:
                active = []
                while _trace_pending and len(active) < len(batch):
                    active.append(_trace_pending.popleft())
                _trace_active = active
            else:
                _trace_active = []
    return tmp


def consume_batch_channels(expected_count):
    try:
        wanted = int(expected_count)
    except Exception:
        wanted = 0
    if wanted <= 0:
        return []
    with _msg_lock:
        channels = list(_batch_channel_queue.popleft()) if _batch_channel_queue else []
    if len(channels) < wanted:
        channels.extend([""] * (wanted - len(channels)))
    return channels[:wanted]


def trace_llm_request():
    if _async_dispatch_enabled():
        return
    if not _trace_enabled():
        return
    now = _now_ms()
    with _trace_lock:
        for item in _trace_active:
            if "llm_req_ms" not in item:
                item["llm_req_ms"] = now
                _log(f"[perf] msg#{item['id']} recv->llm_req={now - item['recv_ms']}ms")


def trace_llm_response():
    if _async_dispatch_enabled():
        return
    if not _trace_enabled():
        return
    now = _now_ms()
    with _trace_lock:
        for item in _trace_active:
            req = item.get("llm_req_ms")
            if req is not None and "llm_resp_ms" not in item:
                item["llm_resp_ms"] = now
                _log(f"[perf] msg#{item['id']} llm_req->llm_resp={now - req}ms")


def trace_complete_batch():
    if not _trace_enabled():
        return
    global _trace_active
    with _trace_lock:
        _trace_active = []


def is_connected():
    return _connected


def _irc_loop(channel, server, port, nick):
    global _running, _sock, _connected
    while _running:
        sock = None
        try:
            sock = socket.socket()
            sock.settimeout(_connect_timeout_s())
            sock.connect((server, port))
            sock.settimeout(None)
            _sock = sock
            _log(f"connected to {server}:{port} as {nick}")
            _send(f"NICK {nick}")
            _send(f"USER {nick} 0 * :{nick}")

            while _running:
                try:
                    data = sock.recv(4096).decode(errors="ignore")
                except OSError as exc:
                    _log(f"socket recv error: {exc}")
                    break

                for line in data.split("\r\n"):
                    if not line:
                        continue
                    if line.startswith("PING"):
                        _send(f"PONG {line.split()[1]}")
                    parts = line.split()
                    if len(parts) > 1 and parts[1] == "001":
                        _connected = True
                        _send(f"JOIN {_channel}")
                        _log(f"authenticated, joining {_channel}")
                    elif line.startswith(":") and " JOIN " in line and f":{nick}!" in line:
                        _log(f"joined {_channel}")
                    elif line.startswith(":") and " PRIVMSG " in line:
                        try:
                            prefix, trailing = line[1:].split(" PRIVMSG ", 1)
                            sender = prefix.split("!", 1)[0]
                            if str(sender or "").strip().lower() == str(nick or "").strip().lower():
                                # Ignore server echo of our own outbound messages to avoid self-feedback loops.
                                continue
                            if " :" not in trailing:
                                continue
                            target, msg = trailing.split(" :", 1)
                            _set_last(f"{sender}: {msg}", target.split()[0].strip())
                        except Exception as exc:
                            _log(f"message parse error: {exc}")
        except Exception as exc:
            _log(f"connection error: {exc}")
        finally:
            _connected = False
            with _sock_lock:
                _sock = None
            if sock is not None:
                sock.close()
            _log("disconnected")
        if _running:
            delay_s = _reconnect_delay_s()
            _log(f"reconnecting in {delay_s:.1f}s")
            time.sleep(delay_s)


def start_irc(channel, server="irc.libera.chat", port=6667, nick="mettaclaw"):
    global _running, _channel, _server, _port, _nick
    if os.getenv("IRC_RANDOM_SUFFIX", "true").strip().lower() in {"1", "true", "yes", "on"}:
        nick = f"{nick}{random.randint(1000, 9999)}"
    _running = True
    _channel = channel
    _server = server
    _port = port
    _nick = nick
    _log(f"starting IRC thread for {server}:{port} channel={channel} nick={nick}")
    t = threading.Thread(target=_irc_loop, args=(channel, server, port, nick), daemon=True)
    t.start()
    return t


def stop_irc():
    global _running
    _running = False


def join_channel(channel):
    global _channel
    ch = str(channel or "").strip()
    if not ch:
        return False
    if not ch.startswith("#"):
        ch = f"#{ch.lstrip('#')}"
    _channel = ch
    if _connected:
        _send(f"JOIN {ch}")
        _log(f"joining {ch}")
        return True
    _log(f"join queued until connected: {ch}")
    return False


def leave_channel(channel):
    global _channel, _reply_channel_hint
    ch = str(channel or "").strip()
    if not ch:
        return False
    if not ch.startswith("#"):
        ch = f"#{ch.lstrip('#')}"
    if _connected:
        _send(f"PART {ch}")
        _log(f"leaving {ch}")
        if _channel == ch:
            _channel = ""
        if _reply_channel_hint == ch:
            _reply_channel_hint = ""
        return True
    _log(f"leave ignored (not connected): {ch}")
    if _channel == ch:
        _channel = ""
    if _reply_channel_hint == ch:
        _reply_channel_hint = ""
    return False


def send_message(text, channel=None):
    sent = False
    target = str(channel or "").strip() or str(_reply_channel_hint or "").strip() or _channel
    compact = " ".join(str(text or "").split()).strip()
    if _connected and target:
        now_ms = _now_ms()
        window_ms = _dedupe_window_ms()
        if window_ms > 0:
            with _last_sent_lock:
                recent = _last_sent.copy()
                if (
                    recent.get("target") == target
                    and recent.get("msg") == compact
                    and (now_ms - int(recent.get("ts_ms", 0))) <= window_ms
                ):
                    _log(f"duplicate outbound suppressed for {target}: {compact[:160]}")
                    return
                _last_sent["target"] = target
                _last_sent["msg"] = compact
                _last_sent["ts_ms"] = now_ms

        chunks = _split_outgoing_chunks(compact, _irc_max_msg_len())
        delay_s = _chunk_delay_s()
        total_chunks = len(chunks)
        for idx, chunk in enumerate(chunks, start=1):
            _send(f"PRIVMSG {target} :{chunk}")
            sent = True
            if os.getenv("IRC_LOG_OUTBOUND", "true").strip().lower() in {"1", "true", "yes", "on"}:
                if total_chunks > 1:
                    _log(f"sent to {target} [{idx}/{total_chunks}]: {chunk[:160]}")
                else:
                    _log(f"sent to {target}: {chunk[:160]}")
            if idx < total_chunks and delay_s > 0:
                time.sleep(delay_s)
    else:
        if not _connected:
            _log(f"send dropped (not connected): {text[:120]}")
        else:
            _log(f"send dropped (no target channel): {text[:120]}")
    if _trace_enabled():
        now = _now_ms()
        with _trace_lock:
            batch = list(_trace_active)
        for item in batch:
            recv_ms = item.get("recv_ms")
            req_ms = item.get("llm_req_ms")
            resp_ms = item.get("llm_resp_ms")
            d1 = (req_ms - recv_ms) if req_ms is not None and recv_ms is not None else None
            d2 = (resp_ms - req_ms) if resp_ms is not None and req_ms is not None else None
            d3 = (now - resp_ms) if resp_ms is not None else None
            total = (now - recv_ms) if recv_ms is not None else None
            status = "sent" if sent else "dropped"
            _log(
                f"[perf] msg#{item['id']} {status} recv->llm_req={d1}ms "
                f"llm_req->llm_resp={d2}ms llm_resp->irc_send={d3}ms total={total}ms"
            )


def get_config():
    return {
        "running": bool(_running),
        "connected": bool(_connected),
        "server": str(_server or ""),
        "port": _port,
        "nick": str(_nick or ""),
        "channel": str(_channel or ""),
        "reply_channel_hint": str(_reply_channel_hint or ""),
    }
