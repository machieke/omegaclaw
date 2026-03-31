import os
import threading
import uuid

import chromadb

_CLIENT = None
_COLLECTIONS = {}
_LOCK = threading.Lock()


def _db_path():
    return os.getenv("CHROMA_DB_PATH", "/PeTTa/repos/mettaclaw/memory/chroma_db")


def _collection_name():
    return os.getenv("CHROMA_COLLECTION", "memories")


def _get_collection(dim=None):
    global _CLIENT, _COLLECTIONS
    base_name = _collection_name()
    name = f"{base_name}_{int(dim)}" if dim is not None else base_name
    with _LOCK:
        cached = _COLLECTIONS.get(name)
        if cached is not None:
            return cached
        path = _db_path()
        os.makedirs(path, exist_ok=True)
        _CLIENT = chromadb.PersistentClient(path=path)
        coll = _CLIENT.get_or_create_collection(
            name=name,
            embedding_function=None,
        )
        _COLLECTIONS[name] = coll
        return coll


def remember(content, embedding, time):
    if not isinstance(content, str):
        raise TypeError("content must be a str")
    if not isinstance(embedding, list) or not all(isinstance(x, (int, float)) for x in embedding):
        raise TypeError("embedding must be a list of floats")
    item_id = str(uuid.uuid4())
    _get_collection(len(embedding)).add(
        ids=[item_id],
        documents=[content],
        embeddings=[embedding],
        metadatas=[{"time": time}],
    )
    return item_id


def forget_ids(item_ids):
    if not isinstance(item_ids, list) or not all(isinstance(x, str) for x in item_ids):
        raise TypeError("item_ids must be a list of str")
    if not item_ids:
        return []
    _get_collection().delete(ids=item_ids)
    return item_ids


def forget_id(item_id):
    if not isinstance(item_id, str):
        raise TypeError("item_id must be a str")
    forget_ids([item_id])
    return item_id


def query_with_ids(query_embedding, k):
    if not isinstance(query_embedding, list) or not all(isinstance(x, (int, float)) for x in query_embedding):
        raise TypeError("query_embedding must be a list of floats")
    if not isinstance(k, int) or k <= 0:
        raise ValueError("k must be > 0")
    res = _get_collection(len(query_embedding)).query(
        query_embeddings=[query_embedding],
        n_results=k,
        include=["documents", "metadatas", "distances"],
    )
    ids = res.get("ids", [[]])[0]
    docs = res.get("documents", [[]])[0]
    metas = res.get("metadatas", [[]])[0]
    out = []
    for i, item_id in enumerate(ids):
        meta = metas[i] if i < len(metas) else None
        t = meta.get("time") if isinstance(meta, dict) else None
        c = docs[i] if i < len(docs) else None
        out.append([item_id, t, c])
    return out


def query(query_embedding, k):
    return [[t, c] for _, t, c in query_with_ids(query_embedding, k)]
