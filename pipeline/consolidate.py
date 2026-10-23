"""知识整理：过期归档 + 近似重复取代，作为清洗后的定期压缩机制。

三类动作：
  1. valid_until 已到期 -> archived
  2. 长期无反馈的低价值活动事件（kind=event）-> archived（可配置天数，0=不处理）
  3. 同类型、同作用域、内容高度相近的条目 -> 保留一条，其余 superseded

判重是两条路并联：
  规则路：标题二元组 Jaccard >= similarity，且正文 Jaccard >= body_similarity
  语义路：text_mode 表征的向量余弦 >= vector_similarity（换词、换语序也能认出来）
只改 state，不删除数据；被取代的条目会记录 supersedes 指向保留的那条。
手动执行：
  python pipeline/consolidate.py --dry-run
  python pipeline/consolidate.py
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
import embed  # noqa: E402
import paths  # noqa: E402
import schema  # noqa: E402
from retrieval import connect  # noqa: E402

ROOT = paths.CODE_ROOT
KB = paths.KB
MANIFEST = paths.MANIFEST
RAW = paths.RAW


def _norm(title):
    """去掉标点/空白并小写，用于近似比较。"""
    keep = []
    for ch in (title or "").lower():
        if ch.isalnum() or "\u4e00" <= ch <= "\u9fff":
            keep.append(ch)
    return "".join(keep)


def _has_cjk(text):
    return any("\u4e00" <= ch <= "\u9fff" for ch in (text or ""))


# 标题尾部「（来源路径）」：路径是溯源标识，不是语义，去掉后才好比较
_PATH_SUFFIX = re.compile(r"[（(]([^（()）]*)[）)]\s*$")


def _key(title):
    """参与相似比较的标题：去掉尾部纯路径来源标注后归一化。

    像「触发条件（.trae_skills_xxx_skill）」与「触发条件（other_path）」会因此可比，
    而「触发条件」与「严重度分级」不会因为共用一条路径而被误判为重复。
    """
    title = (title or "").strip()
    m = _PATH_SUFFIX.search(title)
    if m:
        inner = m.group(1)
        if not _has_cjk(inner) and re.search(r"[_/.\d]", inner):
            title = title[:m.start()]
    return _norm(title)


def _bigrams(text):
    return {text[i:i + 2] for i in range(len(text) - 1)} if len(text) > 1 else {text}


def _pairs(atoms, similarity):
    """用二元组倒排找候选，再算 Jaccard；返回相似到可以合并的 (i, j)。"""
    bags = [_bigrams(a["norm"]) for a in atoms]
    index = defaultdict(set)
    for i, bag in enumerate(bags):
        for bg in bag:
            index[bg].add(i)
    out = []
    for i, bag in enumerate(bags):
        if len(bag) < 3:
            continue
        shared = Counter()
        for bg in bag:
            for j in index[bg]:
                if j > i:
                    shared[j] += 1
        for j, n in shared.items():
            if n < 2:
                continue
            union = len(bag | bags[j])
            if union and n / union >= similarity:
                out.append((i, j))
    return out


def _body_similarity(a, b):
    """正文的二元组 Jaccard 相似度。"""
    ba, bb = _bigrams(_norm(a)), _bigrams(_norm(b))
    if not ba or not bb:
        return 0.0
    return len(ba & bb) / len(ba | bb)


def _vec_pairs(conn, c, items, threshold):
    """同一 kind 桶内按向量余弦找候选对，返回 [(i, j, cos)]。

    向量来自 atom_embedding（清洗阶段的语义闸门已经算过），这里不产生任何网络请求。
    分块做矩阵乘，避免 1 万条同 kind 时一次性铺开 800MB 的相似度矩阵。
    """
    ids, mat = embed.load_matrix(conn, c)
    if mat is None or not ids:
        return []
    np = embed._numpy()
    pos = {aid: i for i, aid in enumerate(ids)}
    rows = [pos.get(it["atom_id"]) for it in items]
    keep = [k for k, r in enumerate(rows) if r is not None]
    if len(keep) < 2:
        return []
    mat_idx = [rows[k] for k in keep]
    out = []
    block = 1024
    for start in range(0, len(keep), block):
        idx = mat_idx[start:start + block]
        sims = mat[idx] @ mat[mat_idx].T
        # 只在 numpy 侧筛一遍阈值，Python 循环次数等于命中对数，不是 n²
        ai, bj = np.where(sims >= threshold)
        for a, b in zip(ai.tolist(), bj.tolist()):
            i = keep[start + a]
            j = keep[b]
            if j <= i:
                continue
            out.append((i, j, float(sims[a][b])))
    return out


class _DSU:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def _winner_key(atom, feedback):
    """trust_tier 越小越好；再看正向反馈、可靠度、最近更新时间。"""
    return (atom["trust_tier"] or 3, -feedback.get(atom["atom_id"], 0),
            -(atom["veracity"] or 0), -(atom["updated_at"] or 0),
            -len(atom["body"] or ""))


def _trim_raw(keep, dry_run=False):
    """每个（来源类型, 来源路径）只保留最近 keep 个原始归档版本，返回清理条数。

    原始归档是内容寻址的：同一文件每改一次就多存一份，不清理会无限增长。
    知识正文已落在 kb.db，删掉旧版本不影响检索；只影响「从 raw 完整重建」时
    能回溯到的历史深度。
    """
    if keep <= 0 or not os.path.exists(MANIFEST):
        return 0
    m = sqlite3.connect(MANIFEST, timeout=15)
    m.row_factory = sqlite3.Row
    try:
        rows = m.execute(
            "SELECT sha256, kind, source, path FROM raw_object"
            " ORDER BY created_at DESC, rowid DESC").fetchall()
        seen, drop = defaultdict(int), []
        for r in rows:
            key = (r["kind"], r["source"])
            seen[key] += 1
            if seen[key] > keep:
                drop.append(r)
        if dry_run:
            return len(drop)
        for r in drop:
            m.execute("DELETE FROM raw_object WHERE sha256=?", (r["sha256"],))
            try:
                os.remove(os.path.join(RAW, r["path"]))
            except OSError:
                pass
        m.commit()
        return len(drop)
    finally:
        m.close()


def run(conn=None, dry_run=False, similarity=None, event_retention_days=None):
    cfg = config.load().get("consolidate") or {}
    if similarity is None:
        similarity = float(cfg.get("similarity", 0.88))
    body_similarity = float(cfg.get("body_similarity", 0.85))
    vector_similarity = float(cfg.get("vector_similarity", 0.86))
    if event_retention_days is None:
        event_retention_days = int(cfg.get("event_retention_days", 365))
    enabled = bool(cfg.get("enabled", True))
    if not enabled and not dry_run:
        return {"skipped": "consolidate.enabled=false"}

    conn = conn or connect()
    schema.migrate(conn)
    now = int(time.time())

    # 1) 到期归档
    expired = 0
    rows = conn.execute(
        "SELECT atom_id FROM atom WHERE state='active' AND valid_until IS NOT NULL"
        " AND valid_until<=?", (now,)).fetchall()
    expired = len(rows)
    if expired and not dry_run:
        conn.execute("UPDATE atom SET state='archived' WHERE state='active'"
                     " AND valid_until IS NOT NULL AND valid_until<=?", (now,))

    # 2) 低价值活动事件的保留期
    retired_events = 0
    if event_retention_days > 0:
        cutoff = now - event_retention_days * 86400
        rows = conn.execute("""
            SELECT atom_id FROM atom a
            WHERE a.state='active' AND a.kind='event' AND COALESCE(a.static_importance,0.5)<0.4
              AND COALESCE(a.updated_at,a.created_at,0)<?
              AND NOT EXISTS (SELECT 1 FROM usage_event u WHERE u.atom_id=a.atom_id
                              AND u.event IN ('accepted','cited','verified'))""",
            (cutoff,)).fetchall()
        retired_events = len(rows)
        if retired_events and not dry_run:
            ids = [r[0] for r in rows]
            ph = ",".join("?" * len(ids))
            conn.execute(f"UPDATE atom SET state='archived' WHERE atom_id IN ({ph})", ids)

    # 2.5) 反馈日志保留期：检索/引用每次调用都会写一行，不清理会无限增长
    usage_days = int((config.load().get("feedback") or {}).get("usage_retention_days", 180))
    usage_trimmed = 0
    if usage_days > 0:
        cutoff = now - usage_days * 86400
        if dry_run:
            usage_trimmed = conn.execute(
                "SELECT COUNT(*) FROM usage_event WHERE created_at<?", (cutoff,)).fetchone()[0]
        else:
            usage_trimmed = conn.execute(
                "DELETE FROM usage_event WHERE created_at<?", (cutoff,)).rowcount

    # 2.6) 原始归档：每个来源只留最近几个版本
    raw_trimmed = _trim_raw(int(cfg.get("raw_keep_versions", 3)), dry_run)

    # 3) 近似重复取代
    feedback = {r[0]: r[1] for r in conn.execute("""
        SELECT atom_id, SUM(CASE WHEN event IN ('accepted','cited','verified') THEN 1 ELSE 0 END)
        FROM usage_event GROUP BY atom_id""")}
    rows = conn.execute("""
        SELECT atom_id, kind, title, body, scope, trust_tier, veracity, updated_at
        FROM atom WHERE state='active'""").fetchall()
    by_kind = defaultdict(list)
    for r in rows:
        # 事件是时间线记录，标题相似不代表可互相取代，交给上面的保留期处理
        if r["kind"] == "event":
            continue
        # 纯英文/路径标题是唯一标识（如 store/snippets/xxx.ts），不是语义标题
        if not _has_cjk(r["title"]):
            continue
        key = _key(r["title"])
        if len(key) < 4:
            continue
        by_kind[r["kind"]].append({**dict(r), "norm": key})

    superseded = 0
    clusters = 0
    vec_cfg = config.load().get("embedding") or {}
    use_vectors = embed.healthy(vec_cfg)
    vec_hits = 0
    for kind, items in by_kind.items():
        if len(items) < 2:
            continue
        dsu = _DSU(len(items))
        # 规则路：标题二元组近似，再用正文 Jaccard 复核，避免「同名不同内容」被压缩
        for i, j in _pairs(items, similarity):
            if (items[i]["scope"] or "global") != (items[j]["scope"] or "global"):
                continue
            if _body_similarity(items[i]["body"], items[j]["body"]) < body_similarity:
                continue
            dsu.union(i, j)
        # 语义路：换词/换语序的同一结论也能认出来。正文仍要复核——向量看重「主题」，
        # 同一篇文档的不同小节（关键实体 / 关键概念）很像，但合并会丢内容。
        if use_vectors:
            for i, j, cos in _vec_pairs(conn, vec_cfg, items, vector_similarity):
                if (items[i]["scope"] or "global") != (items[j]["scope"] or "global"):
                    continue
                if _body_similarity(items[i]["body"], items[j]["body"]) < body_similarity:
                    continue
                vec_hits += 1
                dsu.union(i, j)
        groups = defaultdict(list)
        for i in range(len(items)):
            groups[dsu.find(i)].append(i)
        for idxs in groups.values():
            if len(idxs) < 2:
                continue
            clusters += 1
            winner = min((items[i] for i in idxs), key=lambda a: _winner_key(a, feedback))
            for i in idxs:
                loser = items[i]
                if loser["atom_id"] == winner["atom_id"]:
                    continue
                superseded += 1
                if dry_run:
                    continue
                conn.execute(
                    "UPDATE atom SET state='superseded', supersedes=?, valid_until=?"
                    " WHERE atom_id=? AND state='active'",
                    (winner["atom_id"], now, loser["atom_id"]))
                conn.execute(
                    "INSERT OR REPLACE INTO supersede_log"
                    "(loser_id,winner_id,similarity,created_at) VALUES (?,?,?,?)",
                    (loser["atom_id"], winner["atom_id"], similarity, now))

    if not dry_run:
        conn.commit()
    stats = {"expired": expired, "retired_events": retired_events,
             "usage_trimmed": usage_trimmed, "raw_trimmed": raw_trimmed,
             "clusters": clusters, "superseded": superseded,
             "similarity": similarity, "body_similarity": body_similarity,
             "vector_similarity": vector_similarity, "vector_pairs": vec_hits,
             "vectors": use_vectors, "dry_run": bool(dry_run)}
    try:
        import oplog  # noqa: PLC0415
        oplog.write("info", "consolidate.run", **stats)
    except Exception:  # noqa: BLE001
        pass
    return stats


def main():
    ap = argparse.ArgumentParser(description="知识整理：过期归档 + 近似重复取代")
    ap.add_argument("--db", default=KB)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--similarity", type=float, default=None)
    ap.add_argument("--event-retention-days", type=int, default=None)
    args = ap.parse_args()
    conn = sqlite3.connect(args.db, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    out = run(conn, dry_run=args.dry_run, similarity=args.similarity,
              event_retention_days=args.event_retention_days)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
