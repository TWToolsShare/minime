"""语义相关性闸门 + 语义分类校准（一次向量化，两个决策）。

1) 相关性：每条知识取 text_mode 指定的文本（标题+摘要 或 标题+正文）向量，
   和主题种子（项目/技术标签，或 embedding.seeds 手填）逐一算余弦取最大值；
   低于 min_score 的归档。归档是软删除：state='archived'，检索不再命中，随时可恢复：
     python pipeline/semantic_gate.py --restore
2) 分类校准：规则抽取只能把内容标成 fact，真正的「踩坑/决定/偏好」要靠词表命中。
   这里用一小撮原型句的向量做小样本分类（原型向量落 embedding_cache，只算一次），
   把与某个原型明显更近的 fact 改判成对应 kind，让 minime_troubleshoot /
   minime_preference 这类按 kind 过滤的工具能真正捞到东西。

开关：embedding.enabled（分类另受 embedding.reclassify 控制）。
未配置接口或不可达时整体 skipped，不影响清洗结果。
"""
import argparse
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
import embed  # noqa: E402
import paths  # noqa: E402
from normalize import fingerprint  # noqa: E402
from retrieval import connect  # noqa: E402

KB = paths.KB

DEFAULT_SOURCES = ("local_doc", "obsidian", "cloud_conversation", "snapshot_turn",
                   "workbuddy_session", "workbuddy_memory", "qoder_session",
                   "doubao_conversation")

# 分类校准的原型句：每类几句，取最大值作为该类得分。原型越贴近真实写法，校准越准。
KIND_PROTOTYPES = {
    "fact": ("客观背景信息，记录发生了什么、现状是什么、有哪些数据",
             "某个概念的客观说明与定义"),
    "troubleshooting": ("踩坑记录：报错的原因是什么，怎么修复",
                        "失败的现象、根因分析与解决办法",
                        "这个问题的原因是……解决办法是……"),
    "decision": ("技术选型结论：必须这样做，统一改为 X 而不是 Y",
                 "架构或流程上的决定，以及相应的约束和边界"),
    "preference": ("个人偏好：我更喜欢用这种方式，习惯这样做",
                   "不可妥协的个人约束与风格要求"),
}

# 只校正规则抽取留下的通用 fact，且必须明显优于 fact 原型才改判
RECLASSIFY_KINDS = ("troubleshooting", "decision", "preference")
MARGIN = 0.06


def healthy(c):
    """兼容旧调用点：embedding 是否可用。"""
    return embed.healthy(c)


def _seeds(conn, c):
    seeds = [str(s).strip() for s in (c.get("seeds") or []) if str(s).strip()]
    if seeds:
        return seeds
    rows = conn.execute("""
        SELECT f.value, COUNT(*) n FROM facet f
        JOIN atom_facet af ON af.facet_id=f.facet_id
        JOIN atom a ON a.atom_id=af.atom_id AND a.state='active'
        WHERE f.dim='project' AND f.value NOT IN ('unknown','')
        GROUP BY f.value ORDER BY n DESC LIMIT 40""").fetchall()
    seeds += [r[0] for r in rows]
    rows = conn.execute("""
        SELECT f.value, COUNT(*) n FROM facet f
        JOIN atom_facet af ON af.facet_id=f.facet_id
        JOIN atom a ON a.atom_id=af.atom_id AND a.state='active'
        WHERE f.dim='tech' GROUP BY f.value ORDER BY n DESC LIMIT 20""").fetchall()
    seeds += [r[0] for r in rows]
    rows = conn.execute("""
        SELECT f.value, COUNT(*) n FROM facet f
        JOIN atom_facet af ON af.facet_id=f.facet_id
        JOIN atom a ON a.atom_id=af.atom_id AND a.state='active'
        WHERE f.dim='topic' AND f.value NOT LIKE 'file:%'
        GROUP BY f.value ORDER BY n DESC LIMIT 10""").fetchall()
    seeds += [r[0] for r in rows]
    # 去重且保持顺序
    seen, uniq = set(), []
    for s in seeds:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


def _reclassify(conn, c, prepared, dry_run=False):
    """用原型向量校正 kind。返回统计；向量模型不可用时返回 skipped。"""
    if not c.get("reclassify", True):
        return {"skipped": "embedding.reclassify=false"}
    min_score = float(c.get("kind_min_score", 0.55))
    protos, failed = {}, 0
    for kind, texts in KIND_PROTOTYPES.items():
        vecs = []
        for i, text in enumerate(texts):
            v = embed.text_vector(conn, c, f"proto:{kind}:{i}", text)
            if v:
                vecs.append(v)
            else:
                failed += 1
        if vecs:
            protos[kind] = vecs
    if len(protos) < 2 or failed >= 3:
        return {"skipped": "原型向量不可用"}

    changed = {}
    for row, vec in prepared:
        if row["state"] != "active" or row["kind"] != "fact":
            continue
        if (row["trust_tier"] or 2) < 2:      # 用户明确表达的不要动
            continue
        scores = {k: max(embed.cosine(vec, pv) for pv in pvs) for k, pvs in protos.items()}
        best = max(scores, key=scores.get)
        if best not in RECLASSIFY_KINDS:
            continue
        if scores[best] < min_score or scores[best] - scores.get("fact", 0) < MARGIN:
            continue
        changed[best] = changed.get(best, 0) + 1
        if dry_run:
            continue
        try:
            conn.execute("UPDATE atom SET kind=?, fingerprint=?, updated_at=? WHERE atom_id=?",
                         (best, fingerprint(best, row["title"]), int(time.time()),
                          row["atom_id"]))
        except sqlite3.IntegrityError:
            changed[best] -= 1       # 同标题同 kind 已存在，交给去重流程
    return {"reclassified": sum(changed.values()), "by_kind": changed}


def run(conn=None, dry_run=False, limit=None, restore=False):
    """执行一次语义闸门 + 分类校准。返回统计；未启用/未配置时返回 skipped。"""
    conn = conn or connect()
    embed.ensure(conn)

    if restore:
        rows = [r[0] for r in conn.execute("SELECT atom_id FROM semantic_archive")]
        n = 0
        for aid in rows:
            cur = conn.execute(
                "UPDATE atom SET state='active' WHERE atom_id=? AND state='archived'", (aid,))
            n += cur.rowcount
        conn.execute("DELETE FROM semantic_archive")
        conn.commit()
        return {"restored": n}

    c = config.load().get("embedding") or {}
    if not c.get("enabled"):
        return {"skipped": "embedding.enabled=false"}
    if not embed.healthy(c):
        return {"skipped": "未配置 embedding.base_url / model"}

    seeds = _seeds(conn, c)
    if not seeds:
        return {"skipped": "没有主题种子：配置 embedding.seeds，或先积累项目/技术标签"}

    sources = tuple(str(s).strip() for s in (c.get("sources") or DEFAULT_SOURCES)
                    if str(s).strip()) or DEFAULT_SOURCES
    min_score = float(c.get("min_score", 0.28))
    ph = ",".join("?" * len(sources))
    # 向量化范围与归档范围分开：所有知识都算向量（否则只有关键词能命中），
    # 但只有 sources 指定的源参与「是否与工作主题相关」的归档判定。
    sql = f"""
        SELECT a.atom_id, a.kind, a.title, a.summary, a.body, a.state, a.trust_tier,
               EXISTS (SELECT 1 FROM atom_source s
                       WHERE s.atom_id=a.atom_id AND s.source_kind IN ({ph})) AS in_scope
        FROM atom a
        WHERE a.state IN ('active','archived')
        ORDER BY a.state='active' DESC, a.updated_at DESC"""
    args = list(sources)
    if limit:
        sql += " LIMIT ?"
        args.append(int(limit))
    rows = conn.execute(sql, args).fetchall()
    if not rows:
        return {"atoms": 0, "archived": 0, "restored": 0, "model": embed.model(c),
                "seeds": len(seeds), "min_score": min_score}

    # 知识向量：命中缓存复用，缺失的批量请求（与检索共用同一份 atom_embedding）
    vectors, embedded = embed.vectors_for(conn, c, rows)

    # 种子向量也要缓存：主题种子每轮都一样，没必要重复请求
    seed_vecs, seed_failed = [], 0
    for i, seed in enumerate(seeds):
        v = embed.text_vector(conn, c, f"seed:{seed[:80]}", seed)
        if v:
            seed_vecs.append(v)
        else:
            seed_failed += 1
    if not seed_vecs:
        return {"skipped": "种子向量不可用（接口不可达？）"}

    prepared = [(row, vectors[row["atom_id"]]) for row in rows
                if vectors.get(row["atom_id"])]

    archived = restored = 0
    score_min, score_max = 1.0, -1.0
    restorable = {r[0] for r in conn.execute("SELECT atom_id FROM semantic_archive")}
    # 一次性算完所有知识对主题种子的最大相似度（numpy 矩阵乘，避免 2000×69 次 Python 余弦）
    scores = embed.max_similarity([v for _, v in prepared], seed_vecs)
    for (row, vec), score in zip(prepared, scores):
        if not row["in_scope"]:
            continue          # 已向量化，但不参与主题归档判定
        score_min, score_max = min(score_min, score), max(score_max, score)
        aid = row["atom_id"]
        if score < min_score and row["state"] == "active":
            if not dry_run:
                conn.execute("UPDATE atom SET state='archived' WHERE atom_id=?", (aid,))
                conn.execute("INSERT OR REPLACE INTO semantic_archive"
                             "(atom_id,score,model,created_at) VALUES (?,?,?,?)",
                             (aid, score, embed.model(c), int(time.time())))
            archived += 1
        elif score >= min_score and row["state"] == "archived" and aid in restorable:
            if not dry_run:
                conn.execute("UPDATE atom SET state='active' WHERE atom_id=?", (aid,))
                conn.execute("DELETE FROM semantic_archive WHERE atom_id=?", (aid,))
            restored += 1
    conn.commit()

    kinds = _reclassify(conn, c, prepared, dry_run)

    stats = {"atoms": len(rows), "embedded": embedded, "archived": archived,
             "scoped": sum(1 for r in rows if r["in_scope"]),
             "restored": restored, "model": embed.model(c), "seeds": len(seeds),
             "seed_failed": seed_failed, "min_score": min_score,
             "text_mode": embed.text_mode(c), "dry_run": bool(dry_run),
             "score_min": round(score_min, 4), "score_max": round(score_max, 4),
             "kinds": kinds}
    if not dry_run and embedded:
        embed.invalidate(embed.model(c))
    try:
        import oplog  # noqa: PLC0415
        oplog.write("info", "semantic.gate", **stats)
    except Exception:  # noqa: BLE001
        pass
    return stats


def main():
    ap = argparse.ArgumentParser(description="语义相关性闸门 + 分类校准")
    ap.add_argument("--db", default=KB)
    ap.add_argument("--dry-run", action="store_true", help="只算分，不改状态")
    ap.add_argument("--restore", action="store_true", help="撤销归档")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    conn = sqlite3.connect(args.db, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    try:
        out = run(conn, dry_run=args.dry_run, limit=args.limit, restore=args.restore)
    except (OSError, KeyError, ValueError, RuntimeError) as e:
        out = {"error": f"{type(e).__name__}: {e}"}
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
