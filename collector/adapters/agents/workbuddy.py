"""适配器：WorkBuddy / WorkBuddy AI 本地会话与记忆。

数据形态（默认同时扫描，均可用 collect.workbuddy.roots 覆盖）：
  ~/.workbuddy-ai / ~/.workbuddy
    projects/<项目目录>/<session>.jsonl          逐轮消息（message / ai-title / 文件快照）
    memory/*_memory.md                           助手长期记忆
    user-*-personal/{USER,IDENTITY,SOUL}.md      用户画像与助手身份
    workbuddy.db                                 sessions 元数据（标题/工作区/模型/时间）

采集粒度：会话级 + 记忆文件级；原始内容寻址落盘，重复执行自动去重。
消息里的 <system-reminder> 注入上下文会被剥离，只保留用户真正说过的内容。
"""
import json
import os
import re
import sqlite3
import sys

# collector/ 是 base 框架所在层；上溯三级，直接运行本文件也能调试
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
from base import HOME, ROOT, SourceAdapter, put_raw, register  # noqa: E402

AUTO_ROOTS = (".workbuddy-ai", ".workbuddy")
IDENTITY_FILES = ("USER.md", "IDENTITY.md", "SOUL.md")
SYSTEM_BLOCK = re.compile(r"<system-reminder\b[^>]*>.*?</system-reminder>", re.S)
USER_QUERY = re.compile(r"<user_query>(.*?)</user_query>", re.S)
MAX_MESSAGE_CHARS = 20000
MAX_SESSION_CHARS = 2_000_000


def _cfg():
    try:
        sys.path.insert(0, os.path.join(ROOT, "pipeline"))
        import config  # noqa: PLC0415
        return config.load().get("collect", {}).get("workbuddy") or {}
    except Exception:  # noqa: BLE001 — 配置异常时退回默认，不影响其他源
        return {}


def clean_user_text(text):
    """去掉系统注入的上下文，只留用户输入。

    WorkBuddy 会把工作区信息、技能/记忆提醒塞进 <system-reminder>，真正的
    用户输入在 <user_query> 里；没有该标签时按普通消息处理。
    """
    text = SYSTEM_BLOCK.sub("", text or "")
    queries = [q.strip() for q in USER_QUERY.findall(text) if q.strip()]
    if queries:
        return "\n".join(queries)
    return text.strip()


def _text_of(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        if block.get("type") in ("input_text", "output_text", "text"):
            parts.append(str(block.get("text") or ""))
    return "\n".join(p for p in parts if p)


def parse_transcript(path):
    """解析一个 <session>.jsonl，返回会话 payload（不含数据库元数据）。"""
    session_id = os.path.splitext(os.path.basename(path))[0]
    title = cwd = ""
    messages = []
    files = set()
    total = 0
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return None
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            typ = obj.get("type")
            if typ == "session-meta":
                session_id = obj.get("sessionId") or session_id
                continue
            if typ == "ai-title":
                title = str(obj.get("aiTitle") or title)
                cwd = obj.get("cwd") or cwd
                continue
            if typ == "file-history-snapshot":
                cwd = obj.get("cwd") or cwd
                tracked = ((obj.get("snapshot") or {}).get("trackedFileBackups") or {})
                if isinstance(tracked, dict):
                    files.update(tracked)
                continue
            if typ != "message":
                continue
            role = obj.get("role")
            if role not in ("user", "assistant"):
                continue
            text = _text_of(obj.get("content"))
            text = clean_user_text(text) if role == "user" else text.strip()
            if not text:
                continue
            text = text[:MAX_MESSAGE_CHARS]
            if total + len(text) > MAX_SESSION_CHARS:
                break
            total += len(text)
            messages.append({
                "id": str(obj.get("id") or ""),
                "role": role,
                "text": text,
                "ts": obj.get("timestamp"),
            })
    return {"session_id": session_id, "title": title, "cwd": cwd,
            "messages": messages, "files": sorted(files)}


def db_sessions(root):
    """读 workbuddy.db 的会话元数据；数据库不存在/被占用时返回空表。"""
    path = os.path.join(root, "workbuddy.db")
    if not os.path.exists(path):
        return {}
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return {}
    out = {}
    try:
        cols = {r[1] for r in db.execute("PRAGMA table_info(sessions)")}
        if not cols:
            return {}
        want = [c for c in ("id", "title", "custom_title", "cwd", "model",
                            "created_at", "updated_at", "deleted_at") if c in cols]
        rows = db.execute(f"SELECT {','.join(want)} FROM sessions").fetchall()
        for row in rows:
            item = dict(zip(want, row))
            sid = item.get("id")
            if not sid:
                continue
            out[str(sid)] = item
    except sqlite3.Error:
        return {}
    finally:
        db.close()
    return out


@register
class WorkBuddyAdapter(SourceAdapter):
    name = "workbuddy"
    kind = "workbuddy_session"
    MEMORY_KIND = "workbuddy_memory"
    DEFAULT_INTERVAL_S = 1800
    priority = 42
    description = "WorkBuddy / WorkBuddy AI 本地会话与记忆（JSONL + 记忆文件）"
    label = "WorkBuddy 会话"
    fix_hint = ("先使用一次 WorkBuddy 或 WorkBuddy AI 产生本地会话"
                "（~/.workbuddy-ai / ~/.workbuddy）；仍不可用可在设置页关闭此源")

    def __init__(self, conn):
        super().__init__(conn)
        self.cfg = _cfg()

    # ---- 路径发现
    def _product(self, root):
        name = os.path.basename(root.rstrip("/")) or "workbuddy"
        return "workbuddy-ai" if name.endswith("-ai") else "workbuddy"

    def roots(self):
        explicit = [os.path.abspath(os.path.expanduser(str(r)))
                    for r in (self.cfg.get("roots") or []) if str(r).strip()]
        auto = [os.path.join(HOME, d) for d in AUTO_ROOTS
                if os.path.isdir(os.path.join(HOME, d))]
        return list(dict.fromkeys(explicit + auto))

    def probe(self):
        roots = self.roots()
        if not roots:
            return False, (f"未发现 WorkBuddy 数据目录（{' / '.join(AUTO_ROOTS)}）；"
                           "用设置页 collect.workbuddy.roots 指定自定义目录")
        bad = []
        for root in roots:
            try:
                os.listdir(root)
            except OSError as e:
                bad.append(f"{root}（{e.strerror or e}）")
        if len(bad) == len(roots):
            return False, "无法读取数据目录：" + "；".join(bad)
        return True, f"{len(roots)} 个数据目录：{', '.join(os.path.basename(r) for r in roots)}"

    # ---- 采集
    def _collect_sessions(self, root, product):
        meta = db_sessions(root)
        seen = set()
        sessions = messages = 0
        projects = os.path.join(root, "projects")
        if os.path.isdir(projects):
            for dirpath, _dirs, files in os.walk(projects):
                for fn in files:
                    if not fn.endswith(".jsonl") or fn.endswith(".file-rollback.ndjson"):
                        continue
                    full = os.path.join(dirpath, fn)
                    parsed = parse_transcript(full)
                    if not parsed:
                        continue
                    sid = parsed["session_id"]
                    info = meta.get(sid) or {}
                    if info.get("deleted_at"):
                        continue
                    if sid in seen:
                        continue
                    seen.add(sid)
                    payload = {
                        "product": product,
                        "session_id": sid,
                        "title": info.get("custom_title") or info.get("title")
                                 or parsed["title"],
                        "cwd": info.get("cwd") or parsed["cwd"],
                        "model": info.get("model") or "",
                        "created_at": info.get("created_at"),
                        "updated_at": info.get("updated_at"),
                        "messages": parsed["messages"],
                        "files": parsed["files"],
                    }
                    rel = os.path.relpath(full, root)
                    put_raw(self.conn, self.kind, f"{product}/{rel}",
                            json.dumps(payload, ensure_ascii=False).encode())
                    sessions += 1
                    messages += len(parsed["messages"])
        # 只有元数据、没有转录的会话也保留标题，便于回溯
        for sid, info in meta.items():
            if sid in seen or info.get("deleted_at"):
                continue
            seen.add(sid)
            payload = {
                "product": product,
                "session_id": sid,
                "title": info.get("custom_title") or info.get("title") or "",
                "cwd": info.get("cwd") or "",
                "model": info.get("model") or "",
                "created_at": info.get("created_at"),
                "updated_at": info.get("updated_at"),
                "messages": [],
                "files": [],
            }
            put_raw(self.conn, self.kind, f"{product}/sessions/{sid}",
                    json.dumps(payload, ensure_ascii=False).encode())
            sessions += 1
        return sessions, messages

    def _collect_memory(self, root, product):
        mem_dir = os.path.join(root, "memory")
        memory_files = 0
        if os.path.isdir(mem_dir):
            for fn in sorted(os.listdir(mem_dir)):
                if not fn.endswith(".md") or fn.endswith(".bak"):
                    continue
                full = os.path.join(mem_dir, fn)
                try:
                    body = open(full, encoding="utf-8", errors="replace").read()
                except OSError:
                    continue
                if not body.strip():
                    continue
                payload = {"product": product, "kind": "user_memory",
                           "path": fn, "body": body}
                put_raw(self.conn, self.MEMORY_KIND, f"{product}/memory/{fn}",
                        json.dumps(payload, ensure_ascii=False).encode())
                memory_files += 1
        identity_files = 0
        if self.cfg.get("include_identity", True):
            names = sorted(os.listdir(root)) if os.path.isdir(root) else []
            for name in names:
                if not name.startswith("user-") or not name.endswith("-personal"):
                    continue
                user_dir = os.path.join(root, name)
                if not os.path.isdir(user_dir):
                    continue
                for fn in IDENTITY_FILES:
                    full = os.path.join(user_dir, fn)
                    if not os.path.isfile(full):
                        continue
                    try:
                        body = open(full, encoding="utf-8", errors="replace").read()
                    except OSError:
                        continue
                    if not body.strip():
                        continue
                    payload = {"product": product, "kind": "identity",
                               "path": f"{name}/{fn}", "body": body}
                    put_raw(self.conn, self.MEMORY_KIND,
                            f"{product}/identity/{name}/{fn}",
                            json.dumps(payload, ensure_ascii=False).encode())
                    identity_files += 1
        return memory_files, identity_files

    def collect(self):
        sessions = messages = memory_files = identity_files = 0
        roots = self.roots()
        for root in roots:
            product = self._product(root)
            try:
                s, m = self._collect_sessions(root, product)
                sessions += s
                messages += m
                mf, idf = self._collect_memory(root, product)
                memory_files += mf
                identity_files += idf
            except OSError:
                # 单个目录不可读（权限/被占用）不影响其他目录
                continue
        return {"sessions": sessions, "messages": messages,
                "memory_files": memory_files, "identity_files": identity_files,
                "roots": len(roots)}

    def stats(self):
        n, size = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size),0) FROM raw_object WHERE kind IN (?,?)",
            (self.kind, self.MEMORY_KIND)).fetchone()
        return {"objects": n, "bytes": size}

    def verify(self):
        n = self.conn.execute(
            "SELECT COUNT(*) FROM raw_object WHERE kind IN (?,?)",
            (self.kind, self.MEMORY_KIND)).fetchone()[0]
        return n > 0, f"{n} 份会话/记忆归档"
