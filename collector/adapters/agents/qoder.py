"""适配器：Qoder 本地会话。

数据形态（macOS 默认）：
  ~/Library/Application Support/com.qoder.app.stable/main.sqlite
    chat_sessions          会话（title / cwd / model / 时间 / deleted_at）
    chat_session_messages  消息（payload_json + role / sequence / turn_id）
    turn_file_change_files  每轮改动文件（按 change_set 关联会话）

采集粒度：会话级；消息正文从 payload_json 里提取，只保留纯文本。
Qoder 运行中也可以只读打开（WAL 会一并读取），不会修改原始数据。
"""
import json
import os
import sqlite3
import sys

# collector/ 是 base 框架所在层；上溯三级，直接运行本文件也能调试
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
from base import HOME, ROOT, SourceAdapter, put_raw, register  # noqa: E402

APP_DIRS = ("com.qoder.app.stable", "com.qoder.app", "Qoder")
SKIP_DIRS = {"Cache", "Code Cache", "GPUCache", "DawnGraphiteCache",
             "DawnWebGPUCache", "Crashpad", "blob_storage", "Service Worker",
             "Code Cache", "Crashpad", "logs", "node_modules", ".git"}
MAX_MESSAGE_CHARS = 20000
MAX_SESSION_CHARS = 2_000_000


def _cfg():
    try:
        sys.path.insert(0, os.path.join(ROOT, "pipeline"))
        import config  # noqa: PLC0415
        return config.load().get("collect", {}).get("qoder") or {}
    except Exception:  # noqa: BLE001 — 配置异常时退回默认，不影响其他源
        return {}


def _auto_roots():
    if sys.platform == "darwin":
        return [os.path.join(HOME, "Library/Application Support", d) for d in APP_DIRS] + [
            os.path.join(HOME, ".qoder")]
    if os.name == "nt":
        bases = [os.environ.get("APPDATA"), os.environ.get("LOCALAPPDATA")]
        return [os.path.join(b, d) for b in bases if b for d in APP_DIRS] + [
            os.path.join(HOME, ".qoder")]
    return [os.path.join(HOME, ".config", d) for d in APP_DIRS] + [
        os.path.join(HOME, ".qoder")]


def _content_text(content):
    """把 payload 里的文本块拼成纯文本；工具调用/结果一律跳过。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                typ = str(block.get("type") or "")
                if typ in ("tool_use", "tool_result", "thinking", "reasoning"):
                    continue
                val = (block.get("text") or block.get("content")
                       or block.get("output_text") or block.get("input_text") or "")
                if isinstance(val, str):
                    parts.append(val)
        return "\n".join(p for p in parts if p)
    if isinstance(content, dict):
        val = content.get("text") or content.get("content") or ""
        return val if isinstance(val, str) else ""
    return ""


def _role(value):
    s = str(value or "").strip().lower()
    if s in ("user", "human", "me", "我"):
        return "user"
    if s in ("assistant", "bot", "ai", "qoder"):
        return "assistant"
    return "user"


def parse_payload(payload_json, column_role=""):
    """从一条 chat_session_messages.payload_json 里取 (role, text)。"""
    try:
        payload = json.loads(payload_json or "{}")
    except ValueError:
        return "", ""
    if isinstance(payload, str):
        return _role(column_role), payload.strip()
    if isinstance(payload, list):
        return _role(column_role), _content_text(payload)
    if not isinstance(payload, dict):
        return "", ""
    role = payload.get("role")
    msg = payload.get("message")
    if not role and isinstance(msg, dict):
        role = msg.get("role")
    content = payload.get("content")
    if content is None and isinstance(msg, dict):
        content = msg.get("content")
    if content is None and isinstance(msg, str):
        content = msg
    if content is None:
        content = payload.get("parts") or payload.get("blocks") or payload.get("text")
    text = _content_text(content).strip()
    return _role(role or column_role), text


def _connect(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)


def _columns(db, table):
    try:
        return {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}
    except sqlite3.Error:
        return set()


@register
class QoderAdapter(SourceAdapter):
    name = "qoder"
    kind = "qoder_session"
    DEFAULT_INTERVAL_S = 1800
    priority = 44
    description = "Qoder 本地会话（main.sqlite 的会话与消息）"
    label = "Qoder 会话"
    fix_hint = ("先在 Qoder 里完成一次对话（数据在 "
                "~/Library/Application Support/com.qoder.app.stable/main.sqlite）；"
                "仍不可用可在设置页 collect.qoder.roots 指定自定义目录")

    def __init__(self, conn):
        super().__init__(conn)
        self.cfg = _cfg()

    def roots(self):
        explicit = [os.path.abspath(os.path.expanduser(str(r)))
                    for r in (self.cfg.get("roots") or []) if str(r).strip()]
        auto = [r for r in _auto_roots() if os.path.isdir(r)]
        return list(dict.fromkeys(explicit + auto))

    def _databases(self):
        out = []
        for root in self.roots():
            direct = os.path.join(root, "main.sqlite")
            if os.path.isfile(direct):
                out.append(direct)
                continue
            for dirpath, dirnames, files in os.walk(root):
                rel = os.path.relpath(dirpath, root)
                if rel != "." and rel.count(os.sep) >= 3:
                    dirnames[:] = []
                    continue
                dirnames[:] = [d for d in dirnames
                               if d not in SKIP_DIRS and not d.startswith(".")]
                if "main.sqlite" in files:
                    out.append(os.path.join(dirpath, "main.sqlite"))
        return list(dict.fromkeys(out))

    def _session_count(self, path):
        try:
            db = _connect(path)
        except sqlite3.Error:
            return None, "无法打开 main.sqlite"
        try:
            cols = _columns(db, "chat_sessions")
            if not cols:
                return None, "没有 chat_sessions 表"
            sql = "SELECT COUNT(*) FROM chat_sessions"
            if "deleted_at" in cols:
                sql += " WHERE deleted_at IS NULL"
            return db.execute(sql).fetchone()[0], ""
        except sqlite3.Error as e:
            return None, f"{type(e).__name__}: {e}"
        finally:
            db.close()

    def probe(self):
        roots = self.roots()
        if not roots:
            return False, ("未发现 Qoder 数据目录（com.qoder.app.stable）；"
                           "可在设置页 collect.qoder.roots 指定")
        dbs = self._databases()
        if not dbs:
            return False, "Qoder 目录存在但未找到 main.sqlite；先启动 Qoder 并完成一次对话"
        errors = []
        for path in dbs:
            n, err = self._session_count(path)
            if err:
                errors.append(f"{path}（{err}）")
                continue
            if n:
                return True, f"{n} 个会话（{os.path.basename(os.path.dirname(path))}）"
        if errors:
            return False, "无法读取 Qoder 会话库：" + "；".join(errors[:2])
        return False, "Qoder 尚无本地会话；先进行一次对话再采集"

    # ---- 采集
    def _sessions(self, db):
        cols = _columns(db, "chat_sessions")
        if not cols:
            return []
        want = [c for c in ("session_id", "title", "cwd", "model", "product_mode",
                            "created_at", "updated_at", "deleted_at") if c in cols]
        sql = f"SELECT {','.join(want)} FROM chat_sessions"
        if "deleted_at" in cols:
            sql += " WHERE deleted_at IS NULL"
        rows = db.execute(sql).fetchall()
        return [dict(zip(want, row)) for row in rows if row[want.index("session_id")]]

    def _messages(self, db, session_id):
        cols = _columns(db, "chat_session_messages")
        if not cols or "payload_json" not in cols:
            return []
        want = [c for c in ("message_id", "turn_id", "sequence", "payload_json",
                            "status", "source", "created_at", "role") if c in cols]
        sql = (f"SELECT {','.join(want)} FROM chat_session_messages"
               " WHERE session_id=? ORDER BY sequence")
        messages, total, seen = [], 0, set()
        for row in db.execute(sql, (session_id,)):
            item = dict(zip(want, row))
            mid = str(item.get("message_id") or len(messages))
            if mid in seen:
                continue
            seen.add(mid)
            role, text = parse_payload(item.get("payload_json"), item.get("role"))
            if not text:
                continue
            text = text[:MAX_MESSAGE_CHARS]
            if total + len(text) > MAX_SESSION_CHARS:
                break
            total += len(text)
            messages.append({"id": mid, "role": role, "text": text,
                             "ts": item.get("created_at")})
        return messages

    def _files(self, db, session_id):
        cols = _columns(db, "turn_file_change_files")
        if not cols:
            return []
        try:
            rows = db.execute("""
                SELECT DISTINCT f.path FROM turn_file_change_files f
                JOIN turn_file_change_sets s ON s.change_set_id=f.change_set_id
                WHERE s.session_id=? ORDER BY f.path LIMIT 500""",
                (session_id,)).fetchall()
        except sqlite3.Error:
            return []
        return [r[0] for r in rows if r[0]]

    def collect(self):
        sessions = messages = files = 0
        for path in self._databases():
            try:
                db = _connect(path)
            except sqlite3.Error:
                continue
            try:
                for info in self._sessions(db):
                    sid = str(info["session_id"])
                    msgs = self._messages(db, sid)
                    changed = self._files(db, sid)
                    payload = {
                        "product": "qoder",
                        "session_id": sid,
                        "title": info.get("title") or "",
                        "cwd": info.get("cwd") or "",
                        "model": info.get("model") or "",
                        "created_at": info.get("created_at"),
                        "updated_at": info.get("updated_at"),
                        "messages": msgs,
                        "files": changed,
                    }
                    put_raw(self.conn, self.kind, f"qoder/{sid}",
                            json.dumps(payload, ensure_ascii=False).encode())
                    sessions += 1
                    messages += len(msgs)
                    files += len(changed)
            except sqlite3.Error:
                continue
            finally:
                db.close()
        return {"sessions": sessions, "messages": messages, "files": files}

    def verify(self):
        n = self.conn.execute("SELECT COUNT(*) FROM raw_object WHERE kind=?",
                              (self.kind,)).fetchone()[0]
        if n:
            return True, f"{n} 个会话归档"
        ok, detail = self.probe()
        return (False, "会话可读但暂无可归档内容；Qoder 有新对话后再采集"
                if ok else detail)
