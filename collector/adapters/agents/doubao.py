"""适配器：豆包（Doubao）本地会话。

豆包桌面端把聊天记录放在 Chromium 的 IndexedDB / SQLite 里，官方没有开放
批量导出接口，且 macOS 的应用容器受系统隐私保护。本适配器采取「能读就读、
读不到给明确动作」的策略，支持三类数据：

  1. 豆包导出的 messages JSON（数组项含 role / content / created_at /
     conversation_id），含 JSONL / NDJSON
  2. 本地 SQLite 中的消息表（表名或列名包含 message / chat / conversation）
  3. 导出目录里的 Markdown / TXT（按标题切段入库）

发现顺序（可用 collect.doubao.roots 覆盖）：
  macOS   ~/Library/Group Containers/<team>.com.bot.neotix.doubao/Library
          ~/Library/Containers/com.bot.neotix.doubao/Data/Library
          ~/Library/Application Support/Doubao
  Windows %APPDATA%\\Doubao、%LOCALAPPDATA%\\Doubao
  Linux   ~/.config/doubao、~/.local/share/doubao、~/.doubao

macOS 首次使用若提示「操作不允许」，需要授予 **完全磁盘访问权限**。
注意授权对象是**读取方**（运行 minime 的 Terminal / iTerm / Trae，或实际的
Python 可执行文件），不是豆包：豆包自己能读自己的数据，受限的是 minime 进程。
launchd 定时任务不继承终端授权，定时采集建议改用导出目录方案（无需权限）。
本适配器只读，不修改任何豆包数据。
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

SQLITE_EXT = (".db", ".sqlite", ".sqlite3")
TEXT_EXT = (".json", ".jsonl", ".ndjson", ".md", ".markdown", ".txt")
SKIP_DIRS = {
    "Cache", "Code Cache", "GPUCache", "DawnGraphiteCache", "DawnWebGPUCache",
    "ShaderCache", "GrShaderCache", "GraphiteDawnCache", "Crashpad",
    "blob_storage", "Service Worker", "Shared Dictionary", "component_crx_cache",
    "extensions_crx_cache", "Download Service",
}
# Chromium 自带的库不是聊天记录，直接跳过，避免误判与拖慢
DENY_FILES = {"history", "cookies", "web data", "top sites", "favicons",
              "login data", "shortcuts", "network action predictor",
              "tabloom", "affiliations", "sharedstorage",
              "first_party_sets.db", "heavy_ad_intervention_opt_out.db",
              "gecko2.db", "tea.db"}
DENY_TABLES = {"cookies", "history", "visits", "downloads", "urls", "login",
               "autofill", "keyword_search_terms", "segments", "token_service"}
MESSAGE_HINTS = ("message", "chat", "conversation", "session", "thread", "dialog")
TEXT_COLS = ("content", "text", "message", "msg", "body", "answer", "query", "prompt")
ROLE_COLS = ("role", "sender", "from", "is_user", "author", "msg_type", "message_type")
CONV_COLS = ("conversation_id", "conv_id", "session_id", "chat_id", "thread_id",
             "dialog_id")
TS_COLS = ("created_at", "create_time", "timestamp", "ts", "time", "updated_at")
TITLE_COLS = ("title", "name", "topic", "conversation_title")
MAX_TEXT_BYTES = 8 * 1024 * 1024


def _cfg():
    try:
        sys.path.insert(0, os.path.join(ROOT, "pipeline"))
        import config  # noqa: PLC0415
        return config.load().get("collect", {}).get("doubao") or {}
    except Exception:  # noqa: BLE001 — 配置异常时退回默认，不影响其他源
        return {}


def _auto_roots():
    if sys.platform == "darwin":
        roots = [
            os.path.join(HOME, "Library/Group Containers/96L78H6LMH.com.bot.neotix.doubao/Library"),
            os.path.join(HOME, "Library/Containers/com.bot.neotix.doubao/Data/Library"),
            os.path.join(HOME, "Library/Application Support/Doubao"),
        ]
        # 团队 ID 可能随签名变化，动态补上其他 .com.bot.neotix.doubao 组容器
        group_base = os.path.join(HOME, "Library/Group Containers")
        try:
            for name in os.listdir(group_base):
                if name.endswith(".com.bot.neotix.doubao"):
                    roots.append(os.path.join(group_base, name, "Library"))
        except OSError:
            pass
        return list(dict.fromkeys(roots))
    if os.name == "nt":
        bases = [os.environ.get("APPDATA"), os.environ.get("LOCALAPPDATA")]
        return [os.path.join(b, "Doubao") for b in bases if b]
    return [os.path.join(HOME, ".config/doubao"),
            os.path.join(HOME, ".local/share/doubao"),
            os.path.join(HOME, ".doubao")]


def _content_text(content):
    """把消息内容统一成纯文本（兼容字符串、块数组、对象）。"""
    if isinstance(content, bytes):
        return content.decode("utf-8", "replace")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                val = (block.get("text") or block.get("content")
                       or block.get("input_text") or block.get("output_text") or "")
                if isinstance(val, str):
                    parts.append(val)
        return "\n".join(parts)
    if isinstance(content, dict):
        val = content.get("text") or content.get("content") or ""
        return val if isinstance(val, str) else ""
    return ""


def _role(value):
    s = str(value or "").strip().lower()
    if s in ("user", "human", "me", "我", "客户", "true", "1"):
        return "user"
    if s in ("assistant", "bot", "ai", "豆包", "doubao", "false", "0"):
        return "assistant"
    return "user"


def _looks_message(d):
    if not isinstance(d, dict):
        return False
    has_role = any(k in d for k in ("role", "sender", "is_user", "author"))
    has_text = any(k in d for k in ("content", "text", "message", "msg"))
    return has_role and has_text


def _norm_message(m, idx):
    text = _content_text(m.get("content") or m.get("text") or m.get("message")
                         or m.get("msg") or "")
    text = text.strip()
    if not text:
        return None
    role = m.get("role") or m.get("sender") or m.get("author")
    if role is None and "is_user" in m:
        role = "user" if m.get("is_user") in (True, 1, "1") else "assistant"
    return {
        "id": str(m.get("id") or m.get("message_id") or idx),
        "role": _role(role),
        "text": text[:20000],
        "ts": m.get("created_at") or m.get("timestamp") or m.get("create_time"),
    }


def _conversation(obj, cid, title, out):
    items = None
    for key in ("messages", "items", "data", "records"):
        value = obj.get(key)
        if isinstance(value, list) and any(_looks_message(x) for x in value):
            items = value
            break
    if items is None:
        return False
    messages = [m for m in (_norm_message(x, i) for i, x in enumerate(items)
                            if isinstance(x, dict)) if m]
    if not messages:
        return False
    out.append({
        "conversation_id": str(obj.get("conversation_id") or obj.get("conv_id")
                               or obj.get("session_id") or obj.get("id") or cid),
        "title": str(obj.get("title") or obj.get("name") or obj.get("topic") or title or ""),
        "messages": messages,
    })
    return True


def conversations_from_json(obj, fallback_id=""):
    """从任意 JSON 结构里找出会话；兼容导出数组、会话对象与容器对象。"""
    out = []
    if isinstance(obj, list):
        msgs = [x for x in obj if _looks_message(x)]
        if msgs:
            grouped = {}
            for item in msgs:
                cid = str(item.get("conversation_id") or item.get("conv_id")
                          or item.get("session_id") or fallback_id)
                grouped.setdefault(cid, []).append(item)
            for cid, items in grouped.items():
                _conversation({"messages": items}, cid, "", out)
            return out
        for item in obj[:2000]:
            if isinstance(item, dict):
                out.extend(conversations_from_json(item, fallback_id))
        return out
    if not isinstance(obj, dict):
        return out
    if _conversation(obj, fallback_id, "", out):
        return out
    for key in ("conversations", "chats", "sessions", "threads", "message_list",
                "msg_list", "history", "data", "list"):
        value = obj.get(key)
        if isinstance(value, list):
            for item in value[:2000]:
                if isinstance(item, dict):
                    out.extend(conversations_from_json(item, fallback_id))
            if out:
                return out
    for value in obj.values():
        if isinstance(value, dict) and isinstance(value.get("messages"), list):
            out.extend(conversations_from_json(value, fallback_id))
    return out


def _sqlite_columns(db, table):
    try:
        return list(db.execute(f'PRAGMA table_info("{table}")'))
    except sqlite3.Error:
        return []


def _sqlite_has_messages(path):
    """快速确认一个 SQLite 是否像消息库，避免把 Chromium 自带库当会话。"""
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
    except sqlite3.Error:
        return False
    try:
        tables = [r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' LIMIT 50")]
        for table in tables:
            low = table.lower()
            if low.startswith("sqlite_") or low in DENY_TABLES:
                continue
            info = _sqlite_columns(db, table)
            cols = [r[1] for r in info]
            types = {r[1]: (r[2] or "").lower() for r in info}
            low_cols = {c.lower(): c for c in cols}
            text_col = next((low_cols[c] for c in TEXT_COLS if c in low_cols), None)
            if not text_col:
                text_col = next((c for c in cols
                                 if any(k in c.lower() for k in ("content", "text", "message"))
                                 and ("text" in types.get(c, "") or "char" in types.get(c, "")
                                      or "clob" in types.get(c, ""))), None)
            if not text_col:
                continue
            role_col = next((low_cols[c] for c in ROLE_COLS if c in low_cols), None)
            if role_col or any(h in low for h in MESSAGE_HINTS):
                return True
        return False
    except sqlite3.Error:
        return False
    finally:
        db.close()


def conversations_from_sqlite(path):
    """从 SQLite 里提取消息表；列名/表名双重识别，读不到返回空。"""
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return []
    out = []
    try:
        tables = [r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        titles, message_tables = {}, []
        for table in tables:
            low = table.lower()
            if low.startswith("sqlite_") or low in DENY_TABLES:
                continue
            try:
                info = list(db.execute(f'PRAGMA table_info("{table}")'))
            except sqlite3.Error:
                continue
            cols = [r[1] for r in info]
            types = {r[1]: (r[2] or "").lower() for r in info}
            low_cols = {c.lower(): c for c in cols}
            text_col = next((low_cols[c] for c in TEXT_COLS if c in low_cols), None)
            if not text_col:
                text_col = next((c for c in cols
                                 if any(k in c.lower() for k in ("content", "text", "message"))
                                 and ("text" in types.get(c, "") or "char" in types.get(c, "")
                                      or "clob" in types.get(c, ""))), None)
            title_col = next((low_cols[c] for c in TITLE_COLS if c in low_cols), None)
            id_col = low_cols.get("id") or low_cols.get("conversation_id")
            if not text_col and title_col and id_col:
                try:
                    for cid, title in db.execute(
                            f'SELECT "{id_col}","{title_col}" FROM "{table}" LIMIT 5000'):
                        if title:
                            titles[str(cid)] = str(title)
                except sqlite3.Error:
                    pass
                continue
            if not text_col:
                continue
            role_col = next((low_cols[c] for c in ROLE_COLS if c in low_cols), None)
            if not role_col and not any(h in low for h in MESSAGE_HINTS):
                continue
            message_tables.append((table, text_col, role_col,
                                   next((low_cols[c] for c in CONV_COLS if c in low_cols), None),
                                   next((low_cols[c] for c in TS_COLS if c in low_cols), None),
                                   low_cols.get("id") or low_cols.get("message_id")))
        for table, text_col, role_col, conv_col, ts_col, id_col in message_tables:
            try:
                rows = db.execute(f'SELECT * FROM "{table}" LIMIT 20000').fetchall()
            except sqlite3.Error:
                continue
            names = [d[0] for d in db.execute(f'SELECT * FROM "{table}" LIMIT 0').description]
            grouped = {}
            for row in rows:
                item = dict(zip(names, row))
                text = _content_text(item.get(text_col)).strip()
                if not text:
                    continue
                cid = str(item.get(conv_col)) if conv_col else f"table:{table}"
                grouped.setdefault(cid, []).append({
                    "id": str(item.get(id_col) or len(grouped.get(cid, []))),
                    "role": _role(item.get(role_col)) if role_col else "user",
                    "text": text[:20000],
                    "ts": item.get(ts_col) if ts_col else None,
                })
            for cid, messages in grouped.items():
                out.append({"conversation_id": cid,
                            "title": titles.get(cid, ""),
                            "messages": messages})
    except sqlite3.Error:
        return out
    finally:
        db.close()
    return out


@register
class DoubaoAdapter(SourceAdapter):
    name = "doubao"
    kind = "doubao_conversation"
    DEFAULT_INTERVAL_S = 3600
    priority = 45
    description = "豆包会话（导出 JSON/JSONL、本地 SQLite、导出 Markdown/TXT）"
    label = "豆包会话"
    fix_hint = ("macOS 无权读取时：给运行 minime 的终端/Python 授予完全磁盘访问权限"
                "（不是给豆包授权）；或改用豆包导出目录（设置页 collect.doubao.roots，"
                "无需任何权限）")

    def __init__(self, conn):
        super().__init__(conn)
        self.cfg = _cfg()
        self._explicit_roots = []

    def roots(self):
        explicit = [os.path.abspath(os.path.expanduser(str(r)))
                    for r in (self.cfg.get("roots") or []) if str(r).strip()]
        auto = _auto_roots()
        return list(dict.fromkeys(explicit + auto)), explicit

    # ---- 扫描
    def _scan(self, roots, quick=False):
        """返回 (候选文件, indexeddb 目录数, 权限错误列表)。"""
        candidates, denied = [], []
        indexeddb = 0
        budget = 4000 if quick else 120000
        seen = 0

        def onerror(err):
            denied.append(f"{getattr(err, 'filename', '')}（{err.strerror or err}）")

        for root in roots:
            if not os.path.isdir(root):
                continue
            for dirpath, dirnames, filenames in os.walk(root, onerror=onerror):
                seen += 1
                if seen > budget:
                    break
                rel_depth = os.path.relpath(dirpath, root).count(os.sep)
                if rel_depth >= 8:
                    dirnames[:] = []
                    continue
                keep = []
                for d in dirnames:
                    if d.endswith(".indexeddb.leveldb"):
                        indexeddb += 1
                        continue
                    if d in SKIP_DIRS or d.startswith("."):
                        continue
                    keep.append(d)
                dirnames[:] = keep
                for fn in filenames:
                    low = fn.lower()
                    full = os.path.join(dirpath, fn)
                    is_export = self._explicit_hit(full)
                    source = (f"{os.path.basename(root)}/"
                              f"{os.path.relpath(full, root).replace(os.sep, '/')}")
                    if low.endswith(SQLITE_EXT) and low not in DENY_FILES:
                        if _sqlite_has_messages(full):
                            candidates.append(("sqlite", full, source))
                    elif low.endswith(TEXT_EXT):
                        rel = os.path.relpath(full, root).lower()
                        if is_export or any(k in rel for k in (
                                "export", "backup", "chat", "conversation", "message")):
                            candidates.append(("text", full, source))
                if quick and candidates:
                    return candidates, indexeddb, denied
        return candidates, indexeddb, denied

    def _explicit_hit(self, full):
        return any(full.startswith(r + os.sep) or full == r
                   for r in self._explicit_roots)

    def probe(self):
        roots, explicit = self.roots()
        self._explicit_roots = explicit
        existing = [r for r in roots if os.path.isdir(r)]
        readable, denied = [], []
        for root in existing:
            try:
                os.listdir(root)
                readable.append(root)
            except OSError as e:
                denied.append(f"{root}（{e.strerror or e}）")
        if explicit:
            missing = [r for r in explicit if not os.path.isdir(r)]
            if missing and not readable:
                return False, f"配置的豆包目录不存在：{', '.join(missing[:3])}"
        if not existing:
            return False, ("未发现豆包数据目录；可把豆包导出的 JSON/Markdown 放到 "
                           "~/Documents/doubao-export，并在设置页 collect.doubao.roots 指定")
        if not readable:
            return False, "无权读取豆包数据目录：" + "；".join(denied[:2])
        candidates, indexeddb, walk_denied = self._scan(readable, quick=True)
        if candidates:
            return True, f"{len(candidates)} 个候选文件（含导出/本地库）"
        if walk_denied or denied:
            denied = (walk_denied or denied)[:2]
            return False, ("部分目录无权读取：" + "；".join(denied)
                           + "（给运行 minime 的终端/Python 授予完全磁盘访问权限，"
                             "不是给豆包授权；也可改用导出目录）")
        if indexeddb:
            return False, ("发现豆包 IndexedDB 缓存，但本地格式无法直接读取；"
                           "请在豆包「设置 → 数据导出」导出后放入 collect.doubao.roots，"
                           "或把 DevTools 导出的 messages JSON 放进该目录")
        return False, ("豆包数据目录存在但未发现可解析的会话；"
                       "把导出的 JSON/Markdown 放到 collect.doubao.roots 指定目录")

    # ---- 采集
    def _add_conversation(self, source, conv):
        payload = {
            "source": source,
            "conversation_id": conv.get("conversation_id") or source,
            "title": conv.get("title") or "",
            "messages": conv.get("messages") or [],
        }
        key = f"{source}#{payload['conversation_id']}"
        put_raw(self.conn, self.kind, key,
                json.dumps(payload, ensure_ascii=False).encode())
        return len(payload["messages"])

    def _from_json_file(self, path, source):
        try:
            size = os.path.getsize(path)
        except OSError:
            return 0, 0
        if size > MAX_TEXT_BYTES:
            return 0, 0
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            return 0, 0
        convs = []
        if path.lower().endswith((".jsonl", ".ndjson")):
            records = []
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue
            convs = conversations_from_json(records, os.path.splitext(
                os.path.basename(path))[0])
        else:
            try:
                convs = conversations_from_json(json.loads(text), os.path.splitext(
                    os.path.basename(path))[0])
            except ValueError:
                return 0, 0
        n = sum(self._add_conversation(source, c) for c in convs)
        return len(convs), n

    def _from_text_file(self, path, source):
        try:
            if os.path.getsize(path) > MAX_TEXT_BYTES:
                return 0
            body = open(path, encoding="utf-8", errors="replace").read().strip()
        except OSError:
            return 0
        if len(body) < 20:
            return 0
        return self._add_conversation(source, {
            "conversation_id": os.path.basename(path),
            "title": os.path.splitext(os.path.basename(path))[0],
            "messages": [{"id": "doc", "role": "document", "text": body, "ts": None}],
        })

    def collect(self):
        roots, explicit = self.roots()
        self._explicit_roots = explicit
        candidates, indexeddb, denied = self._scan(roots)
        conversations = messages = json_files = sqlite_dbs = 0
        for kind, full, source in candidates:
            try:
                if kind == "sqlite":
                    convs = conversations_from_sqlite(full)
                    sqlite_dbs += 1
                elif full.lower().endswith((".md", ".markdown", ".txt")):
                    n = self._from_text_file(full, source)
                    conversations += 1 if n else 0
                    messages += n
                    continue
                else:
                    json_files += 1
                    c, n = self._from_json_file(full, source)
                    conversations += c
                    messages += n
                    continue
            except Exception:  # noqa: BLE001 — 单个库损坏不影响其他文件
                continue
            for conv in convs:
                conversations += 1
                messages += self._add_conversation(source, conv)
        return {"conversations": conversations, "messages": messages,
                "files_scanned": len(candidates), "json_files": json_files,
                "sqlite_dbs": sqlite_dbs, "indexeddb_dirs": indexeddb,
                "permission_denied": len(denied)}

    def stats(self):
        n, size = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size),0) FROM raw_object WHERE kind=?",
            (self.kind,)).fetchone()
        return {"objects": n, "bytes": size}

    def verify(self):
        n = self.conn.execute("SELECT COUNT(*) FROM raw_object WHERE kind=?",
                              (self.kind,)).fetchone()[0]
        if n:
            return True, f"{n} 个会话归档"
        ok, detail = self.probe()
        if ok:
            return False, "数据可读但暂无可入库的会话；豆包有新对话后再采集"
        return False, detail
