"""处理进度 + 后台任务。

解决两件事：
  1. 「我采了多少、洗干净多少、LLM 归纳到哪了」——统一成一个 progress 快照
  2. 面板上点「立即归纳」不再卡住页面——长任务丢到后台线程，前端轮询 job 状态

进度口径（全部可按来源下钻）：
  turns        快照轮次（有实质改动才是 LLM 候选）
  candidates   LLM 候选轮次（additions+deletions>=5 或 n_added>=2）
  processed    llm_cursor 中已归纳轮次（无论出没出结论）
  atoms        llm_atom 里实际的归纳产物
  pending      candidates - processed

后台任务：store/jobs.db 的 job 表；同一 kind 同时只允许一个，避免重复烧模型。
不支持跨进程强制终止（线程无法安全 kill），但可标记 cancelled，任务在下一轮检查时自行退出。
"""
import json
import os
import sqlite3
import threading
import time
import traceback
import uuid

import paths

ROOT = paths.CODE_ROOT
KB = paths.KB
MANIFEST = paths.MANIFEST
JOBS = paths.JOBS
sys_path = os.path.join(ROOT, "pipeline")

import sys  # noqa: E402
sys.path.insert(0, sys_path)

_ACTIVE = {}          # kind -> job_id（进程内）
_ACTIVE_LOCK = threading.Lock()


# ---------------------------------------------------------------- 进度
def _turn_stats():
    """候选轮次统计（manifest.db）。"""
    if not os.path.exists(MANIFEST):
        return {"turns": 0, "candidates": 0}
    m = paths.db_connect(MANIFEST)
    try:
        turns = m.execute("SELECT COUNT(*) FROM turn").fetchone()[0]
        cand = m.execute("""SELECT COUNT(*) FROM turn
                            WHERE (additions+deletions) >= 5 OR n_added >= 2""").fetchone()[0]
        return {"turns": turns, "candidates": cand}
    except sqlite3.Error:
        return {"turns": 0, "candidates": 0}
    finally:
        m.close()


def _kb_stats():
    if not os.path.exists(KB):
        return {"atoms": 0, "processed": 0, "processed_with_atoms": 0, "llm_atoms": 0,
                "by_source": {}, "by_kind": {}, "last_processed_at": None}
    c = paths.db_connect(KB, row_factory=sqlite3.Row)
    try:
        atoms = c.execute("SELECT COUNT(*) FROM atom WHERE state='active'").fetchone()[0]
        processed, with_atoms, last = c.execute(
            "SELECT COUNT(*), COALESCE(SUM(CASE WHEN atoms>0 THEN 1 ELSE 0 END),0),"
            " MAX(processed_at) FROM llm_cursor").fetchone()
        llm_atoms = c.execute("SELECT COUNT(*) FROM llm_atom").fetchone()[0]
        by_source = {r[0]: r[1] for r in c.execute(
            "SELECT source_kind, COUNT(*) FROM atom_source GROUP BY source_kind"
            " ORDER BY 2 DESC")}
        by_kind = {r[0]: r[1] for r in c.execute(
            "SELECT kind, COUNT(*) FROM atom WHERE state='active' GROUP BY kind"
            " ORDER BY 2 DESC")}
        return {"atoms": atoms, "processed": processed,
                "processed_with_atoms": with_atoms, "llm_atoms": llm_atoms,
                "by_source": by_source, "by_kind": by_kind, "last_processed_at": last}
    finally:
        c.close()


def _llm_cfg():
    import config  # noqa: PLC0415
    cfg = config.load()
    return cfg.get("llm") or {}


def _collect_stats():
    if not os.path.exists(MANIFEST):
        return []
    import config  # noqa: PLC0415
    m = paths.db_connect(MANIFEST, row_factory=sqlite3.Row)
    try:
        rows = m.execute("SELECT adapter,last_run,last_status,fails,runs,last_elapsed"
                         " FROM collect_state ORDER BY adapter").fetchall()
        enabled = ((config.load().get("collect") or {}).get("enabled") or {})
        out = []
        for r in rows:
            d = dict(r)
            d["enabled"] = enabled.get(d["adapter"], True) is not False
            out.append(d)
        return out
    except sqlite3.Error:
        return []
    finally:
        m.close()


def llm_progress():
    """LLM 归纳进度快照。"""
    t = _turn_stats()
    k = _kb_stats()
    cand, done = t["candidates"], k["processed"]
    # 已处理的轮次可能多于当前候选（历史轮次后来被快照裁剪），比值封顶 100
    pct = 0.0 if not cand else min(100.0, round(done / cand * 100, 1))
    cfg = _llm_cfg()
    return {
        "enabled": bool(cfg.get("enabled")),
        "model": cfg.get("model"),
        "base_url": cfg.get("base_url"),
        "turns": t["turns"],
        "candidates": cand,
        "processed": done,
        "pending": max(0, cand - done),
        "percent": pct,
        "processed_with_atoms": k["processed_with_atoms"],
        "llm_atoms": k["llm_atoms"],
        "atoms": k["atoms"],
        "last_processed_at": k["last_processed_at"],
        "by_source": k["by_source"],
        "by_kind": k["by_kind"],
        "per_run": cfg.get("max_atoms_per_run") or 40,
        "running": active_job("llm"),
        "collect": _collect_stats(),
    }


def snapshot():
    """面板「处理进度」视图的完整数据。"""
    p = llm_progress()
    p["collect"] = _collect_stats()
    p["jobs"] = list_jobs(limit=10)
    p["generated_at"] = int(time.time())
    return p


# ---------------------------------------------------------------- 后台任务
def _jobs_conn():
    paths.ensure()
    os.makedirs(os.path.dirname(JOBS), exist_ok=True)
    c = paths.db_connect(JOBS, check_same_thread=False)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS job (
      job_id     TEXT PRIMARY KEY,
      kind       TEXT NOT NULL,
      status     TEXT NOT NULL,     -- running|ok|error|cancelled
      total      INTEGER DEFAULT 0,
      done       INTEGER DEFAULT 0,
      message    TEXT,
      result     TEXT,
      started_at INTEGER NOT NULL,
      ended_at   INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_job_started ON job(started_at DESC);
    """)
    c.commit()
    return c


def _job_update(job_id, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    c = _jobs_conn()
    try:
        if "message" in fields and "status" not in fields:
            # 取消标记优先：任务自己回报的进度文案不能把 cancelling 覆盖掉
            c.execute(f"UPDATE job SET {cols} WHERE job_id=?"
                      " AND COALESCE(message,'') != 'cancelling'",
                      (*fields.values(), job_id))
        else:
            c.execute(f"UPDATE job SET {cols} WHERE job_id=?", (*fields.values(), job_id))
        c.commit()
    finally:
        c.close()


def job(job_id):
    c = _jobs_conn()
    c.row_factory = sqlite3.Row
    try:
        r = c.execute("SELECT * FROM job WHERE job_id=?", (job_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["result"] = json.loads(d["result"]) if d.get("result") else None
        return d
    finally:
        c.close()


def list_jobs(limit=20):
    c = _jobs_conn()
    c.row_factory = sqlite3.Row
    try:
        rows = c.execute("SELECT job_id,kind,status,total,done,message,started_at,ended_at"
                         " FROM job ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["elapsed_s"] = int((d["ended_at"] or time.time()) - d["started_at"])
            out.append(d)
        return out
    finally:
        c.close()


def active_job(kind=None):
    with _ACTIVE_LOCK:
        items = list(_ACTIVE.items())
    return {k: v for k, v in items if kind is None or k == kind} or None


def _cleanup_jobs(keep=200):
    c = _jobs_conn()
    try:
        c.execute("DELETE FROM job WHERE job_id NOT IN"
                  " (SELECT job_id FROM job ORDER BY started_at DESC LIMIT ?)", (keep,))
        c.commit()
    finally:
        c.close()


def cancel(job_id):
    """标记取消：任务在每个条目之间检查，不保证立刻生效。"""
    c = _jobs_conn()
    try:
        c.execute("UPDATE job SET message='cancelling' WHERE job_id=? AND status='running'",
                  (job_id,))
        c.commit()
    finally:
        c.close()
    return job(job_id)


def _cancelled(job_id):
    j = job(job_id)
    return bool(j and j["message"] == "cancelling")


def start(kind, target, *args, **kwargs):
    """在后台线程启动任务。同一 kind 已在跑则直接返回现有 job（不重复启动）。"""
    with _ACTIVE_LOCK:
        if kind in _ACTIVE:
            return {"job_id": _ACTIVE[kind], "status": "running", "reused": True}
        job_id = f"{kind}-{int(time.time())}-{uuid.uuid4().hex[:6]}"
        _ACTIVE[kind] = job_id
    c = _jobs_conn()
    try:
        c.execute("INSERT INTO job (job_id,kind,status,started_at) VALUES (?,?,?,?)",
                  (job_id, kind, "running", int(time.time())))
        c.commit()
    finally:
        c.close()

    def runner():
        status, result = "ok", None
        try:
            result = target(job_id, *args, **kwargs)
        except Exception as e:  # noqa: BLE001 — 后台任务异常要落库，不能静默
            status = "error"
            result = {"error": f"{type(e).__name__}: {e}",
                      "trace": traceback.format_exc()[-1500:]}
        finally:
            # 取消的最终判定以任务回报为准：跑到一半被叫停也必须落成 cancelled
            if isinstance(result, dict) and result.get("cancelled"):
                status = "cancelled"
            elif status == "error":
                _job_update(job_id, message=result["error"])
            final_msg = (f"已停止（完成 {result.get('done', 0)} 步）"
                         if status == "cancelled" else None)
            _job_update(job_id, status=status, ended_at=int(time.time()),
                        result=json.dumps(result, ensure_ascii=False, default=str),
                        **({"message": final_msg} if final_msg else {}))
            with _ACTIVE_LOCK:
                _ACTIVE.pop(kind, None)
            try:
                import oplog  # noqa: PLC0415
                level = {"ok": "info", "cancelled": "warn"}.get(status, "error")
                oplog.write(level, f"job.{kind}",
                            job_id=job_id, status=status,
                            **(result if isinstance(result, dict) else {}))
            except Exception:  # noqa: BLE001
                pass
            _cleanup_jobs()

    threading.Thread(target=runner, name=f"minime-{kind}", daemon=True).start()
    return {"job_id": job_id, "status": "running"}


# ---------------------------------------------------------------- 任务体
def run_llm_job(job_id, limit=None):
    """后台执行 LLM 归纳，逐条回报进度。"""
    import llm_enrich as L  # noqa: PLC0415
    cfg = L.config.load()
    if not cfg["llm"]["enabled"]:
        raise RuntimeError("LLM 归纳未启用：在设置页打开「启用 LLM 归纳」")
    h = L.health(cfg)
    if not h["ok"]:
        raise RuntimeError(f"本地模型不可达：{h.get('error')}")
    conn = L.schema.init()
    # 增量写入：归纳只是往现有知识库里补几条，不能用重建模式的 Writer
    w = L.Writer(conn, full=False)
    # limit=None 用配置上限；limit=0 表示不限量（一次性归纳全部待处理轮次）
    n = None if limit == 0 else int(limit or cfg["llm"]["max_atoms_per_run"] or 40)
    targets = L.pick_targets(conn, cfg, n)
    scope = "全部" if n is None else f"上限 {n} 轮"
    _job_update(job_id, total=len(targets), done=0,
                message=f"待归纳 {len(targets)} 轮（{scope}，模型 {cfg['llm']['model']}）")
    created = skipped = errors = 0
    for i, t in enumerate(targets, 1):
        if _cancelled(job_id):
            w.rebuild_fts_tags()
            conn.commit()
            return {"cancelled": True, "created": created, "skipped": skipped,
                    "errors": errors, "done": i - 1, "total": len(targets)}
        project = L.project_of_session(t["session_id"])
        try:
            c2, s2, e2 = L._run_batch(w, conn, cfg, [(t, project,
                                                     L.build_prompt(t, project))], 0, 0, 0)
            created, skipped, errors = created + c2, skipped + s2, errors + e2
        except Exception as e:  # noqa: BLE001 — 单轮失败不影响后续
            errors += 1
            L._mark_done(conn, t, 0)
            _job_update(job_id, message=f"第 {i} 轮失败：{type(e).__name__}")
        _job_update(job_id, done=i,
                    message=f"已处理 {i}/{len(targets)} · 新增知识 {created} · 跳过 {skipped}")
    w.rebuild_fts_tags()
    conn.commit()
    return {"created": created, "skipped": skipped, "errors": errors,
            "targets": len(targets)}


def run_pipeline_job(job_id, steps=("collect", "index", "normalize")):
    """后台跑指定流水线步骤（采集 / 索引 / 归一化）。"""
    import subprocess  # noqa: PLC0415
    scripts = {"collect": ["collector/collect.py"],
               "index": ["collector/index_all.py"],
               "normalize": ["pipeline/normalize.py"],
               "turns": ["pipeline/extract_turns.py"],
               "graph": ["pipeline/graph.py"],
               "profile": ["pipeline/profile.py"]}
    lock = paths.acquire_lock()
    if lock is None:
        raise RuntimeError("另一个流水线正在运行（数据目录 .pipeline.lock），稍后重试")
    _job_update(job_id, total=len(steps), done=0)
    out = {}
    for i, s in enumerate(steps, 1):
        if s not in scripts:
            continue
        if _cancelled(job_id):
            return {"cancelled": True, "steps": out}
        _job_update(job_id, message=f"正在执行 {s}")
        p = subprocess.run([sys.executable, *scripts[s]], cwd=ROOT,
                           capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError(f"{s} 失败：\n{p.stderr[-1200:]}")
        out[s] = p.stdout.strip().split("\n")[-1][:300]
        _job_update(job_id, done=i, message=f"{s} 完成")
    # 采集/清洗改了知识层，图谱跟着重建一次，否则面板里还是旧关系
    subprocess.run([sys.executable, "pipeline/graph.py"], cwd=ROOT,
                   capture_output=True, text=True)
    paths.release_lock(lock)
    return {"steps": out}


def run_embed_job(job_id):
    """后台重算当前模型下的全部知识向量（换模型/换文本模式后点击按钮触发）。

    只重算向量，不动知识本身。按批推进并回报进度，可取消。
    """
    import config  # noqa: PLC0415
    import embed  # noqa: PLC0415
    import retrieval as R  # noqa: PLC0415
    c = (config.load().get("embedding") or {})
    if not c.get("enabled"):
        raise RuntimeError("向量检索未启用：在设置页打开「启用向量检索」")
    if not embed.healthy(c):
        raise RuntimeError("未配置 embedding.base_url / model")
    mdl = embed.model(c)
    # 先探一次连通性：接口不可达时立即失败并说清楚，别让整批任务挂在超时上
    try:
        embed.embed_texts(c, ["ping"], timeout=min(15.0, float(c.get("query_timeout_s") or 15)))
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"向量服务不可达（{c.get('base_url')}）：{type(e).__name__}: {e}") from e
    conn = R.connect()
    embed.ensure(conn)
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM atom WHERE state IN ('active','archived')").fetchone()[0]
        _job_update(job_id, total=total, done=0,
                    message=f"待重算 {total} 条（模型 {mdl}）")
        done = embedded = 0
        # 每次只推进 batch_size 条：vectors_for 内部会串行发一轮网络请求，
        # 块太大就长时间持有写事务，面板和其他进程会撞 database is locked。
        step = max(1, int(c.get("batch_size") or 32))
        while True:
            if _cancelled(job_id):
                return {"cancelled": True, "done": done, "embedded": embedded, "model": mdl}
            rows = conn.execute(
                """SELECT atom_id, kind, title, summary, body, state, trust_tier
                   FROM atom WHERE state IN ('active','archived')
                   ORDER BY atom_id LIMIT ? OFFSET ?""", (step, done)).fetchall()
            if not rows:
                break
            # vectors_for 命中 sig 缓存则复用，缺失的按 batch 请求并落库后 commit
            _v, n = embed.vectors_for(conn, c, rows)
            embedded += n
            done += len(rows)
            _job_update(job_id, done=min(done, total),
                        message=f"已处理 {done}/{total} 条 · 新算向量 {embedded}")
        embed.invalidate(mdl)  # 让检索端的向量矩阵缓存失效，下次重建
        return {"processed": done, "embedded": embedded, "model": mdl}
    finally:
        conn.close()


if __name__ == "__main__":
    print(json.dumps(snapshot(), ensure_ascii=False, indent=1))
