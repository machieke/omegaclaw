import datetime
import json
import os
import re
import urllib.error
import urllib.request

_ENSURED_MODELS = set()
_SKILL_CMD_RE = re.compile(
    r"\(\s*(remember|query|pin|shell|read-file|write-file|append-file|send|search|metta)\b"
)
_PLUS_RE = re.compile(r"\b(-?\d+)\s*(?:\+|plus)\s*(-?\d+)\b")
_TIME_RE = re.compile(r"\b(what(?:'s| is)?(?: the)? time|time is it|current time)\b")
_HELLO_RE = re.compile(r"^\s*(hi|hello|hey)\b")


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
    if " | " in text:
        text = text.split(" | ")[-1].strip()
    if ": " in text:
        head, tail = text.split(": ", 1)
        if head and " " not in head:
            return tail.strip()
    return text


def _heuristic_skill_from_user_message(user_msg):
    text = _strip_user_prefix(user_msg)
    if not text:
        return None
    lower = text.lower()

    plus = _PLUS_RE.search(lower)
    if plus:
        lhs = int(plus.group(1))
        rhs = int(plus.group(2))
        return f'((send {json.dumps(f"{lhs} plus {rhs} is {lhs + rhs}.")}))'

    if _TIME_RE.search(lower):
        now = datetime.datetime.now().astimezone()
        stamp = now.strftime("%Y-%m-%d %H:%M:%S %Z")
        return f'((send {json.dumps(f"Current time is {stamp}.")}))'

    if _HELLO_RE.search(lower):
        return '((send "Hello."))'

    return None


def _is_true(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def normalize_skill_output(s, user_msg="", msg_new=False, max_send_chars=360):
    text = str(s or "").strip()
    if not text:
        return "()"

    if not _is_true(msg_new):
        return "()"

    # If the model already emitted one of the supported skill commands,
    # keep it and just normalize parenthesis framing.
    if _SKILL_CMD_RE.search(text):
        return balance_parentheses(text)

    heuristic = _heuristic_skill_from_user_message(user_msg)
    if heuristic is not None:
        return heuristic

    plain = text.replace("\r\n", "\n").replace("\r", "\n")
    plain = plain.replace("_newline_", "\n").replace("_apostrophe_", "'").replace("_quote_", '"')
    if len(plain) > max_send_chars:
        plain = plain[: max_send_chars - 3] + "..."
    return f"((send {json.dumps(plain)}))"


def _ollama_base_url():
    return os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")


def _request_json(method, path, payload=None, timeout=180):
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


def ollama_chat(model, prompt, max_tokens, effort):
    env_model = os.getenv("OLLAMA_MODEL", "").strip()
    chosen_model = env_model or str(model or "").strip() or "llama3.1:8b"
    _ensure_ollama_model(chosen_model)
    payload = {
        "model": chosen_model,
        "messages": [{"role": "user", "content": str(prompt)}],
        "stream": False,
        "options": {
            "num_predict": int(max_tokens),
        },
    }
    _ = effort  # Reserved for future mapping to Ollama options.
    res = _post_json("/api/chat", payload)
    msg = (res.get("message") or {}).get("content")
    if isinstance(msg, str):
        return msg
    # Some Ollama responses use top-level "response".
    fallback = res.get("response")
    if isinstance(fallback, str):
        return fallback
    return str(res)


def ollama_embedding(text):
    model = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
    _ensure_ollama_model(model)
    # Prefer /api/embed (newer), fall back to /api/embeddings.
    try:
        res = _post_json(
            "/api/embed",
            {
                "model": model,
                "input": str(text),
            },
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
    )
    embedding = res.get("embedding")
    if isinstance(embedding, list):
        return embedding
    raise RuntimeError(f"Ollama embedding response missing embedding vector: {res}")
