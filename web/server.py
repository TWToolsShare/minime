"""minime Web 面板：知识浏览 / 图谱 / 画像 / 工作区 / 采集源 / 日志 / 数据源包 / 设置。

纯标准库 http.server + 前端单页，无外部依赖。
安全：只绑定回环地址（127.0.0.1），并校验 Host 头，避免被局域网访问。
"""
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))
sys.path.insert(0, os.path.join(ROOT, "collector"))

import graph as G  # noqa: E402
import oplog  # noqa: E402
import paths  # noqa: E402
import progress as P  # noqa: E402
import retrieval as R  # noqa: E402
import settings_schema as SS  # noqa: E402
import config as C  # noqa: E402

STARTED_AT = time.time()


def _port():
    try:
        return int((SS.load().get("web") or {}).get("port") or 8765)
    except Exception:  # noqa: BLE001
        return 8765


PORT = _port()
LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}


ASSET_TYPES = {".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}


def _asset(path):
    """面板静态资源：只允许 docs/brand 下的 svg/png，文件名取 basename 防穿越。"""
    ext = os.path.splitext(path)[1].lower()
    if ext not in ASSET_TYPES:
        return None
    full = os.path.join(ROOT, "docs/brand", os.path.basename(path))
    return (full, ASSET_TYPES[ext]) if os.path.isfile(full) else None


def api_health():
    """健康检查：结构化摘要 + 「改了没重启」检测。"""
    stale = []
    for rel in ("web/server.py", "web/index.html", "pipeline/retrieval.py",
                "pipeline/embed.py", "pipeline/chat.py", "pipeline/schema.py",
                "collector/base.py", "pipeline/progress.py"):
        p = os.path.join(ROOT, rel)
        if os.path.exists(p) and os.path.getmtime(p) > STARTED_AT + 1:
            stale.append(rel)
    try:
        atoms = R.connect().execute(
            "SELECT COUNT(*) FROM atom WHERE state='active'").fetchone()[0]
    except sqlite3.Error:
        atoms = 0
    return {"ok": True, "uptime_s": int(time.time() - STARTED_AT), "port": PORT,
            "atoms": atoms,
            "jobs": list((P.active_job() or {}).keys()),
            "code_stale": stale}


def api_restart():
    """就地重启面板：重新执行 web/server.py，保存的端口与代码改动生效。

    先返回响应、延迟 exec，避免浏览器侧连接被直接掐断。
    """
    new_port = _port()
    oplog.write("warn", "web.restart", old_port=PORT, new_port=new_port)
    t = threading.Timer(0.4, _reexec)
    t.daemon = True
    t.start()
    return {"ok": True, "port": new_port, "pid": os.getpid()}


def _fix_std_streams():
    """execv 前修复标准流：后台/nohup 启动时 0/1/2 可能已失效，
    直接继承会让新解释器在 init_sys_streams 阶段 Fatal 崩溃。
    这里统一兜底重定向到有效 fd（0 接 /dev/null，1/2 追加写 web.log）。"""
    try:
        devnull = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull, 0)
        os.close(devnull)
    except OSError:
        pass
    log_path = os.path.join(paths.LOGS, "web.log")
    try:
        out = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        os.dup2(out, 1)
        os.dup2(out, 2)
        os.close(out)
    except OSError:
        pass


def _reexec():
    """等价于重新执行 minime web：同一解释器加载最新代码。"""
    args = [sys.executable, os.path.abspath(__file__)] + sys.argv[1:]
    if os.name == "nt":
        # Windows 没有 execv：起一个延迟启动的替身，等旧进程释放端口
        subprocess.Popen([sys.executable, "-c",
                          "import subprocess,sys,time;time.sleep(1);"
                          "subprocess.Popen(sys.argv[1:])", *args])
        os._exit(0)
    _fix_std_streams()
    os.execv(args[0], args)


def api_facets():
    conn = R.connect()
    return R.facet_tree(conn)


def api_atoms(qs):
    conn = R.connect()
    q = qs.get("q", [""])[0]
    project = qs.get("project", [""])[0]
    tech = qs.get("tech", [""])[0]
    kinds = [k for k in qs.get("kind", [""])[0].split(",") if k]
    limit = int(qs.get("limit", ["30"])[0])
    conds = []
    if project:
        conds.append({"facet": {"dim": "project", "eq": project}})
    if tech:
        conds.append({"facet": {"dim": "tech", "in": tech.split(",")}})
    facet = qs.get("facet", [""])[0]
    if facet and ":" in facet:
        dim, value = facet.split(":", 1)
        conds.append({"facet": {"dim": dim, "eq": value}})
    sens = [x for x in qs.get("sensitivity", [""])[0].split(",") if x]
    if sens:
        conds.append({"sensitivity": {"in": sens}})
    coll = qs.get("collection", [""])[0]
    if coll:
        conds.append({"collection": {"eq": coll}})
    flt = None
    if len(conds) == 1:
        flt = conds[0]
    elif conds:
        flt = {"all": conds}
    state = qs.get("state", ["active"])[0]
    if state not in ("active", "dormant", "superseded", "archived"):
        state = "active"
    hits = R.search(conn, q or None, flt, kinds=kinds or None, limit=limit, state=state)
    matched = R.search(conn, q or None, flt, kinds=kinds or None, count_only=True,
                       state=state)
    ids = [h["atom"]["atom_id"] for h in hits]
    facets = R.facets_of(conn, ids)
    fb = {}
    if ids:
        ph = ",".join("?" * len(ids))
        # 每条知识最近一次「有效反馈」+ 各事件次数：让采用/引用/不相关在刷新后仍然可见
        for r in conn.execute(
                f"SELECT atom_id, event FROM usage_event WHERE id IN ("
                f" SELECT MAX(id) FROM usage_event WHERE atom_id IN ({ph})"
                f" AND event IN ('accepted','cited','rejected','verified','corrected')"
                f" GROUP BY atom_id)", ids):
            fb.setdefault(r["atom_id"], {})["last"] = r["event"]
        for r in conn.execute(
                f"SELECT atom_id, event, COUNT(*) n FROM usage_event"
                f" WHERE atom_id IN ({ph})"
                f" AND event IN ('accepted','cited','rejected','verified','corrected')"
                f" GROUP BY atom_id, event", ids):
            fb.setdefault(r["atom_id"], {})[r["event"]] = r["n"]
    items = []
    for h in hits:
        a = dict(h["atom"])
        a.pop("fingerprint", None)
        a["score"] = h["score"]
        a["breakdown"] = h["breakdown"]
        a["facets"] = facets.get(a["atom_id"], [])
        a["packs"] = h.get("packs", [])
        a["feedback"] = fb.get(a["atom_id"], {})
        a["sources"] = [dict(r) for r in conn.execute(
            "SELECT source_kind,source_ref FROM atom_source WHERE atom_id=? LIMIT 3",
            (a["atom_id"],))]
        items.append(a)
    total = conn.execute("SELECT COUNT(*) FROM atom WHERE state='active'").fetchone()[0]
    states = {r[0]: r[1] for r in conn.execute(
        "SELECT state, COUNT(*) FROM atom GROUP BY state")}
    thr = int((C.load().get("feedback") or {}).get("dormant_after_rejects") or 0)
    return {"total_atoms": total, "states": states, "state": state,
            "reject_threshold": thr,
            "matched": matched, "count": len(items), "items": items}


def api_graph(qs):
    conn = R.connect()
    node = qs.get("node", [""])[0]
    if node:
        return G.node_detail(conn, node)
    limit = int(qs.get("limit", ["80"])[0])
    return G.graph_json(conn, limit, around=qs.get("around", [""])[0] or None)


def api_profile():
    conn = R.connect()
    row = conn.execute(
        "SELECT payload FROM profile_snapshot ORDER BY created_at DESC LIMIT 1").fetchone()
    if not row:
        return {"dimensions": []}
    return json.loads(row["payload"])


def api_stats():
    conn = R.connect()
    m = sqlite3.connect(paths.MANIFEST)
    m.row_factory = sqlite3.Row
    raw = m.execute("SELECT COUNT(*) n, COALESCE(SUM(size),0) b FROM raw_object").fetchone()
    turns = m.execute("SELECT COUNT(*) n FROM turn").fetchone()["n"]
    files = m.execute("SELECT COUNT(*) n FROM turn_file").fetchone()["n"]
    sessions = m.execute("SELECT COUNT(DISTINCT session_id) n FROM turn").fetchone()["n"]
    return {
        "atoms": conn.execute("SELECT COUNT(*) FROM atom WHERE state='active'").fetchone()[0],
        "entities": conn.execute("SELECT COUNT(*) FROM entity").fetchone()[0],
        "relations": conn.execute("SELECT COUNT(*) FROM relation").fetchone()[0],
        "usage_events": conn.execute("SELECT COUNT(*) FROM usage_event").fetchone()[0],
        "raw_objects": raw["n"], "raw_bytes": raw["b"],
        "snapshot_sessions": sessions, "turns": turns, "file_changes": files,
    }


def api_feedback(body):
    conn = R.connect()
    res = R.record_feedback(conn, body["atom_id"], body["event"],
                            body.get("detail"), surface="web")
    return {"ok": True, **(res or {})}


def api_restore(body):
    """把降权或归档的知识恢复为正常检索状态。"""
    conn = R.connect()
    return {"ok": True, **R.restore_atom(conn, body["atom_id"])}


def api_workspaces(qs):
    idx = paths.INDEX
    if not os.path.exists(idx):
        return {"workspaces": [], "total_sessions": 0}
    conn = sqlite3.connect(idx)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT w.workspace_id, w.name, w.path, w.kind, w.sessions,
               COUNT(s.session_id) AS indexed_sessions,
               SUM(CASE WHEN s.has_snapshot=1 THEN 1 ELSE 0 END) AS with_snapshot,
               MAX(s.last_ts) AS last_ts,
               (SELECT COUNT(*) FROM file_history f
                WHERE f.workspace_id=w.workspace_id) AS files
        FROM workspace w LEFT JOIN session s ON s.workspace_id=w.workspace_id
        GROUP BY w.workspace_id
        HAVING indexed_sessions > 0 OR files > 0
        ORDER BY indexed_sessions DESC, files DESC""").fetchall()
    total = conn.execute("SELECT COUNT(*) FROM session").fetchone()[0]
    turns = conn.execute("SELECT COUNT(*) FROM session WHERE has_snapshot=1").fetchone()[0]
    conn.close()
    return {"total_sessions": total, "sessions_with_snapshot": turns,
            "workspaces": [dict(r) for r in rows]}


def api_config(qs, body):
    """GET: {saved, current, schema, path}；POST: {values:{key:value}} 按 schema 校验保存。"""
    if body is None:
        return {"saved": SS.saved_view(), "current": SS.current_view(),
                "schema": SS.schema(), "path": SS.PATH}
    values = body.get("values") if "values" in body else body
    res = SS.save(values)
    oplog.write("info" if res["ok"] else "warn", "config.save",
                applied=list(res.get("applied") or {}), errors=res.get("errors"))
    return res


def api_logs(qs):
    limit = int(qs.get("limit", ["200"])[0])
    level = qs.get("level", ["all"])[0]
    kw = qs.get("q", [""])[0]
    return {"items": oplog.list_logs(level, limit, kw), "stats": oplog.stats()}


def api_log_detail(qs):
    entry = oplog.detail(qs.get("id", [""])[0])
    return entry or {"error": "not found"}


def api_packs(qs, body):
    """资源包管理：包 / 知识集 / 开关 / 筛选建集 / 属性目录。

    GET  -> {packs, attrs, sensitivity}
    POST -> {action: enable|create_pack|create_collection|preview|fill|drop_collection
                     |delete_pack|assign_sensitivity|collection_atoms}
    """
    import packs as PK  # noqa: PLC0415
    conn = R.connect()
    PK.ensure(conn)
    if body is None:
        return {"packs": PK.list_packs(conn), "attrs": PK.attr_catalog(),
                "sensitivity": {r[0] or "internal": r[1] for r in conn.execute(
                    "SELECT sensitivity,COUNT(*) FROM atom WHERE state='active' GROUP BY 1")}}
    try:
        action = body.get("action")
        if action == "enable":
            return PK.set_enabled(body["collection_id"], bool(body.get("enabled")), conn)
        if action == "create_pack":
            name = (body.get("name") or "").strip()
            if not name:
                return {"error": "包名不能为空"}
            return PK.create_pack(name, body.get("description") or "",
                                  body.get("author") or "", conn)
        if action == "delete_pack":
            return PK.delete_pack(body["pack_id"], conn)
        if action == "create_collection":
            name = (body.get("name") or "").strip()
            if not name:
                return {"error": "知识集名不能为空"}
            c = PK.add_collection(body["pack_id"], name, body.get("description") or "",
                                  bool(body.get("enabled")), conn)
            if body.get("filters"):
                c["filled"] = PK.fill_collection(c["collection_id"], body["filters"], conn)
            return c
        if action == "drop_collection":
            return PK.drop_collection(body["collection_id"], conn)
        if action == "preview":
            return PK.preview_filter(body.get("filters") or {}, int(body.get("limit") or 20), conn)
        if action == "fill":
            return {"added": PK.fill_collection(body["collection_id"],
                                                body.get("filters") or {}, conn)}
        if action == "collection_atoms":
            return {"items": PK.collection_atoms(conn, body["collection_id"],
                                                 int(body.get("limit") or 100),
                                                 int(body.get("offset") or 0))}
        if action == "assign_sensitivity":
            return PK.assign_sensitivity(conn, only_null=True)
        return {"error": f"unknown action: {action}"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def api_pack(qs, body):
    """数据源包：GET 列表 + 脱敏体检；POST {action:audit|export|import|inspect|upload}。

    upload 走 multipart：把浏览器选的 .minime.zip 存进 store/packs，之后与本地包一样可 inspect/导入。
    """
    import pack as P  # noqa: PLC0415
    packs_dir = paths.PACKS
    os.makedirs(packs_dir, exist_ok=True)
    if body is None:
        items = []
        for fn in sorted(os.listdir(packs_dir)):
            if fn.endswith(".minime.zip"):
                full = os.path.join(packs_dir, fn)
                items.append({"file": fn, "size_kb": round(os.path.getsize(full) / 1024, 1),
                              "mtime": int(os.path.getmtime(full))})
        try:
            audit = P.audit_atoms()
        except Exception as e:  # noqa: BLE001
            audit = {"error": f"{type(e).__name__}: {e}", "findings": [], "atoms": 0}
        return {"packs": items, "dir": packs_dir, "audit": audit}
    action = body.get("action")
    try:
        if action == "audit":
            return P.audit_atoms()
        if action == "export":
            name = (body.get("name") or "my-worker").strip()
            out = os.path.join(packs_dir, f"{name}.minime.zip")
            return P.export(name=name, out=out,
                            include_raw=bool(body.get("include_raw")),
                            with_profile=not body.get("no_profile"),
                            author=body.get("author") or "",
                            collections=body.get("collections") or None,
                            attrs=body.get("attrs") or None,
                            description=body.get("description") or "",
                            pack_id=body.get("pack_id"),
                            strict=not body.get("loose"))
        if action == "inspect":
            return P.inspect(os.path.join(packs_dir, body["file"]))
        if action == "import":
            return P.import_pack(os.path.join(packs_dir, body["file"]),
                                 dry_run=bool(body.get("dry_run", True)),
                                 conflict=body.get("conflict") or "merge",
                                 enable=bool(body.get("enable")))
        if action == "delete":
            fn = os.path.basename(body["file"])
            p = os.path.join(packs_dir, fn)
            if not fn.endswith(".minime.zip") or not os.path.isfile(p):
                return {"error": f"包不存在：{fn}"}
            os.remove(p)
            return {"deleted": fn}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
    return {"error": f"unknown action: {action}"}


def api_pack_upload(ctype, data):
    """保存上传的包：解析 multipart，只留文件部分；文件名做安全化。"""
    import re  # noqa: PLC0415
    m = re.search(r"boundary=(?:\"([^\"]+)\"|([^;]+))", ctype or "")
    if not m:
        raise ValueError("不是合法的 multipart 请求（缺 boundary）")
    boundary = ("--" + (m.group(1) or m.group(2))).encode()
    parts = data.split(boundary)
    filename, payload = None, None
    for part in parts:
        head, _, rest = part.partition(b"\r\n\r\n")
        if not head:
            continue
        hm = re.search(rb'filename="?([^"\r\n;]+)"?', head)
        if not hm:
            continue
        filename = hm.group(1).decode("utf-8", "replace")
        payload = rest[:-2] if rest.endswith(b"\r\n") else rest   # 去掉尾随 CRLF
        break
    if not filename or payload is None:
        raise ValueError("没有找到上传的文件字段")
    if not payload.startswith(b"PK"):
        raise ValueError("上传的不是 zip 包（minime 数据源包应为 .minime.zip）")
    packs_dir = paths.PACKS
    os.makedirs(packs_dir, exist_ok=True)
    safe = re.sub(r"[^\w.\-]+", "_", os.path.basename(filename))
    if not safe.endswith(".zip"):
        safe += ".minime.zip"
    dst = os.path.join(packs_dir, safe)
    with open(dst, "wb") as f:
        f.write(payload)
    return {"file": safe, "size_kb": round(len(payload) / 1024, 1)}


def api_llm_health():
    import config as C  # noqa: PLC0415
    import llm_enrich as L  # noqa: PLC0415
    return L.health(C.load())


def api_sync(qs, body):
    """记忆同步：GET 看状态与候选，POST {dry_run:false} 实际写入。"""
    import memory_sync as MS  # noqa: PLC0415
    import retrieval as RR  # noqa: PLC0415
    conn = RR.connect()
    if body is None:
        out = MS.stats(conn)
        out["preview"] = MS.run(dry_run=True).get("candidates", [])[:20]
        return out
    return MS.run(dry_run=bool(body.get("dry_run", True)))


def api_collectors(qs, body):
    """采集适配器状态；POST {adapter, force} 后台触发一次采集。"""
    import collect as C  # noqa: PLC0415
    if body is None:
        conn = C.init_db()
        try:
            return C.AutoCollector(conn).status()
        finally:
            conn.close()
    adapter = body.get("adapter")
    names = {adapter} if adapter else None

    def _run(job_id):
        # SQLite 连接不能跨线程：后台线程里自建、自关，别复用主线程的 conn
        conn = C.init_db()
        try:
            label = adapter
            if adapter:
                for a in C.registry(conn, {adapter}):
                    label = a.label or adapter
                P._job_update(job_id, total=1, message=f"正在采集「{label}」")
            res = C.AutoCollector(conn, names).tick(force=True)
            if adapter:
                P._job_update(job_id, done=1, message=f"「{label}」采集完成")
            return {"results": res}
        finally:
            conn.close()

    return P.start("collect", _run)


def api_progress(qs, body):
    if body is None:
        return P.snapshot()
    action = body.get("action")
    if action == "llm":
        return P.start("llm", P.run_llm_job, body.get("limit"))
    if action == "pipeline":
        steps = tuple(body.get("steps") or ("collect", "index", "normalize"))
        return P.start("pipeline", P.run_pipeline_job, steps)
    if action == "embed":
        return P.start("embed", P.run_embed_job)
    return {"error": f"unknown action: {action}"}


def api_job(qs, body):
    if body is not None and body.get("action") == "cancel":
        return P.cancel(body["job_id"]) or {"error": "job not found"}
    jid = qs.get("id", [""])[0]
    if not jid:
        return {"jobs": P.list_jobs(limit=int(qs.get("limit", ["20"])[0]))}
    return P.job(jid) or {"error": "job not found"}


def api_chat(qs, body):
    """对话：GET 列表/单会话；POST {action, ...}。

    GET  ?id=<sid>            取会话全文
    GET  （无参）              最近会话列表
    POST {action:"new"}       建会话
    POST {action:"ask", session_id, question}  一轮问答
    POST {action:"rename", session_id, title}
    POST {action:"delete", session_id}
    """
    import chat as C  # noqa: PLC0415
    if body is None:
        sid = qs.get("id", [""])[0]
        if sid:
            s = C.get_session(sid)
            return s or {"error": "session not found"}
        return {"sessions": C.list_sessions(), "model": _chat_model()}
    action = body.get("action")
    if action == "new":
        sid = C.new_session(body.get("title") or "新对话", _chat_model())
        return {"session_id": sid}
    if action == "ask":
        q = (body.get("question") or "").strip()
        if not q:
            return {"error": "问题不能为空"}
        sid = body.get("session_id") or C.new_session(q[:40], _chat_model())
        return C.ask(sid, q, int(body.get("top_k") or C.TOP_K))
    if action == "rename":
        C.rename_session(body["session_id"], body.get("title") or "新对话")
        return {"ok": True}
    if action == "delete":
        C.delete_session(body["session_id"])
        return {"ok": True}
    return {"error": f"unknown action: {action}"}


def _chat_model():
    import config as C  # noqa: PLC0415
    return (C.load().get("llm") or {}).get("model")


ROUTES = {
    "/api/health": lambda qs, body: api_health(),
    "/api/restart": lambda qs, body: api_restart(),
    "/api/logs": lambda qs, body: api_logs(qs),
    "/api/log": lambda qs, body: api_log_detail(qs),
    "/api/pack": lambda qs, body: api_pack(qs, body),
    "/api/packs": lambda qs, body: api_packs(qs, body),
    "/api/facets": lambda qs, body: api_facets(),
    "/api/atoms": lambda qs, body: api_atoms(qs),
    "/api/graph": lambda qs, body: api_graph(qs),
    "/api/profile": lambda qs, body: api_profile(),
    "/api/stats": lambda qs, body: api_stats(),
    "/api/workspaces": lambda qs, body: api_workspaces(qs),
    "/api/config": lambda qs, body: api_config(qs, body),
    "/api/llm/health": lambda qs, body: api_llm_health(),
    "/api/sync": lambda qs, body: api_sync(qs, body),
    "/api/collectors": lambda qs, body: api_collectors(qs, body),
    "/api/progress": lambda qs, body: api_progress(qs, body),
    "/api/job": lambda qs, body: api_job(qs, body),
    "/api/chat": lambda qs, body: api_chat(qs, body),
    "/api/feedback": lambda qs, body: api_feedback(body),
    "/api/atom/restore": lambda qs, body: api_restore(body),
}


class Handler(BaseHTTPRequestHandler):
    server_version = "minime"
    sys_version = ""

    def log_message(self, *a):
        pass

    def _send(self, code, payload, ctype="application/json; charset=utf-8"):
        data = payload if isinstance(payload, bytes) else json.dumps(
            payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path, download=False, ctype=None):
        """直出文件（导出下载 / 面板页面 / 品牌图），大文件不占内存。"""
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", ctype or (
            "application/octet-stream" if download else "text/html; charset=utf-8"))
        if download:
            fn = os.path.basename(path)
            self.send_header("Content-Disposition",
                             f'attachment; filename="{fn}"; '
                             f"filename*=UTF-8''{quote(fn)}")
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(256 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _error(self, code, kind, message):
        return self._send(code, {"error": {"message": message, "type": kind, "code": code}})

    def _host_ok(self):
        """面板只接受回环 Host，防止 DNS rebinding / 局域网访问。"""
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in LOOPBACK

    def _dispatch(self, body):
        u = urlparse(self.path)
        t0 = time.time()
        if not self._host_ok():
            return self._error(403, "host_not_allowed", "仅允许通过 127.0.0.1 访问")
        if u.path in ("/", "/index.html"):
            p = os.path.join(ROOT, "web/index.html")
            if os.path.exists(p):
                return self._send_file(p)
            return self._send(200, "<pre>面板文件缺失：web/index.html</pre>".encode(),
                              "text/html; charset=utf-8")
        if u.path == "/api/pack/download":
            return self._download(parse_qs(u.query))
        asset = _asset(u.path)
        if asset:
            return self._send_file(asset[0], ctype=asset[1])
        fn = ROUTES.get(u.path)
        if not fn:
            return self._error(404, "not_found", f"未知接口 {u.path}")
        qs = parse_qs(u.query)
        try:
            res = fn(qs, body)
        except Exception as e:  # noqa: BLE001
            oplog.write("error", "web.error", path=u.path,
                        error=f"{type(e).__name__}: {e}",
                        trace=traceback.format_exc()[-1200:])
            return self._error(500, "internal", f"{type(e).__name__}: {e}")
        # 只记业务动作，不记轮询（GET 读接口不进日志）
        if body is not None and not u.path.startswith("/api/health"):
            oplog.write("info", "web.action", path=u.path,
                        elapsed_ms=int((time.time() - t0) * 1000),
                        ok=not (isinstance(res, dict) and res.get("error")))
        return self._send(200, res)

    def _download(self, qs):
        """下载 store/packs 下的包；文件名只取 basename，防止路径穿越。"""
        fn = os.path.basename(qs.get("file", [""])[0])
        p = os.path.join(paths.PACKS, fn)
        if not fn.endswith(".minime.zip") or not os.path.isfile(p):
            return self._error(404, "not_found", f"包不存在：{fn}")
        oplog.write("info", "pack.download", file=fn)
        return self._send_file(p, download=True)

    def do_GET(self):
        self._dispatch(None)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        ctype = self.headers.get("Content-Type") or ""
        # 上传包走 multipart，整体读入后交给 api_pack_upload
        if ctype.startswith("multipart/form-data"):
            if n > 256 * 1024 * 1024:
                return self._error(413, "too_large", "包超过 256MB")
            data = self.rfile.read(n)
            try:
                res = api_pack_upload(ctype, data)
            except Exception as e:  # noqa: BLE001
                return self._error(500, "internal", f"{type(e).__name__}: {e}")
            oplog.write("info", "pack.upload", **res)
            return self._send(200, res)
        if n > 64 * 1024 * 1024:
            return self._error(413, "too_large", "请求体过大")
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError as e:
            return self._error(400, "bad_json", f"请求体不是合法 JSON: {e}")
        self._dispatch(body)


def main():
    # 数据目录被清空后 kb.db 还没有任何表，各接口会统一报 no such table。
    # 启动时先把表结构建好，让面板在任何状态下都能打开，用户可以直接从页面点采集。
    import schema  # noqa: PLC0415
    try:
        schema.init().close()
    except Exception as e:  # noqa: BLE001 — 建表失败不该阻止面板启动
        oplog.write("error", "web.schema_init", error=f"{type(e).__name__}: {e}")
    try:
        sys.path.insert(0, os.path.join(ROOT, "collector"))
        import base as collector_base  # noqa: PLC0415
        collector_base.init_db().close()      # manifest.db: raw_object / collect_state
        import extract_turns as ET  # noqa: PLC0415
        m = sqlite3.connect(paths.MANIFEST)   # manifest.db: turn / turn_file
        try:
            ET.ensure_schema(m)
            m.commit()
        finally:
            m.close()
    except Exception as e:  # noqa: BLE001
        oplog.write("error", "web.manifest_init", error=f"{type(e).__name__}: {e}")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"minime web: http://127.0.0.1:{PORT}/")
    oplog.write("info", "web.start", port=PORT, pid=os.getpid())
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        oplog.write("info", "web.stop", reason="keyboard_interrupt")


if __name__ == "__main__":
    main()
