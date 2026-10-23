"""适配器：Trae 工作区索引。

数据形态：
  User/workspaceStorage/<hash>/state.vscdb   会话 id 线索（ChatStore.turnsHeight）
  User/workspaceStorage/<hash>/workspace.json 工作区真实路径
  User/History/<hash>/entries.json            编辑器文件历史
采集粒度：工作区级（归档 workspace -> [sid] 映射）
"""
import json
import os
import re
import sqlite3
import sys

# collector/ 是 base 框架所在层；上溯三级，直接运行本文件也能调试
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
from base import HISTORY, WS_STORAGE, SourceAdapter, put_raw, register  # noqa: E402

SID_RE = re.compile(r"\b[0-9a-f]{24}\b")


def session_ids_from_vscdb(p):
    """从一个 state.vscdb 里抠出该工作区出现过的全部 session id。"""
    out = {}
    try:
        db = sqlite3.connect(f"file:{p}?immutable=1", uri=True)
        rows = db.execute("select key,value from ItemTable").fetchall()
        db.close()
    except sqlite3.Error:
        return out
    for k, v in rows:
        if not isinstance(v, (str, bytes)):
            continue
        try:
            data = json.loads(v)
        except (TypeError, ValueError):
            for sid in SID_RE.findall(str(k)):
                out.setdefault(sid, 0)
            continue
        if isinstance(data, dict) and isinstance(data.get("state"), dict):
            for key in data["state"].get("turnsHeight") or {}:
                sid = key.split("-")[0]
                if len(sid) == 24:
                    out[sid] = out.get(sid, 0) + 1
            continue
        if isinstance(data, dict):
            for key in data:
                for sid in SID_RE.findall(str(key)):
                    out.setdefault(sid, 0)
        else:
            for sid in SID_RE.findall(str(v)[:100000]):
                out.setdefault(sid, 0)
    return out


@register
class WorkspaceAdapter(SourceAdapter):
    name = "workspace_index"
    kind = "workspace_index"
    DEFAULT_INTERVAL_S = 900
    priority = 20
    description = "全部工作区的会话索引（workspaceStorage/state.vscdb）"
    label = "工作区索引"
    fix_hint = "确认 Trae 使用过至少一个工作区"

    def probe(self):
        if not os.path.isdir(WS_STORAGE):
            return False, f"目录不存在：{WS_STORAGE}"
        n = len([d for d in os.listdir(WS_STORAGE)
                 if os.path.exists(os.path.join(WS_STORAGE, d, "state.vscdb"))])
        return (True, f"{n} 个工作区库") if n else (False, "没有 state.vscdb")

    def collect(self):
        out = {}
        for d in sorted(os.listdir(WS_STORAGE)):
            dbp = os.path.join(WS_STORAGE, d, "state.vscdb")
            if not os.path.exists(dbp):
                continue
            ids = session_ids_from_vscdb(dbp)
            if not ids:
                continue
            out[d] = {"sessions": sorted(ids), "hints": ids}
        put_raw(self.conn, self.kind, "workspaceStorage",
                json.dumps(out, ensure_ascii=False).encode())
        return {"workspaces": len(out),
                "workspace_sessions": sum(len(v["sessions"]) for v in out.values())}

    def verify(self):
        n = self.conn.execute("SELECT COUNT(*) FROM raw_object WHERE kind=?",
                              (self.kind,)).fetchone()[0]
        return n > 0, f"{n} 份工作区索引归档"


@register
class FileHistoryAdapter(SourceAdapter):
    name = "file_history"
    kind = "file_history"
    DEFAULT_INTERVAL_S = 1800
    priority = 30
    description = "VS Code 编辑器文件历史（AI 直接改文件时也会留档）"
    label = "文件编辑历史"
    fix_hint = "在 Trae 中打开工作区并保存任意文件；仍不可用时可关闭此采集源"

    def probe(self):
        return (os.path.isdir(HISTORY), HISTORY if os.path.isdir(HISTORY)
                else f"目录不存在: {HISTORY}")

    def collect(self):
        entries = {}
        dirs = 0
        for d in os.listdir(HISTORY):
            ej = os.path.join(HISTORY, d, "entries.json")
            if not os.path.exists(ej):
                continue
            try:
                meta = json.load(open(ej))
            except (OSError, ValueError):
                continue
            res = meta.get("resource") or ""
            if not res:
                continue
            dirs += 1
            entries[res] = [{"timestamp": e.get("timestamp"), "source": e.get("source")}
                            for e in meta.get("entries", []) if e.get("timestamp")]
        put_raw(self.conn, self.kind, "history",
                json.dumps(entries, ensure_ascii=False).encode())
        return {"files": len(entries),
                "entries": sum(len(v) for v in entries.values()), "dirs": dirs}
