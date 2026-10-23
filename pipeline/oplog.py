"""运行日志：JSONL 追加持久化 + 敏感信息脱敏 + 分档保留。

设计要点（照着 workbuddy-connect 的 requestlog 思路）：
  - 每行一条 JSON，重启不丢；坏行跳过，不影响其余记录
  - 列表接口不带 body，详情接口才返回完整内容（几千条下依然轻快）
  - 请求体/响应体截断到 4KB，标注原长
  - 错误 7 天 / 普通 12 小时，硬上限 5000 条
  - 鉴权头与密钥字段统一 ***

落盘：store/logs/minime.jsonl
"""
import json
import os
import re
import threading
import time
import uuid

import paths

LOG_DIR = paths.LOGS
LOG_PATH = os.path.join(LOG_DIR, "minime.jsonl")
MAX_ENTRIES = 5000
BODY_LIMIT = 4096
KEEP_ERROR_S = 7 * 86400
KEEP_INFO_S = 12 * 3600

SECRET_KEYS = re.compile(
    r"(authorization|cookie|set-cookie|api[_-]?key|token|secret|password|passwd)",
    re.I)
_SECRET_HEADERS = {"authorization", "proxy-authorization", "cookie", "set-cookie",
                   "x-api-key", "x-auth-token"}

_lock = threading.Lock()


def redact_headers(headers):
    if not headers:
        return {}
    out = {}
    for k, v in dict(headers).items():
        out[k] = "***" if k.lower() in _SECRET_HEADERS else v
    return out


def redact_obj(obj, depth=0):
    """递归脱敏；对密钥类字段直接替换为 ***。"""
    if depth > 4:
        return "…"
    if isinstance(obj, dict):
        return {k: ("***" if SECRET_KEYS.search(str(k)) else redact_obj(v, depth + 1))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(x, depth + 1) for x in obj[:50]]
    if isinstance(obj, str):
        return _scalar(obj, secret=True)
    return obj


def _scalar(v, secret=False):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    # 去掉可能夹带的本地绝对路径（隐私）
    s = re.sub(r"/Users/[^/\s\"']+", "~", s)
    if len(s) > BODY_LIMIT:
        s = s[:BODY_LIMIT] + f"…（已截断，原长 {len(s)}）"
    return "***" if secret else s


def ensure_dir():
    os.makedirs(LOG_DIR, exist_ok=True)


def write(level="info", event="", **fields):
    """追加一条日志。fields 中 secret 前缀的键会被脱敏。"""
    ensure_dir()
    rec = {"id": uuid.uuid4().hex[:12], "ts": time.time(), "level": level,
           "event": event}
    for k, v in fields.items():
        if k.startswith("_"):
            continue
        if SECRET_KEYS.search(k):
            rec[k] = "***"
        elif k == "headers":
            rec[k] = redact_headers(v)
        elif isinstance(v, (dict, list)):
            rec[k] = redact_obj(v)
        elif isinstance(v, str):
            rec[k] = _scalar(v)
        else:
            rec[k] = v
    line = json.dumps(rec, ensure_ascii=False, default=str)
    with _lock:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def _iter():
    if not os.path.exists(LOG_PATH):
        return
    with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                yield json.loads(ln)
            except ValueError:
                continue


def list_logs(level="all", limit=200, keyword=""):
    """列表：不带 body/detail，按时间倒序。"""
    limit = max(1, min(int(limit or 200), 500))
    out = []
    kw = (keyword or "").lower()
    for r in _iter():
        if level == "error" and r.get("level") != "error":
            continue
        if level == "warn" and r.get("level") != "warn":
            continue
        if level == "info" and r.get("level") != "info":
            continue
        if kw and kw not in json.dumps(r, ensure_ascii=False).lower():
            continue
        out.append({k: v for k, v in r.items() if k != "body"})
    out.reverse()
    return out[:limit]


def detail(entry_id):
    for r in _iter():
        if r.get("id") == entry_id:
            return r
    return None


def stats():
    n = e = 0
    for r in _iter():
        n += 1
        if r.get("level") == "error":
            e += 1
    return {"entries": n, "errors": e, "path": LOG_PATH, "max": MAX_ENTRIES}


def prune():
    """按分档保留期 + 硬上限裁剪；无需删除时不写回。"""
    now = time.time()
    rows = list(_iter())
    keep = []
    for r in rows:
        age = now - float(r.get("ts") or 0)
        ttl = KEEP_ERROR_S if r.get("level") == "error" else KEEP_INFO_S
        if age <= ttl:
            keep.append(r)
    if len(keep) > MAX_ENTRIES:
        keep = keep[-MAX_ENTRIES:]
    if len(keep) == len(rows):
        return {"pruned": 0, "kept": len(keep)}
    ensure_dir()
    tmp = LOG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in keep:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    os.replace(tmp, LOG_PATH)
    return {"pruned": len(rows) - len(keep), "kept": len(keep)}
