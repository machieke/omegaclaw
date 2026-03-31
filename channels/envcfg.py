import os


def get(name, default=""):
    key = str(name or "").strip()
    if not key:
        return str(default)
    return str(os.getenv(key, default))


def get_int(name, default=0):
    raw = get(name, default)
    try:
        return int(str(raw).strip())
    except Exception:
        try:
            return int(default)
        except Exception:
            return 0
