"""Embedding 客户端 + 向量缓存 + 矩阵索引（检索、闸门、去重、分类共用）。

一个模型只算一次：
  - 知识正文向量落 atom_embedding（按内容签名，文本变了才重算）
  - 原型/查询等短文本落 embedding_cache
  - 全量矩阵导出成 store/vec_cache/<model>.npy，检索时直接矩阵乘，不用逐条 base64 解码

配置（config.json -> embedding）：
  enabled / base_url / model / api_key / batch_size / query_timeout_s
  text_mode: title_summary（默认，标题+摘要）| full（标题+正文）
接口不可达时所有调用返回 None / 抛 RuntimeError，由调用方决定降级行为。
"""
import array
import base64
import hashlib
import json
import math
import os
import re
import time
import urllib.error
import urllib.request

import config
import paths

CACHE_DIR = paths.VEC_CACHE

DEFAULT_TIMEOUT = 120
QUERY_TIMEOUT = 4
MAX_TEXT = 1200
TEXT_MODES = ("title_summary", "full")


# ---------------------------------------------------------------- 配置
def cfg():
    return config.load().get("embedding") or {}


def healthy(c=None):
    """开关打开且填了 base_url / model 才算可用。"""
    c = c if c is not None else cfg()
    return bool(c.get("enabled") and (c.get("base_url") or "").strip()
                and (c.get("model") or "").strip())


def model(c=None):
    return (c if c is not None else cfg()).get("model") or ""


# ---------------------------------------------------------------- 客户端
def embed_texts(c, texts, timeout=None):
    """OpenAI 兼容 /embeddings；按 batch_size 分批。失败抛 RuntimeError/URLError。"""
    base = (c.get("base_url") or "").rstrip("/")
    mdl = c.get("model") or ""
    if not base or not mdl:
        raise RuntimeError("未配置 embedding.base_url / model")
    batch = max(1, int(c.get("batch_size") or 32))
    out = []
    for i in range(0, len(texts), batch):
        part = texts[i:i + batch]
        payload = json.dumps({"model": mdl, "input": part}).encode()
        req = urllib.request.Request(
            base + "/embeddings", data=payload, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {c.get('api_key') or 'none'}"})
        with urllib.request.urlopen(req, timeout=timeout or DEFAULT_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        items = sorted(data.get("data") or [], key=lambda x: x.get("index", 0))
        vecs = [it.get("embedding") for it in items]
        if len(vecs) != len(part) or any(not v for v in vecs):
            raise RuntimeError(f"embedding 返回数量不符：期望 {len(part)}，实际 {len(vecs)}")
        out.extend(vecs)
    return out


_Q_CACHE = {}


def embed_query(query, c=None, timeout=None):
    """查询向量；接口不可达/超时返回 None，让检索降级为纯关键词。

    面板连续搜索会重复同一批查询，缓存最近 256 条（命中即 0 网络）。
    """
    c = c if c is not None else cfg()
    key = (model(c), query)
    if key in _Q_CACHE:
        return _Q_CACHE[key]
    try:
        vecs = embed_texts(c, [query], timeout=timeout
                           or float(c.get("query_timeout_s") or QUERY_TIMEOUT))
    except Exception:  # noqa: BLE001
        return None
    vec = vecs[0] if vecs else None
    if vec:
        if len(_Q_CACHE) >= 256:
            _Q_CACHE.clear()
        _Q_CACHE[key] = vec
    return vec


# ---------------------------------------------------------------- 编码/相似度
def encode(vec):
    """向量落库：float32 二进制。比 base64 小 25%，解码走 frombytes 几乎零成本。"""
    return array.array("f", [float(x) for x in vec]).tobytes()


def decode(v):
    """兼容历史数据：新库是 BLOB，老库是 base64 文本。"""
    a = array.array("f")
    if isinstance(v, (bytes, bytearray, memoryview)):
        a.frombytes(bytes(v))
    else:
        a.frombytes(base64.b64decode(v))
    return a.tolist()


def norm(vec):
    return math.sqrt(sum(x * x for x in vec)) or 1.0


def cosine(a, b):
    if not a or not b or len(a) != len(b):
        return -1.0
    return sum(x * y for x, y in zip(a, b)) / (norm(a) * norm(b))


def max_similarity(vecs, seeds):
    """每行向量对 seeds 取最大余弦。有 numpy 时一次矩阵乘，否则退回逐条循环。"""
    if not vecs or not seeds:
        return []
    np = _numpy()
    if np is not None:
        a = np.asarray(vecs, dtype="float32")
        b = np.asarray(seeds, dtype="float32")
        if a.ndim == 2 and b.ndim == 2 and a.shape[1] == b.shape[1]:
            a = a / (np.linalg.norm(a, axis=1, keepdims=True) + 1e-9)
            b = b / (np.linalg.norm(b, axis=1, keepdims=True) + 1e-9)
            return (a @ b.T).max(axis=1).tolist()
    return [max(cosine(v, s) for s in seeds) for v in vecs]


def signature(text, mode=None):
    return hashlib.sha1(f"{mode or ''}\n{text}".encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- 文本
def text_mode(c=None):
    m = str((c if c is not None else cfg()).get("text_mode") or "").strip()
    return m if m in TEXT_MODES else "title_summary"


def text_of(row, mode=None):
    """参与向量化的文本。title_summary 取摘要（没有摘要退正文），full 取全文。

    两边用同一份文本很重要：闸门判定「是否与主题相关」和检索「是否与查询相近」
    必须作用在同一个表征上，否则命中率无从解释。
    """
    mode = mode or text_mode()
    title = row["title"] or ""
    if mode == "full":
        body = row["body"] or ""
    else:
        body = row["summary"] or row["body"] or ""
    return (title + "\n" + body)[:MAX_TEXT]


# ---------------------------------------------------------------- 缓存读写
def ensure(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS atom_embedding(
        atom_id TEXT NOT NULL, model TEXT NOT NULL, sig TEXT NOT NULL,
        vec TEXT NOT NULL, updated_at INTEGER NOT NULL, PRIMARY KEY(atom_id, model))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS embedding_cache(
        key TEXT NOT NULL, model TEXT NOT NULL, sig TEXT NOT NULL,
        vec TEXT NOT NULL, updated_at INTEGER NOT NULL, PRIMARY KEY(key, model))""")


def vectors_for(conn, c, rows):
    """批量取知识向量：命中缓存复用，缺失的批量请求并落库。

    返回 (vectors, embedded)：vectors 形如 {atom_id: [float]}；embedded 是新算的条数。
    """
    mdl = model(c)
    ids = [r["atom_id"] for r in rows]
    cached = {}
    for i in range(0, len(ids), 500):   # 只查本批，避免每批都全表扫 atom_embedding
        part = ids[i:i + 500]
        q = ("SELECT atom_id, sig, vec FROM atom_embedding WHERE model=?"
             f" AND atom_id IN ({','.join('?' * len(part))})")
        for r in conn.execute(q, (mdl, *part)):
            cached[r["atom_id"]] = (r["sig"], r["vec"])
    out, todo = {}, []
    for row in rows:
        text = text_of(row)
        sig = signature(text, text_mode(c))
        hit = cached.get(row["atom_id"])
        if hit and hit[0] == sig:
            try:
                out[row["atom_id"]] = decode(hit[1])
                continue
            except (ValueError, TypeError):
                pass
        todo.append((row["atom_id"], text, sig))

    embedded = 0
    step = max(1, int(c.get("batch_size") or 32))
    for i in range(0, len(todo), step):
        part = todo[i:i + step]
        vecs = embed_texts(c, [t for _a, t, _s in part])
        embedded += len(vecs)
        now = int(time.time())
        for (aid, _text, sig), vec in zip(part, vecs):
            out[aid] = vec
            conn.execute(
                "INSERT OR REPLACE INTO atom_embedding(atom_id,model,sig,vec,updated_at)"
                " VALUES (?,?,?,?,?)", (aid, mdl, sig, encode(vec), now))
    if embedded:
        conn.commit()
    return out, embedded


def text_vector(conn, c, key, text, timeout=None):
    """任意短文本的向量（原型、示例句），按 key 缓存；失败返回 None。"""
    mdl = model(c)
    sig = signature(text)
    row = conn.execute("SELECT sig, vec FROM embedding_cache WHERE key=? AND model=?",
                       (key, mdl)).fetchone()
    if row and row["sig"] == sig:
        try:
            return decode(row["vec"])
        except (ValueError, TypeError):
            pass
    try:
        vec = embed_texts(c, [text], timeout=timeout)[0]
    except Exception:  # noqa: BLE001
        return None
    conn.execute("INSERT OR REPLACE INTO embedding_cache(key,model,sig,vec,updated_at)"
                 " VALUES (?,?,?,?,?)", (key, mdl, sig, encode(vec), int(time.time())))
    conn.commit()
    return vec


# ---------------------------------------------------------------- 矩阵索引
def _safe(name):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name or "default")[:80]


def _numpy():
    try:
        import numpy as np  # noqa: PLC0415
        return np
    except Exception:  # noqa: BLE001
        return None


def _meta_path(mdl):
    return os.path.join(CACHE_DIR, _safe(mdl) + ".json")


def _npy_path(mdl):
    return os.path.join(CACHE_DIR, _safe(mdl) + ".npy")


def _stamp(conn, mdl, mode):
    row = conn.execute("SELECT COUNT(*), COALESCE(MAX(updated_at),0)"
                       " FROM atom_embedding WHERE model=?", (mdl,)).fetchone()
    return {"model": mdl, "count": row[0], "max_updated_at": row[1], "text_mode": mode}


_MAT_CACHE = {}


def load_matrix(conn, c=None):
    """返回 (ids, matrix)：matrix 是已归一化的 float32 二维数组。

    没有 numpy 时返回 (ids, None)，调用方退回逐条余弦（Python 循环）。
    两级缓存：进程内（命中直接返回，不碰磁盘）+ store/vec_cache 的 .npy
    （跨进程复用，避免每次检索都解码全量 base64）。失效条件是
    (条数, 最大更新时间, text_mode, 模型) 变化。
    """
    c = c if c is not None else cfg()
    mdl = model(c)
    mode = text_mode(c)
    np = _numpy()
    if np is None:
        return _rows(conn, mdl)[0], None

    stamp = _stamp(conn, mdl, mode)
    key = (mdl, mode, stamp["count"], stamp["max_updated_at"])
    mem = _MAT_CACHE.get(key)
    if mem is not None:
        return mem

    ids, mat = [], None
    mpath, npath = _meta_path(mdl), _npy_path(mdl)
    if os.path.exists(mpath) and os.path.exists(npath):
        try:
            meta = json.load(open(mpath, encoding="utf-8"))
            if (meta.get("count") == stamp["count"]
                    and meta.get("max_updated_at") == stamp["max_updated_at"]
                    and meta.get("text_mode") == mode):
                candidate = np.load(npath)
                if candidate.ndim == 2:
                    ids, mat = list(meta.get("ids") or []), candidate
        except (OSError, ValueError):
            pass

    if mat is None:
        ids, mat = _build_matrix(conn, mdl, mode, np)
        if mat is None:
            return ids, None

    if len(_MAT_CACHE) > 2:
        _MAT_CACHE.clear()
    _MAT_CACHE[key] = (ids, mat)
    return ids, mat


def _rows(conn, mdl):
    ids, vecs = [], []
    for r in conn.execute("SELECT atom_id, vec FROM atom_embedding WHERE model=?", (mdl,)):
        try:
            v = decode(r["vec"])
        except (ValueError, TypeError):
            continue
        ids.append(r["atom_id"])
        vecs.append(v)
    return ids, vecs


def _build_matrix(conn, mdl, mode, np):
    """从 atom_embedding 全量重建矩阵并写盘；维度不一致（换过模型残留）时放弃。"""
    ids, vecs = _rows(conn, mdl)
    if not ids:
        return [], None
    try:
        mat = np.asarray(vecs, dtype="float32")
    except ValueError:
        return ids, None
    if mat.ndim != 2 or mat.shape[1] == 0:
        return ids, None
    mat /= (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        np.save(_npy_path(mdl), mat)
        meta = dict(_stamp(conn, mdl, mode), dim=int(mat.shape[1]), ids=ids)
        tmp = _meta_path(mdl) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
        os.replace(tmp, _meta_path(mdl))
    except OSError:
        pass
    return ids, mat


def invalidate(model_name=None):
    """向量矩阵缓存失效（模型切换 / 批量重算后调用）。"""
    _MAT_CACHE.clear()
    names = [model_name] if model_name else []
    if not names and os.path.isdir(CACHE_DIR):
        names = [f[:-5] for f in os.listdir(CACHE_DIR) if f.endswith(".json")]
    removed = 0
    for n in names:
        for p in (_meta_path(n), _npy_path(n)):
            try:
                os.remove(p)
                removed += 1
            except OSError:
                pass
    return removed
