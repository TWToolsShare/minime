"""minime：个人分身 MCP 服务。

设计契约：
  - 任何「替我决策」的回答都必须带证据（atom_id + 来源），无证据时明确返回 insufficient
  - 所有调用都会写 usage_event，驱动学习型评分（retrieved / cited / accepted / rejected）
"""
import hashlib
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))
sys.path.insert(0, os.path.join(ROOT, "collector"))

from mcp.server.mcpserver import MCPServer  # noqa: E402

import paths  # noqa: E402
import retrieval as R  # noqa: E402

mcp = MCPServer("minime")


def _conn():
    return R.connect()


def _pack(hits, conn, with_facets=True):
    ids = [h["atom"]["atom_id"] for h in hits]
    facets = R.facets_of(conn, ids) if with_facets else {}
    out = []
    for h in hits:
        a = h["atom"]
        srcs = conn.execute(
            "SELECT source_kind, source_ref FROM atom_source WHERE atom_id=? LIMIT 3",
            (a["atom_id"],)).fetchall()
        out.append({
            "atom_id": a["atom_id"],
            "kind": a["kind"],
            "title": a["title"],
            "summary": a["summary"],
            "scope": a["scope"],
            "trust_tier": a["trust_tier"],
            "score": h["score"],
            "score_breakdown": h["breakdown"],
            "dormant": bool(h.get("dormant")),
            "facets": facets.get(a["atom_id"], []),
            "evidence": [f"{s['source_kind']}::{s['source_ref']}" for s in srcs],
        })
    return out


DECIDE_SYSTEM = """你是用户的个人分身，只能依据给定的知识证据回答。
先用证据判断「够不够回答这个问题」，再给结论。

输出严格 JSON，不要别的文字：
{"sufficient": true 或 false,
 "answer": "结论，<=120 字，每个结论后用 [1][2] 标注依据编号；sufficient=false 时写清楚缺什么证据",
 "used": [实际引用的编号, 例如 1,3],
 "verdict": "answerable 或 insufficient_evidence"}

规则：
- 只有证据讲的是同一对象、同一场景、同一动作，才算回答了问题
- 只是出现相同关键词、但说的是别的东西（别的项目 / 别的功能 / 别的系统），一律不算证据，不许拿来凑答案
- 证据只能支撑部分结论时，答已支撑的部分，并在 answer 里点明缺口
- 证据不足就判 sufficient=false，answer 只写「缺什么证据」，不给推测性结论
- answer 里至少引用一条证据编号；一条都引不到就说明证据不足
- 证据互相矛盾时指出冲突，说明哪条更强（可信度 / 更新时间）
- 编号必须是下面给出的，不许编造，不许引用不存在的内容

知识证据：
{context}
"""


def _cite_nums(text, n):
    """从回答里抽出 [n] 引用编号，越界的丢掉。"""
    out = []
    for x in re.findall(r"\[(\d{1,2})\]", text or ""):
        i = int(x)
        if 1 <= i <= n and i not in out:
            out.append(i)
    return out


def _parse_json(text):
    """模型有时会包一层 ```json 或加解释，尽量把第一个 JSON 对象捞出来。"""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


def _llm_decide(question, packed):
    """把「证据够不够 + 结论」交给模型判断。返回 dict；模型不可用返回 None。"""
    try:
        import llm_enrich as L  # noqa: PLC0415
        cfg = R.config.load()
        if not L.health(cfg).get("ok"):
            return None
        ctx = "\n\n".join(
            f"[{i}] ({p['kind']} / 可信 {p['trust_tier']} / 相关度 {p['score']:.2f}) "
            f"{p['title']}\n{(p['summary'] or '')[:400]}"
            for i, p in enumerate(packed, 1))
        reply = L.llm_chat(cfg, [
            {"role": "system", "content": DECIDE_SYSTEM.replace("{context}", ctx)},
            {"role": "user", "content": question}], timeout=120).strip()
        if not reply:
            return None
    except Exception:  # noqa: BLE001  模型不可用时由调用方退回规则路径
        return None

    data = _parse_json(reply)
    if not isinstance(data, dict):
        # 模型没按格式回：当成结论用，引用编号从正文里抽
        cited = _cite_nums(reply, len(packed))
        return {"sufficient": bool(cited), "answer": reply, "used": cited,
                "verdict": "answerable" if cited else "insufficient_evidence"}
    cited = data.get("used")
    if not isinstance(cited, list):
        cited = _cite_nums(data.get("answer") or "", len(packed))
    cited = sorted({int(x) for x in cited
                    if isinstance(x, (int, float)) and 1 <= int(x) <= len(packed)})
    verdict = str(data.get("verdict") or "").strip()
    if verdict not in ("answerable", "insufficient_evidence"):
        verdict = "answerable" if data.get("sufficient", True) else "insufficient_evidence"
    # 一条证据都指不出来，就不能算回答了（防止拿相似关键词硬凑）
    if verdict == "answerable" and not cited:
        verdict = "insufficient_evidence"
    return {"sufficient": verdict == "answerable",
            "answer": (data.get("answer") or "").strip() or "（模型未给出结论）",
            "used": cited, "verdict": verdict}


_DECIDE_CACHE = {}      # (模型+问题+证据指纹) -> (过期时间, 判断结果)
_DECIDE_MAX = 256


def _cached_llm_decide(cfg, question, packed):
    """同一问题 + 同一批证据在 TTL 内复用模型结论。

    MCP 服务常驻，重复提问很常见；缓存只存「模型判断结果」，
    检索与证据打包仍然每次重跑，命中不足时自然失效。
    返回 (judged, hit)。
    """
    ttl = int((cfg.get("answer") or {}).get("decide_cache_ttl_s", 600))
    if ttl <= 0:
        return _llm_decide(question, packed), False
    model = str((cfg.get("llm") or {}).get("model") or "")
    raw = "\0".join([model, question.strip(), "|".join(p["atom_id"] for p in packed)])
    key = hashlib.sha1(raw.encode("utf-8")).hexdigest()
    now = time.time()
    hit = _DECIDE_CACHE.get(key)
    if hit and hit[0] > now:
        return dict(hit[1]), True
    judged = _llm_decide(question, packed)
    if judged is not None:
        if len(_DECIDE_CACHE) >= _DECIDE_MAX:
            _DECIDE_CACHE.clear()
        _DECIDE_CACHE[key] = (now + ttl, dict(judged))
    return judged, False


def _decide(conn, question, scope=None, filter_ast=None, top_k=5):
    cfg = R.config.load()
    mode = R.config.answer_mode(cfg)
    min_score = float((cfg.get("answer") or {}).get("decide_min_score", 0.55))
    # 规则模式：检索只走关键词，结论靠固定阈值；模型模式：检索叠加向量，结论交给模型判断
    hits = R.search(conn, question, filter_ast, limit=top_k, include_events=False,
                    log_usage=True, include_dormant=True,
                    use_vectors=(mode == "llm"))
    packed = _pack(hits, conn)
    kinds = {p["kind"] for p in packed}
    strong = [p for p in packed if p["score"] >= min_score and p["trust_tier"] <= 2]

    judged, used_ids, unknown_reason = None, [], None
    cached = False
    if mode == "llm" and packed:
        judged, cached = _cached_llm_decide(cfg, question, packed)

    if judged is not None:
        verdict = judged["verdict"]
        answer = judged["answer"]
        source = "llm"
        used_ids = [packed[i - 1]["atom_id"] for i in judged["used"]]
        picked = [packed[i - 1] for i in judged["used"]] or strong
        confidence = (sum(p["score"] for p in picked[:3]) / min(3, len(picked))
                      if picked else 0.0)
        if not judged["sufficient"]:
            unknown_reason = answer
    elif strong:
        # 规则路径（answer.mode=rules，或模型不可达时的兜底）
        verdict = "answerable"
        lead = strong[0]
        answer = lead["summary"] or lead["title"]
        confidence = sum(p["score"] for p in strong[:3]) / min(3, len(strong))
        source = "rules" if mode == "rules" else "rules_fallback"
        used_ids = [p["atom_id"] for p in strong[:3]]
    else:
        verdict = "insufficient_evidence"
        answer = ("知识库中没有足够依据回答该问题。补充相关项目或技术的对话记录后重试。"
                  if mode == "rules" else
                  "知识库中没有足够依据回答该问题；模型也未在现有证据中找到答案。")
        confidence = min([p["score"] for p in packed], default=0.0)
        source = "rules" if mode == "rules" else "rules_fallback"

    # 引用即反馈：把真正被用到的知识记一次 cited，反馈闭包不依赖调用方主动上报。
    # 缓存命中不重复记，避免同一问题多问几次就把权重刷高。
    if used_ids and not cached:
        R.record_feedback_many(conn, used_ids, "cited", question[:200], surface="mcp")

    counter = [p["title"] for p in packed if p["kind"] == "troubleshooting"][:3]
    unknowns = []
    if unknown_reason:
        unknowns.append(unknown_reason)
    if scope and not any(scope in (p["scope"] or "") for p in packed):
        unknowns.append(f"缺少 {scope} 的直接证据，仅用通用知识推断")
    if len(packed) < 3:
        unknowns.append("可用证据条数偏少，结论可能片面")
    return {
        "verdict": verdict,
        "answer": answer,
        "confidence": round(min(confidence, 1.0), 3),
        "cited": used_ids,
        "confidence_breakdown": {
            "answer_source": source,
            "mode": mode,
            "cached": cached,
            "min_score": min_score,
            "kinds_found": sorted(kinds),
            "strong_hits": len(strong),
            "total_hits": len(packed),
        },
        "evidence": packed,
        "counterpoints": counter,
        "unknowns": unknowns,
        "policy": "只依据用户已沉淀的知识作答；无证据时明确返回 insufficient_evidence",
    }


# ------------------------------------------------------------------ tools
@mcp.tool()
def minime_decide(question: str, scope: str = "", project: str = "") -> str:
    """基于知识库证据回答问题，返回判断、证据与不确定性；证据不足时返回 insufficient_evidence。
    question: 要判断的问题。scope/project: 限定作用域（如 tech:docker）。"""
    conn = _conn()
    flt = {"facet": {"dim": "project", "eq": project}} if project else None
    return json.dumps(_decide(conn, question, scope or project or None, flt),
                      ensure_ascii=False, indent=1)


@mcp.tool()
def minime_chat(question: str, session_id: str = "") -> str:
    """基于知识库的多轮问答。session_id 为空时新建会话；传入上次返回的 session_id 可继续该会话。"""
    import chat as C  # noqa: PLC0415
    try:
        sid = session_id or C.new_session(question[:40])
        r = C.ask(sid, question)
    except RuntimeError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    return json.dumps({"session_id": sid, **r}, ensure_ascii=False, indent=1)


@mcp.tool()
def minime_recall(query: str, kinds: str = "", project: str = "",
                  tech: str = "", limit: int = 8, cited_ids: str = "") -> str:
    """检索知识。kinds: 逗号分隔 decision|preference|fact|troubleshooting|pattern|event|insight。
    cited_ids: 你实际用到的 item 的 atom_id（逗号分隔），会记为「被引用」提升其排序权重。"""
    conn = _conn()
    klist = [k.strip() for k in kinds.split(",") if k.strip()] or None
    conds = []
    if project:
        conds.append({"facet": {"dim": "project", "eq": project}})
    if tech:
        conds.append({"facet": {"dim": "tech", "in": [t.strip() for t in tech.split(",")]}})
    flt = None
    if len(conds) == 1:
        flt = conds[0]
    elif conds:
        flt = {"all": conds}
    hits = R.search(conn, query, flt, kinds=klist, limit=min(limit, 30),
                    log_usage=True, include_dormant=True)
    cited = [x.strip() for x in cited_ids.split(",") if x.strip()]
    fb = R.record_feedback_many(conn, cited, "cited", query[:200], surface="mcp") \
        if cited else {"recorded": 0}
    return json.dumps({"query": query, "count": len(hits), "cited": fb["recorded"],
                       "items": _pack(hits, conn)},
                      ensure_ascii=False, indent=1)


@mcp.tool()
def minime_preference(topic: str = "", project: str = "") -> str:
    """查询用户在某个主题/项目上的偏好与硬约束。"""
    conn = _conn()
    flt = {"all": [{"facet": {"dim": "project", "eq": project}}]} if project else None
    hits = R.search(conn, topic or None, flt, kinds=["preference", "decision"],
                    limit=15, log_usage=True)
    return json.dumps({"items": _pack(hits, conn)}, ensure_ascii=False, indent=1)


@mcp.tool()
def minime_project_context(project: str) -> str:
    """获取项目背景：项目约定、技术栈、历史问题与近期工作。"""
    conn = _conn()
    flt = {"facet": {"dim": "project", "eq": project}}
    hits = R.search(conn, None, flt, limit=20, log_usage=True)
    techs = [r[0] for r in conn.execute(
        "SELECT DISTINCT f.value FROM atom_facet af JOIN facet f ON f.facet_id=af.facet_id"
        " JOIN atom_facet ap ON ap.atom_id=af.atom_id JOIN facet p ON p.facet_id=ap.facet_id"
        " WHERE p.dim='project' AND p.value=? AND f.dim='tech'", (project,))]
    groups = {}
    for p in _pack(hits, conn):
        groups.setdefault(p["kind"], []).append(p)
    return json.dumps({"project": project, "tech": techs,
                       "by_kind": {k: v for k, v in groups.items()},
                       "totals": {k: len(v) for k, v in groups.items()}},
                      ensure_ascii=False, indent=1)


@mcp.tool()
def minime_troubleshoot(symptom: str, project: str = "") -> str:
    """按症状检索历史踩坑与排障经验。"""
    conn = _conn()
    flt = {"facet": {"dim": "project", "eq": project}} if project else None
    hits = R.search(conn, symptom, flt, kinds=["troubleshooting"], limit=10,
                    log_usage=True, include_dormant=True)
    return json.dumps({"symptom": symptom, "items": _pack(hits, conn)},
                      ensure_ascii=False, indent=1)


@mcp.tool()
def minime_remember(title: str, body: str, kind: str = "fact",
                    scope: str = "", project: str = "", tech: str = "") -> str:
    """写入一条新知识。kind: decision|preference|fact|troubleshooting|pattern|insight。
    project/tech: 逗号分隔，会自动建标签。"""
    import time
    from normalize import HALF_LIFE, Writer, detect_scene, detect_tech  # noqa: PLC0415
    conn = _conn()
    w = Writer(conn)
    facets = []
    if project:
        facets += [w.put_facet("project", p.strip()) for p in project.split(",") if p.strip()]
    for t in [x.strip() for x in tech.split(",") if x.strip()] + detect_tech(body):
        facets.append(w.put_facet("tech", t))
    facets += [w.put_facet("scene", s) for s in detect_scene(body)]
    aid = w.add_atom(kind=kind, title=title, body=body, summary=body[:160],
                     scope=scope or (f"project:{project.split(',')[0]}" if project else None),
                     confidence=0.95, veracity=0.9, importance=0.7, trust_tier=1,
                     source_kind="manual", source_ref=f"mcp@{int(time.time())}",
                     excerpt=body[:400], facets=facets)
    conn.commit()
    return json.dumps({"atom_id": aid, "kind": kind,
                       "half_life_days": HALF_LIFE.get(kind, 90)},
                      ensure_ascii=False)


@mcp.tool()
def minime_feedback(atom_id: str, event: str, detail: str = "") -> str:
    """对某条知识反馈：cited|accepted|rejected|corrected|verified。
    反馈用于调整检索权重：accepted/cited/verified 加分并恢复已降权知识，rejected 减分；
    连续多次 rejected 只降权（state=dormant），正常命中不足时仍会召回，不会删除。"""
    conn = _conn()
    R.record_feedback(conn, atom_id, event, detail, surface="mcp")
    row = conn.execute("SELECT title,veracity FROM atom WHERE atom_id=?", (atom_id,)).fetchone()
    return json.dumps({"ok": True, "atom_id": atom_id, "event": event,
                       "veracity": row["veracity"] if row else None},
                      ensure_ascii=False)


@mcp.tool()
def minime_tag(atom_id: str, dim: str, value: str) -> str:
    """给知识打标签。dim: scene|project|topic|tech|activity|period|quality 或自定义维度。"""
    conn = _conn()
    w_fid = f"{dim}:{value.strip().lower().replace(' ', '-')}"
    conn.execute("INSERT OR IGNORE INTO facet(facet_id,dim,value) VALUES (?,?,?)",
                 (w_fid, dim, value.strip()))
    conn.execute("INSERT OR REPLACE INTO atom_facet(atom_id,facet_id,weight,confidence,origin)"
                 " VALUES (?,?,?,?,?)", (atom_id, w_fid, 1.0, 1.0, "user"))
    conn.commit()
    return json.dumps({"ok": True, "atom_id": atom_id, "facet": w_fid}, ensure_ascii=False)


@mcp.tool()
def minime_filter(filter_json: str, query: str = "", limit: int = 20) -> str:
    """按多维标签筛选知识。filter_json 示例：
    {"all":[{"facet":{"dim":"project","eq":"weave/spark"}},
            {"facet":{"dim":"tech","in":["llm","typescript"]}}]}"""
    conn = _conn()
    try:
        flt = json.loads(filter_json) if filter_json.strip() else None
    except ValueError as e:
        return json.dumps({"error": f"invalid filter json: {e}"}, ensure_ascii=False)
    try:
        hits = R.search(conn, query or None, flt, limit=min(limit, 50), log_usage=True)
    except R.FilterError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    return json.dumps({"count": len(hits), "items": _pack(hits, conn)},
                      ensure_ascii=False, indent=1)


@mcp.tool()
def minime_profile() -> str:
    """查看能力画像（多维评分 + 证据数 + 知识空白）。"""
    conn = _conn()
    row = conn.execute("SELECT * FROM profile_snapshot ORDER BY created_at DESC LIMIT 1").fetchone()
    if not row:
        from profile import build  # noqa: PLC0415
        _sid, payload = build(conn)
        return json.dumps(payload, ensure_ascii=False, indent=1)
    payload = json.loads(row["payload"])
    return json.dumps({"generated_at": payload["generated_at"],
                       "dimensions": payload["dimensions"],
                       "projects": payload["projects"],
                       "techs": payload["techs"],
                       "coverage_gap": payload.get("coverage_gap")},
                      ensure_ascii=False, indent=1)


@mcp.tool()
def minime_blindspots(limit: int = 10) -> str:
    """盲区：知识库中证据薄弱的维度（项目/技术/知识类型）。"""
    conn = _conn()
    out = {"low_evidence_dims": [], "thin_projects": [], "kind_gaps": []}
    for dim in ("project", "tech"):
        rows = conn.execute("""
            SELECT f.value, COUNT(DISTINCT af.atom_id) n FROM facet f
            LEFT JOIN atom_facet af ON af.facet_id=f.facet_id
            WHERE f.dim=? GROUP BY 1 ORDER BY n ASC LIMIT ?""", (dim, limit)).fetchall()
        out["low_evidence_dims"] += [
            {"dim": dim, "value": r["value"], "atoms": r["n"]} for r in rows if r["n"] < 3]
    kinds_all = {"decision", "preference", "fact", "troubleshooting", "pattern", "insight"}
    have = {r[0] for r in conn.execute("SELECT DISTINCT kind FROM atom")}
    out["kind_gaps"] = sorted(kinds_all - have)
    out["note"] = ("盲区仅反映本地证据覆盖情况，不代表能力缺失；"
                   "建议补充这些维度的对话记录，或手动写入知识")
    return json.dumps(out, ensure_ascii=False, indent=1)


@mcp.tool()
def minime_config(action: str = "get", section: str = "", values: str = "") -> str:
    """查看/修改 minime 配置（LLM 归纳、记忆同步开关）。
    action: get | set | llm_health
      get  -> 返回当前配置
      set  -> section="llm"|"memory_sync", values='{"enabled":true}'
      llm_health -> 探测本地模型是否可用
    """
    import config as C  # noqa: PLC0415
    if action == "get":
        cfg = C.load()
        cfg["llm"]["api_key"] = "***" if cfg["llm"].get("api_key") else ""
        return json.dumps(cfg, ensure_ascii=False, indent=1)
    if action == "llm_health":
        import llm_enrich as L  # noqa: PLC0415
        return json.dumps(L.health(C.load()), ensure_ascii=False)
    if action == "set":
        if section not in ("llm", "memory_sync", "collect"):
            return json.dumps({"error": f"unknown section: {section}"}, ensure_ascii=False)
        try:
            patch = json.loads(values or "{}")
        except ValueError as e:
            return json.dumps({"error": f"invalid values json: {e}"}, ensure_ascii=False)
        C.save({section: patch})
        return json.dumps({"ok": True, "section": section, "applied": patch},
                          ensure_ascii=False)
    return json.dumps({"error": f"unknown action: {action}"}, ensure_ascii=False)


@mcp.tool()
def minime_sync(dry_run: bool = True) -> str:
    """把 minime 的高质量知识同步到 mnemosyne 记忆库（需先开启 memory_sync.enabled）。
    dry_run=true 时只列出候选，不实际写入。"""
    import memory_sync as MS  # noqa: PLC0415
    return json.dumps(MS.run(dry_run=dry_run), ensure_ascii=False, indent=1)


@mcp.tool()
def minime_workspaces(keyword: str = "", min_sessions: int = 1) -> str:
    """列出全部工作区及其会话/历史规模，用于定位「哪个工作区有多少历史对话」。"""
    import os as _os  # noqa: PLC0415
    import sqlite3 as _sq  # noqa: PLC0415
    idx = paths.INDEX
    if not _os.path.exists(idx):
        return json.dumps({"error": "index.db 不存在，请先运行 collector/index_all.py"},
                          ensure_ascii=False)
    conn = _sq.connect(idx)
    conn.row_factory = _sq.Row
    sql = """SELECT w.name, w.path, w.kind, w.sessions,
                    COUNT(s.session_id) AS indexed_sessions,
                    SUM(CASE WHEN s.has_snapshot=1 THEN 1 ELSE 0 END) AS with_snapshot
             FROM workspace w LEFT JOIN session s ON s.workspace_id=w.workspace_id
             WHERE w.name LIKE ? GROUP BY w.workspace_id
             HAVING indexed_sessions >= ? ORDER BY indexed_sessions DESC"""
    rows = [dict(r) for r in conn.execute(sql, (f"%{keyword}%", min_sessions))]
    total = conn.execute("SELECT COUNT(*) FROM session").fetchone()[0]
    conn.close()
    return json.dumps({"total_sessions_indexed": total, "workspaces": rows},
                      ensure_ascii=False, indent=1)


@mcp.tool()
def minime_collect(adapter: str = "", force: bool = False) -> str:
    """采集器状态 / 手动采集。
    adapter="" 返回全部适配器的健康度与进度；force=true 时立即执行采集，未指定 adapter 则采集全部适配器。"""
    import collect as C  # noqa: PLC0415
    conn = C.init_db()
    ac = C.AutoCollector(conn, {adapter} if adapter else None)
    if not force:
        return json.dumps(ac.status(), ensure_ascii=False, indent=1)
    return json.dumps({"results": ac.tick(force=True)}, ensure_ascii=False, indent=1)


@mcp.tool()
def minime_pack(action: str = "inspect", path: str = "", name: str = "my-worker",
                prefix: str = "", dry_run: bool = True) -> str:
    """资源包：导出或导入个人知识与决策能力。
    action: inspect | export | import
      inspect -> path 指向 .minime.zip，返回清单（不落库）
      export  -> 导出到 store/packs/<name>.minime.zip（默认脱敏，不含原文）
      import  -> 导入 path；dry_run=true 只预览；prefix 建议填写以隔离他人知识
    """
    import pack as P  # noqa: PLC0415
    import os as _os  # noqa: PLC0415
    try:
        if action == "export":
            out = _os.path.join(paths.PACKS, f"{name}.minime.zip")
            _os.makedirs(_os.path.dirname(out), exist_ok=True)
            return json.dumps(P.export(name=name, out=out), ensure_ascii=False, indent=1)
        if not path:
            return json.dumps({"error": "需要 path 参数"}, ensure_ascii=False)
        full = path if _os.path.isabs(path) else _os.path.join(paths.PACKS, path)
        if action == "import":
            return json.dumps(P.import_pack(full, dry_run=dry_run, prefix=prefix),
                              ensure_ascii=False, indent=1)
        return json.dumps(P.inspect(full), ensure_ascii=False, indent=1)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)


@mcp.tool()
def minime_sync_status() -> str:
    """查看知识库与记忆库同步状态（已同步条数、按类型分布、可用 bank）。"""
    import memory_sync as MS  # noqa: PLC0415
    return json.dumps(MS.stats(R.connect()), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    # 默认 stdio（Trae / Claude Code / Cursor / Codex 通用）；
    # --http 时用 streamable-http 暴露，供局域网内其他机器/其他 AI 共享同一份记忆
    if "--http" in sys.argv:
        import argparse  # noqa: PLC0415
        p = argparse.ArgumentParser()
        p.add_argument("--http", action="store_true")
        p.add_argument("--host", default="127.0.0.1")
        p.add_argument("--port", type=int, default=8765)
        a = p.parse_args()
        mcp.run(transport="streamable-http", host=a.host, port=a.port)
    else:
        mcp.run()
