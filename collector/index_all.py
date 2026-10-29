"""全工作区历史对话索引器（复用采集适配器）。

依赖两个适配器的存档，不直接读 Trae 目录：
  workspace_index  工作区 -> 会话 id
  file_history     编辑器文件历史
再叠加快照 git / 云端会话的覆盖情况，产出 store/index.db：
  workspace(id, path, name, kind)
  session(session_id, workspace_id, first_ts, last_ts, turn_count, has_snapshot, has_cloud)
  file_history(workspace_id, path, ts, source)

未采集时自动先跑一次采集（保证命令单独执行也能工作）。
"""
import json
import os
import sqlite3
import sys
from urllib.parse import unquote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "collector"))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))
import collect as C  # noqa: E402
import paths  # noqa: E402

INDEX = paths.INDEX
WS_STORAGE = C.WS_STORAGE

SCHEMA = """
CREATE TABLE IF NOT EXISTS workspace (
  workspace_id TEXT PRIMARY KEY,
  path         TEXT,
  name         TEXT,
  kind         TEXT,           -- folder|workspace_file|virtual
  sessions     INTEGER DEFAULT 0,
  storage_dir  TEXT
);
CREATE TABLE IF NOT EXISTS session (
  session_id   TEXT PRIMARY KEY,
  workspace_id TEXT,
  first_ts     INTEGER,
  last_ts      INTEGER,
  turn_hints   INTEGER DEFAULT 0,
  turn_count   INTEGER DEFAULT 0,
  files        INTEGER DEFAULT 0,
  has_snapshot INTEGER DEFAULT 0,
  has_cloud    INTEGER DEFAULT 0,
  title        TEXT
);
CREATE INDEX IF NOT EXISTS idx_session_ws ON session(workspace_id, last_ts DESC);
CREATE TABLE IF NOT EXISTS file_history (
  path         TEXT NOT NULL,
  workspace_id TEXT,
  ts           INTEGER,
  source       TEXT,
  PRIMARY KEY (path, ts)
);
CREATE INDEX IF NOT EXISTS idx_fh_path ON file_history(path);
"""


def ws_name(path):
    """把绝对路径折叠成稳定的项目名（与 kb 的 project facet 对齐）。"""
    if not path:
        return "unknown"
    parts = [p for p in path.strip("/").split("/") if p]
    if "works" in parts:
        i = parts.index("works")
        tail = parts[i + 1:]
        if tail and tail[0] in ("work", "development"):
            tail = tail[1:]
        if len(tail) >= 2:
            return f"{tail[0]}/{'-'.join(tail[1:2])}"
        return tail[0] if tail else path
    if parts and parts[0] in ("Users", "private", "var", "opt", "tmp"):
        tail = parts[2:] if len(parts) > 2 else []
        return "-".join(tail[-2:]) if tail else "root"
    return parts[0] if parts else "unknown"


def _load_raw(conn, kind, source=None):
    sql = "SELECT path FROM raw_object WHERE kind=?"
    args = [kind]
    if source:
        sql += " AND source=?"
        args.append(source)
    row = conn.execute(sql + " ORDER BY created_at DESC LIMIT 1", args).fetchone()
    if not row:
        return None
    return json.load(open(os.path.join(C.RAW, row[0]), encoding="utf-8"))


def ensure_collected(conn):
    """缺哪个源就先跑哪个，避免单独执行本脚本时索引为空。"""
    have = {r[0] for r in conn.execute("SELECT DISTINCT kind FROM raw_object")}
    need = {"workspace_index", "file_history"} - have
    if not need:
        return
    for a in C.registry(conn):
        if a.kind in need:
            a.run()


def build():
    conn = paths.db_connect(INDEX)
    conn.executescript(SCHEMA)
    for t in ("workspace", "session", "file_history"):
        conn.execute(f"DELETE FROM {t}")
    conn.commit()

    m = C.init_db()
    ensure_collected(m)
    ws_index = _load_raw(m, "workspace_index") or {}
    history = _load_raw(m, "file_history") or {}
    if isinstance(ws_index, dict) and "hints" not in next(iter(ws_index.values()), {}):
        ws_index = {k: {"sessions": v, "hints": {s: 0 for s in v}}
                    for k, v in ws_index.items()} if ws_index else {}

    snap_sessions = set(os.listdir(C.SNAPSHOT)) if os.path.isdir(C.SNAPSHOT) else set()
    m.row_factory = sqlite3.Row
    turn_stats = {r["session_id"]: (r["n"], r["files"], r["last"])
                  for r in m.execute(
                      "SELECT session_id, COUNT(*) n, SUM(n_files) files, MAX(ts) last"
                      " FROM turn GROUP BY session_id")}
    cloud = {r["source"] for r in m.execute(
        "SELECT source FROM raw_object WHERE kind='cloud_messages'")}

    # 目录 hash -> 真实工作区路径
    dir_path = {}
    for d in sorted(os.listdir(WS_STORAGE)) if os.path.isdir(WS_STORAGE) else []:
        wj = os.path.join(WS_STORAGE, d, "workspace.json")
        if not os.path.exists(wj):
            continue
        try:
            meta = json.load(open(wj))
        except ValueError:
            continue
        path = (meta.get("folder") or meta.get("workspace") or "").replace("file://", "")
        path = unquote(path)
        if "/Workspaces/" in path and path.endswith("workspace.json"):
            try:
                folders = json.load(open(path)).get("folders") or []
                if folders:
                    path = unquote(folders[0].get("path") or
                                   folders[0].get("uri", "").replace("file://", ""))
            except (OSError, ValueError):
                pass
        dir_path[d] = path

    ws_map = {}
    for d, data in ws_index.items():
        path = dir_path.get(d) or f"virtual:{d}"
        kind = "workspace_file" if path.endswith(".code-workspace") else "folder"
        if not dir_path.get(d):
            kind = "virtual"
        wid = f"W{abs(hash(path)) & 0xFFFFFFFF:08x}"
        ws_map[path] = wid
        ids = data.get("hints") or {s: 0 for s in data.get("sessions", [])}
        conn.execute("INSERT OR REPLACE INTO workspace VALUES (?,?,?,?,?,?)",
                     (wid, path, ws_name(path), kind, len(ids), d))
        for sid, hints in ids.items():
            ts = turn_stats.get(sid)
            conn.execute("""INSERT OR REPLACE INTO session
                (session_id,workspace_id,first_ts,last_ts,turn_hints,turn_count,
                 files,has_snapshot,has_cloud,title)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (sid, wid, None, ts[2] if ts else None, hints,
                 ts[0] if ts else 0, ts[1] if ts else 0,
                 1 if sid in snap_sessions else 0, 1 if sid in cloud else 0, None))

    # 快照里有、但没有任何 workspaceStorage 记录到的会话 -> 归到 unknown
    known = {r[0] for r in conn.execute("SELECT session_id FROM session")}
    conn.execute("INSERT OR IGNORE INTO workspace VALUES "
                 "('W00000000','','unknown','virtual',0,'')")
    orphan = 0
    for sid, (n, files, last) in turn_stats.items():
        if sid in known:
            continue
        orphan += 1
        conn.execute("""INSERT OR REPLACE INTO session
            (session_id,workspace_id,last_ts,turn_count,files,has_snapshot,has_cloud)
            VALUES (?,?,?,?,?,?,?)""",
            (sid, "W00000000", last, n, files, 1, 1 if sid in cloud else 0))

    # 编辑器文件历史（AI 直接改文件也会进这里）
    hcount = 0
    for res, entries in history.items():
        name = ws_name(res)
        wid = next((v for p, v in ws_map.items() if p and res.startswith(p)), None)
        if not wid:
            wid = f"W{abs(hash(name)) & 0xFFFFFFFF:08x}"
            conn.execute("INSERT OR IGNORE INTO workspace VALUES (?,?,?,?,?,?)",
                         (wid, name, name, "virtual", 0, ""))
        for e in entries:
            ts = int(e.get("timestamp") or 0)
            if ts:
                conn.execute("INSERT OR IGNORE INTO file_history VALUES (?,?,?,?)",
                             (res, wid, ts, e.get("source") or ""))
                hcount += 1

    conn.commit()
    stats = {
        "workspaces": conn.execute("SELECT COUNT(*) FROM workspace").fetchone()[0],
        "sessions_total": conn.execute("SELECT COUNT(*) FROM session").fetchone()[0],
        "sessions_with_snapshot": conn.execute(
            "SELECT COUNT(*) FROM session WHERE has_snapshot=1").fetchone()[0],
        "sessions_orphan": orphan,
        "file_history_entries": hcount,
        "workspaces_with_sessions": conn.execute(
            "SELECT COUNT(*) FROM workspace WHERE sessions>0").fetchone()[0],
    }
    conn.close()
    return stats


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=1))
