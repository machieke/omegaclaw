import os
import random
import socket
import threading

_running = False
_sock = None
_sock_lock = threading.Lock()
_last_message = ""
_msg_lock = threading.Lock()
_channel = None
_connected = False


def _log(msg):
    print(f"[irc] {msg}", flush=True)


def _send(cmd):
    with _sock_lock:
        if _sock:
            _sock.sendall((cmd + "\r\n").encode())


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


def is_connected():
    return _connected


def _irc_loop(channel, server, port, nick):
    global _running, _sock, _connected
    sock = None
    try:
        sock = socket.socket()
        sock.connect((server, port))
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
                        if " :" not in trailing:
                            continue
                        msg = trailing.split(" :", 1)[1]
                        _set_last(f"{sender}: {msg}")
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


def start_irc(channel, server="irc.libera.chat", port=6667, nick="mettaclaw"):
    global _running, _channel
    if os.getenv("IRC_RANDOM_SUFFIX", "true").strip().lower() in {"1", "true", "yes", "on"}:
        nick = f"{nick}{random.randint(1000, 9999)}"
    _running = True
    _channel = channel
    _log(f"starting IRC thread for {server}:{port} channel={channel} nick={nick}")
    t = threading.Thread(target=_irc_loop, args=(channel, server, port, nick), daemon=True)
    t.start()
    return t


def stop_irc():
    global _running
    _running = False


def send_message(text):
    if _connected:
        _send(f"PRIVMSG {_channel} :{text}")
    else:
        _log(f"send dropped (not connected): {text[:120]}")
