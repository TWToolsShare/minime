"""检索与评分引擎（Web 与 MCP 共用同一套）。

评分 = Σ w_i * s_i，权重可在 config 覆盖：
  fts 0.30 / importance 0.20 / confidence 0.15 / recency 0.15 / feedback 0.10 / trust 0.10
反馈分来自 usage_event 的指数衰减累加 + 日级加分上限，保证「越用越好用」又不会被刷爆。

向量检索（embedding.enabled，默认开）：查询向量与 atom_embedding 缓存算余弦，
并入「关键词/语义」这一项（语义分 (1+cos)/2 映射到 0~1）；余弦低于
embedding.min_score × 0.8 的命中丢弃（防噪声，语义闸门用同一阈值判定无关）。
全量向量走 embed.load_matrix 的 numpy 矩阵（磁盘缓存 + 矩阵乘），
没有 numpy 时才退回逐条余弦；接口不可达时整体静默降级为纯 FTS。
"""
import json
import math
import os
import re
import sqlite3
import time
import uuid

from normalize import bigrams

import config
import embed
import packs
import paths
import schema

KB = paths.KB

WEIGHTS = {
    "fts": 0.30, "importance": 0.20, "confidence": 0.15,
    "recency": 0.15, "feedback": 0.10, "trust": 0.10,
}
RECENCY_HALFLIFE_DAYS = 60
FEEDBACK_HALFLIFE_DAYS = 45
EVENT_DELTA = {
    "retrieved": 0.0, "cited": 0.06, "accepted": 0.12,
    "verified": 0.20, "corrected": -0.10, "rejected": -0.25,
}
DAILY_POSITIVE_CAP = 0.25

FACET_DIMS = ("scene", "project", "topic", "tech", "activity", "period", "quality")


def connect():
    conn = sqlite3.connect(KB, timeout=15)
    conn.row_factory = sqlite3.Row
    # 后台采集/归纳可能正持写锁，这里等一会儿而不是直接报 locked
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


_GATE_READY = False


def _ensure_gate(conn):
    """老库可能缺表：检索前跑一次迁移（每个进程只做一次）。"""
    global _GATE_READY
    if _GATE_READY:
        return
    try:
        schema.migrate(conn)
        _GATE_READY = True
    except sqlite3.Error:
        pass


# ---------------------------------------------------------------- 标签筛选 AST
class FilterError(ValueError):
    pass


def compile_filter(node) -> tuple[str, list]:
    """把 {"any"/"all"/"not"/"facet"} 树编译成 SQL 条件。

    {"facet": {"dim": "tech", "in": ["docker","git"]}}
    {"facet": {"dim": "project", "eq": "weave/spark"}}
    {"any": [ ... ]} / {"all": [ ... ]} / {"not": {...}}
    """
    if not isinstance(node, dict) or len(node) != 1:
        raise FilterError("filter node must have exactly one key")
    key, val = next(iter(node.items()))
    if key == "any":
        parts, args = [], []
        for n in val:
            s, a = compile_filter(n)
            parts.append(s)
            args += a
        if not parts:
            return "0", []
        return "(" + " OR ".join(parts) + ")", args
    if key == "all":
        parts, args = [], []
        for n in val:
            s, a = compile_filter(n)
            parts.append(s)
            args += a
        return "(" + " AND ".join(parts) + ")" if parts else "1", args
    if key == "not":
        s, a = compile_filter(val)
        return f"NOT ({s})", a
    if key == "facet":
        dim = val.get("dim")
        if dim not in FACET_DIMS:
            raise FilterError(f"未知标签维度：{dim}；可用维度：{', '.join(FACET_DIMS)}")
        field = "eq" if "eq" in val else "in"
        values = val.get("in") or []
        if "eq" in val:
            values = [val["eq"]]
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise FilterError("facet values must be list of strings")
        if not values:
            return "0", []
        ph = ",".join("?" * len(values))
        # 用 IN 子查询而不是相关 EXISTS：SQLite 会给 IN 建临时 bloom 过滤，
        # 从 facet 侧倒查命中的 atom，再与主查询求交，避免每个 atom 都做一次子查询。
        cond = (f"a.atom_id IN (SELECT af.atom_id FROM atom_facet af"
                f" JOIN facet f ON f.facet_id=af.facet_id"
                f" WHERE f.dim=? AND f.value IN ({ph}))")
        return cond, [dim, *values]
    if key == "sensitivity":
        values = val.get("in") or ([val["eq"]] if "eq" in val else [])
        if not values:
            return "0", []
        ph = ",".join("?" * len(values))
        return f"COALESCE(a.sensitivity,'internal') IN ({ph})", list(values)
    if key == "collection":
        values = val.get("in") or ([val["eq"]] if "eq" in val else [])
        if not values:
            return "0", []
        ph = ",".join("?" * len(values))
        return (f"a.atom_id IN (SELECT pa2.atom_id FROM pack_atom pa2"
                f" WHERE pa2.collection_id IN ({ph}))", list(values))
    raise FilterError(f"unknown node: {key}")


# ---------------------------------------------------------------- 分数
def recency_score(ts, halflife_days=RECENCY_HALFLIFE_DAYS):
    if not ts:
        return 0.2
    age_days = max(0.0, (time.time() - ts) / 86400)
    return 0.5 ** (age_days / halflife_days)


def feedback_scores(conn):
    now = time.time()
    out = {}
    daily = {}
    for r in conn.execute(
            "SELECT atom_id,event,delta,created_at FROM usage_event ORDER BY created_at"):
        d = EVENT_DELTA.get(r["event"], 0.0) + (r["delta"] or 0.0)
        if d > 0:
            day = time.strftime("%Y-%m-%d", time.localtime(r["created_at"]))
            key = (r["atom_id"], day)
            used = daily.get(key, 0.0)
            d = min(d, max(0.0, DAILY_POSITIVE_CAP - used))
            daily[key] = used + d
        age = max(0.0, (now - r["created_at"]) / 86400)
        out[r["atom_id"]] = out.get(r["atom_id"], 0.0) + d * (
            0.5 ** (age / FEEDBACK_HALFLIFE_DAYS))
    return {k: max(-0.5, min(0.5, v)) for k, v in out.items()}


def score_row(row, fts_norm, fb, now=None, rel=1.0):
    now = now or time.time()
    iw = row["static_importance"] or 0.5
    cf = (row["confidence"] or 0.5) * 0.5 + (row["veracity"] or 0.5) * 0.5
    rc = recency_score(row["valid_from"] or row["created_at"])
    trusted = {1: 1.0, 2: 0.7, 3: 0.4}.get(row["trust_tier"] or 2, 0.5)
    parts = {
        "fts": WEIGHTS["fts"] * fts_norm * rel,
        "importance": WEIGHTS["importance"] * iw,
        "confidence": WEIGHTS["confidence"] * cf,
        "recency": WEIGHTS["recency"] * rc,
        "feedback": WEIGHTS["feedback"] * (0.5 + fb),
        "trust": WEIGHTS["trust"] * trusted,
    }
    return sum(parts.values()), parts


# ---------------------------------------------------------------- 向量检索
_RRF_K = 60        # 融合系数：语义分按 (1+cos)/2 映射后与关键词分同尺度相加


def _embedding_cfg():
    """embedding 可用时返回配置，否则 None（开关关掉 / 没填地址模型）。"""
    cfg = config.load().get("embedding") or {}
    return cfg if embed.healthy(cfg) else None


def _vec_candidates(conn, cfg, qvec, floor):
    """全量余弦召回，返回 [(atom_id, cos)]，已按相似度降序。

    有 numpy 且矩阵缓存命中时是矩阵乘（10 万条也是毫秒级）；
    否则退回逐条 base64 解码 + 点积，保证没有 numpy 的环境也能跑。
    """
    ids, mat = embed.load_matrix(conn, cfg)
    if not ids:
        return []
    np = embed._numpy() if mat is not None else None
    if np is not None and mat.shape[1] == len(qvec):
        q = np.asarray(qvec, dtype="float32")
        q = q / (np.linalg.norm(q) + 1e-9)
        sims = mat @ q
        take = np.where(sims >= floor)[0]
        if take.size == 0:
            return []
        take = take[np.argsort(-sims[take])][:400]
        return [(ids[int(i)], float(sims[int(i)])) for i in take]

    qn = embed.norm(qvec)
    sims = []
    for r in conn.execute("SELECT atom_id, vec FROM atom_embedding WHERE model=?",
                          (embed.model(cfg),)):
        try:
            v = embed.decode(r["vec"])
        except (ValueError, TypeError):
            continue
        if len(v) != len(qvec):
            continue
        s = sum(x * y for x, y in zip(v, qvec)) / (embed.norm(v) * qn)
        if s >= floor:
            sims.append((r["atom_id"], s))
    sims.sort(key=lambda x: -x[1])
    return sims[:400]


# ---------------------------------------------------------------- 主检索
def _search_state(conn, state, query=None, filter_ast=None, kinds=None, limit=10,
                  include_events=True, log_usage=False, count_only=False,
                  respect_packs=True, use_vectors=True):
    """按单一状态检索（active / dormant / superseded / archived）。"""
    where = ["a.state=?",
             # 语义闸门归档的知识即使因重建暂时回到 active，也不该被检索到
             "NOT EXISTS (SELECT 1 FROM semantic_archive sa WHERE sa.atom_id=a.atom_id)"]
    args = [state]
    if respect_packs:
        cond, cargs = packs.visible_sql(conn, "a")
        if cond:
            where.append(cond)
            args += cargs
    if kinds:
        where.append(f"a.kind IN ({','.join('?' * len(kinds))})")
        args += list(kinds)
    elif not include_events:
        where.append("a.kind != 'event'")
    if filter_ast:
        cond, cargs = compile_filter(filter_ast)
        where.append(cond)
        args += cargs

    fts_norm = {}
    vec_norm = {}
    sim_of = {}
    fts_ids = []
    if query:
        tokens = [t for t in bigrams(query).split() if t]
        match = " ".join(tokens)
        rows = []
        if tokens:
            # 先 AND（精确），不足再 OR（宽松），再退化为 LIKE
            for expr in (" AND ".join(tokens), " OR ".join(tokens)):
                try:
                    rows = conn.execute(
                        "SELECT a.atom_id AS atom_id, bm25(atom_fts) AS rank "
                        "FROM atom_fts JOIN atom a ON a.rowid = atom_fts.rowid "
                        "WHERE atom_fts MATCH ? LIMIT 400", (expr,)).fetchall()
                except sqlite3.OperationalError:
                    rows = []
                if rows:
                    break
        if rows:
            ranks = {r["atom_id"]: r["rank"] for r in rows}
            lo, hi = min(ranks.values()), max(ranks.values())
            span = (hi - lo) or 1.0
            fts_norm = {k: 1.0 - (v - lo) / span for k, v in ranks.items()}
            fts_ids = list(ranks)
            # 低于 1/4 相关度的命中不参与召回，避免 OR 回退把无关知识带进来
            if len(tokens) > 1:
                fts_ids = [k for k in fts_ids if fts_norm[k] >= 0.25]
                fts_norm = {k: fts_norm[k] for k in fts_ids}

        # 向量侧：查询向量与已缓存的知识向量算余弦，语义相近但关键词没对上的也能进来
        cfg = _embedding_cfg() if use_vectors else None
        if cfg:
            qvec = embed.embed_query(query, cfg)
            if qvec:
                floor = max(0.0, float(cfg.get("min_score", 0.28)) * 0.8)
                sims = _vec_candidates(conn, cfg, qvec, floor)
                if sims:
                    sim_of = dict(sims)
                    lo, hi = sims[-1][1], sims[0][1]
                    span = (hi - lo) or 1.0
                    vec_norm = {aid: (s - lo) / span for aid, s in sims}

        hit_ids = fts_ids + [a for a in vec_norm if a not in fts_norm]
        if hit_ids:
            where.append(f"a.atom_id IN ({','.join('?' * len(hit_ids))})")
            args += hit_ids
        else:
            like = f"%{query.strip()}%"
            where.append("(a.title LIKE ? OR a.body LIKE ?)")
            args += [like, like]

    sql = f"SELECT a.* FROM atom a WHERE {' AND '.join(where)}"
    rows = conn.execute(sql, args).fetchall()
    if count_only:
        return len(rows)
    fb = feedback_scores(conn)
    scored = []
    # 融合：关键词相关度与语义相似度同尺度相加（语义分 (1+cos)/2 映射到 0~1），
    # 只在语义一路命中的条目，前半部分按 _RRF_K 系数折价，避免纯靠模糊相似挤掉精确命中
    hybrid = bool(vec_norm)
    for r in rows:
        aid = r["atom_id"]
        f = fts_norm.get(aid, 0.0)
        v = vec_norm.get(aid, 0.0)
        rel = _RRF_K / (_RRF_K + 1) if hybrid and not f else 1.0
        s, parts = score_row(r, f, fb.get(aid, 0.0), rel=rel)
        if hybrid:
            parts["fts"] = round(WEIGHTS["fts"] * (f + (1.0 + v) / 2.0) * rel, 4)
            s = round(sum(parts.values()), 6)
        if aid in sim_of:
            parts["vec_sim"] = round(sim_of[aid], 4)
        scored.append((s, parts, r))
    scored.sort(key=lambda x: -x[0])
    top = scored[:limit]
    origin = packs.pack_of_atoms(conn, [r["atom_id"] for _s, _p, r in top])
    if log_usage and top:
        now = int(time.time())
        conn.executemany(
            "INSERT INTO usage_event(atom_id,event,surface,query,delta,created_at)"
            " VALUES (?,?,?,?,?,?)",
            [(r["atom_id"], "retrieved", "mcp", (query or "")[:200], 0, now)
             for _s, _p, r in top])
        conn.commit()
    return [{"score": round(s, 4), "breakdown": {k: round(v, 4) for k, v in p.items()},
             "packs": origin.get(r["atom_id"], []),
             "atom": dict(r)} for s, p, r in top]


def search(conn, query=None, filter_ast=None, kinds=None, limit=10,
           include_events=True, log_usage=False, count_only=False,
           respect_packs=True, state="active", include_dormant=False,
           use_vectors=True):
    """检索入口。count_only=True 时只返回命中总数（前端分页用），不做评分。

    respect_packs=True 时只看「已开启知识集」里的知识（资源包开关）。
    include_dormant=True 时，正常知识命中不足才把「降权」知识按惩罚分补进来：
    冷门知识不会因为两次误标就再也召不回。
    use_vectors=False 时本次检索不用向量（回答模式「关键词规则」下强制关闭）。
    """
    _ensure_gate(conn)
    hits = _search_state(conn, state, query, filter_ast, kinds, limit,
                         include_events, log_usage, count_only, respect_packs,
                         use_vectors)
    if (not include_dormant or state != "active" or count_only or not query
            or len(hits) >= limit):
        return hits
    fcfg = config.load().get("feedback") or {}
    min_hits = int(fcfg.get("dormant_fallback_min") or 0)
    if min_hits <= 0 or len(hits) >= min(limit, min_hits):
        return hits
    extra = _search_state(conn, "dormant", query, filter_ast, kinds, limit,
                          include_events, log_usage, False, respect_packs,
                          use_vectors)
    penalty = float(fcfg.get("dormant_penalty") or 0.6)
    for h in extra:
        h["score"] = round(h["score"] * penalty, 4)
        h["dormant"] = True
    return sorted(hits + extra, key=lambda x: -x["score"])[:limit]


def facets_of(conn, atom_ids):
    out = {aid: [] for aid in atom_ids}
    ph = ",".join("?" * len(atom_ids)) if atom_ids else "''"
    for r in conn.execute(
            f"SELECT af.atom_id, f.dim, f.value, f.facet_id FROM atom_facet af "
            f"JOIN facet f ON f.facet_id=af.facet_id WHERE af.atom_id IN ({ph})",
            list(atom_ids)):
        out[r["atom_id"]].append({"dim": r["dim"], "value": r["value"]})
    return out


def record_feedback(conn, atom_id, event, detail=None, surface="mcp"):
    if event not in EVENT_DELTA:
        raise ValueError(f"unknown event: {event}")
    fcfg = config.load().get("feedback") or {}
    now = int(time.time())
    conn.execute(
        "INSERT INTO usage_event(atom_id,event,surface,query,delta,created_at)"
        " VALUES (?,?,?,?,?,?)",
        (atom_id, event, surface, detail, 0, now))
    if event in ("rejected", "corrected"):
        conn.execute("UPDATE atom SET veracity=MAX(0.05, veracity-0.08) WHERE atom_id=?",
                     (atom_id,))
    elif event in ("accepted", "verified", "cited"):
        conn.execute("UPDATE atom SET veracity=MIN(0.99, veracity+0.05) WHERE atom_id=?",
                     (atom_id,))
    demoted = restored = False
    streak, threshold = 0, int(fcfg.get("dormant_after_rejects", 2))
    if event in ("accepted", "cited", "verified"):
        # 降权可逆：任何一次正向使用都让它回到检索池，不需要人工干预
        cur = conn.execute(
            "UPDATE atom SET state='active' WHERE atom_id=? AND state='dormant'",
            (atom_id,))
        if cur.rowcount:
            conn.execute("DELETE FROM atom_demotion WHERE atom_id=?", (atom_id,))
            restored = True
    if event == "rejected":
        # 连续多次「不相关」（中间没有采用/引用/验证）时降权，而不是归档：
        # 降权后仍可在正常命中不足时被召回，也能人工恢复，避免冷门知识被判死刑。
        if threshold > 0:
            last_good = conn.execute(
                "SELECT COALESCE(MAX(id),0) FROM usage_event"
                " WHERE atom_id=? AND event IN ('accepted','cited','verified','corrected')",
                (atom_id,)).fetchone()[0]
            bad = conn.execute(
                "SELECT COUNT(*) FROM usage_event WHERE atom_id=? AND event='rejected'"
                " AND id>?", (atom_id, last_good)).fetchone()[0]
            streak = bad
            if bad >= threshold:
                cur = conn.execute(
                    "UPDATE atom SET state='dormant' WHERE atom_id=? AND state='active'",
                    (atom_id,))
                if cur.rowcount:
                    conn.execute(
                        "INSERT OR REPLACE INTO atom_demotion"
                        "(atom_id,reason,reject_streak,created_at) VALUES (?,?,?,?)",
                        (atom_id, "rejected", bad, now))
                    demoted = True
    conn.commit()
    return {"demoted": demoted, "restored": restored,
            "reject_streak": streak, "threshold": threshold}


def record_feedback_many(conn, atom_ids, event, detail=None, surface="mcp"):
    """批量记反馈（一次回答引用了多条知识时用），语义与单条版一致。

    只处理库里真实存在的 atom，避免调用方传了脏 id 把 usage_event 写花。
    """
    ids = [a for a in dict.fromkeys(atom_ids or []) if a]
    if not ids:
        return {"recorded": 0, "demoted": 0, "restored": 0}
    ph = ",".join("?" * len(ids))
    alive = [r[0] for r in conn.execute(
        f"SELECT atom_id FROM atom WHERE atom_id IN ({ph})", ids)]
    out = {"recorded": 0, "demoted": 0, "restored": 0}
    for aid in alive:
        r = record_feedback(conn, aid, event, detail, surface)
        out["recorded"] += 1
        out["demoted"] += int(bool(r.get("demoted")))
        out["restored"] += int(bool(r.get("restored")))
    return out


def restore_atom(conn, atom_id):
    """把降权/归档的知识恢复为 active（人工纠错入口）。"""
    cur = conn.execute(
        "UPDATE atom SET state='active', valid_until=NULL"
        " WHERE atom_id=? AND state IN ('dormant','archived')", (atom_id,))
    conn.execute("DELETE FROM atom_demotion WHERE atom_id=?", (atom_id,))
    conn.commit()
    return {"restored": cur.rowcount > 0}


def facet_tree(conn, min_count=1):
    """给 Web 的筛选面板用：dim -> [{value, count}]"""
    out = {}
    for r in conn.execute("""
        SELECT f.dim, f.value, COUNT(DISTINCT af.atom_id) n
        FROM facet f JOIN atom_facet af ON af.facet_id=f.facet_id
        GROUP BY f.dim, f.value HAVING n>=? ORDER BY n DESC""", (min_count,)):
        out.setdefault(r["dim"], []).append({"value": r["value"], "count": r["n"]})
    return out


if __name__ == "__main__":
    c = connect()
    for q, flt in [("docker 镜像", None),
                   (None, {"facet": {"dim": "project", "eq": "weave/spark"}}),
                   ("兼容", {"all": [{"facet": {"dim": "tech", "in": ["macos"]}},
                                    {"not": {"facet": {"dim": "tech", "eq": "windows"}}}]})]:
        res = search(c, q, flt, limit=3)
        print(f"\n== query={q!r} filter={flt}")
        for x in res:
            print(f"  {x['score']:.3f} [{x['atom']['kind']}] {x['atom']['title'][:70]}")
    print("\nfacets:", json.dumps({k: len(v) for k, v in facet_tree(c).items()},
                                  ensure_ascii=False))
