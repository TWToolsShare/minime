"""能力画像：从知识与行为证据生成多维画像。

原则（用户认可的口径）：
  - independence 基于**行为证据**：用户主动提出方案/纠正 AI/独立决策，AI 代劳要扣分
  - 每个维度都必须给 evidence 数，证据不足的维度标 insufficient，不出分
"""
import json
import os
import sqlite3
import sys
import time
import uuid
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402
from retrieval import connect  # noqa: E402

MANIFEST = paths.MANIFEST

CORRECTION_MARKERS = ("不对", "错了", "不是这样", "重新", "改回", "你理解错",
                      "我要的是", "应该", "别", "不要", "撤回")
OWN_PROPOSAL_MARKERS = ("我打算", "我的想法", "我认为", "我建议", "方案 A", "我倾向",
                        "我觉得", "按我的", "设计上")


def pct(n, d):
    return round(100.0 * n / d, 1) if d else 0.0


def sat(n, mid):
    """饱和映射：n == mid 时约 50 分，避免大样本一律顶到 100。"""
    if n <= 0:
        return 0.0
    return round(100.0 * n / (n + mid), 1)


def collect(conn):
    atoms = conn.execute("SELECT * FROM atom WHERE state='active'").fetchall()
    tdb = sqlite3.connect(MANIFEST)
    tdb.row_factory = sqlite3.Row
    turns = tdb.execute("SELECT session_id,turn_id,ts,n_files,n_added,n_modified,"
                        "additions,deletions FROM turn").fetchall()
    turn_files = tdb.execute("SELECT session_id,turn_id,path FROM turn_file").fetchall()
    cloud = []
    for r in tdb.execute("SELECT source,path FROM raw_object WHERE kind='cloud_messages'"):
        full = os.path.join(paths.RAW, r["path"])
        try:
            data = json.load(open(full, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        cloud += data.get("data", {}).get("items", [])
    tdb.close()
    return atoms, turns, turn_files, cloud


def user_texts(cloud):
    out = []
    for it in cloud:
        if it.get("role") != "user":
            continue
        try:
            blocks = json.loads(it.get("content") or "[]")
        except ValueError:
            continue
        txt = " ".join(b.get("text_content", "") for b in blocks
                       if isinstance(b, dict) and b.get("type") == "text")
        if txt.strip():
            out.append(txt.strip())
    return out


def build(conn):
    atoms, turns, turn_files, cloud = collect(conn)
    now = int(time.time())

    # ---- 证据池
    projects = Counter()
    techs = Counter()
    kinds = Counter(a["kind"] for a in atoms)
    for r in conn.execute("""SELECT p.value, COUNT(DISTINCT af.atom_id) n
                             FROM atom_facet af JOIN facet p ON p.facet_id=af.facet_id
                             WHERE p.dim='project' GROUP BY 1"""):
        projects[r["value"]] = r["n"]
    for r in conn.execute("""SELECT t.value, COUNT(DISTINCT af.atom_id) n
                             FROM atom_facet af JOIN facet t ON t.facet_id=af.facet_id
                             WHERE t.dim='tech' GROUP BY 1"""):
        techs[r["value"]] = r["n"]

    ts_list = [t["ts"] for t in turns if t["ts"]]
    active_days = len({time.strftime("%Y-%m-%d", time.localtime(t)) for t in ts_list})
    n_turns = len(turns)
    files_touched = len({f["path"] for f in turn_files})
    sessions = len({t["session_id"] for t in turns})
    lines = sum((t["additions"] or 0) + (t["deletions"] or 0) for t in turns)

    # ---- 代码/交付侧的领域广度
    domains = Counter()
    for f in turn_files:
        p = f["path"].lower()
        if any(k in p for k in (".py", ".ts", ".js", ".java", ".go", ".rs")):
            domains["代码"] += 1
        elif any(k in p for k in (".md", ".txt", "docs/")):
            domains["文档"] += 1
        elif any(k in p for k in (".yml", ".yaml", ".json", ".toml", ".env")):
            domains["配置"] += 1
        elif any(k in p for k in (".sql", "schema", "migration")):
            domains["数据模型"] += 1
        elif any(k in p for k in ("docker", "deploy", ".sh", "k8s")):
            domains["部署/运维"] += 1
        elif any(k in p for k in (".css", ".scss", ".tsx", ".vue", ".html")):
            domains["界面"] += 1

    # ---- independence：用户主导 vs AI 代劳
    texts = user_texts(cloud)
    corrections = sum(1 for t in texts if any(m in t for m in CORRECTION_MARKERS))
    proposals = sum(1 for t in texts if any(m in t for m in OWN_PROPOSAL_MARKERS))
    # 已归档的偏好/决策（用户明确表达、trust_tier=1）说明用户在定方向
    user_decisions = sum(1 for a in atoms
                         if a["trust_tier"] == 1 and a["kind"] in ("preference", "decision"))
    # 行为证据：用户主动写下的约束/教训
    user_rules = sum(1 for a in atoms
                     if a["trust_tier"] == 1 and a["kind"] in ("preference", "troubleshooting"))
    indep_evidence = corrections + proposals + user_decisions + user_rules
    # AI 代劳度：AI 改动文件数 / 有记录的用户表达数（仅在有用户文本时可算）
    ai_delegation = round(files_touched / max(1, len(texts)), 2) if texts else None

    dims = []

    def add(dim, score, evidence, detail, sufficient=True):
        dims.append({"dimension": dim, "score": round(score, 1) if sufficient else None,
                     "evidence": evidence,
                     "detail": detail if sufficient else f"证据不足：{detail}"})

    add("tech_breadth", sat(len(techs), 8) * 0.8 + sat(len(domains), 4) * 0.2,
        len(techs), f"覆盖 {len(techs)} 类技术：{', '.join(k for k, _ in techs.most_common(8))}")
    real_projects = [p for p in projects if p not in ("unknown",)]
    proj_names = [k for k, _ in projects.most_common() if k != "unknown"][:6]
    add("project_span", sat(len(real_projects), 6), len(real_projects),
        f"涉及 {len(real_projects)} 个项目：{', '.join(proj_names)}")
    add("delivery_volume", sat(n_turns, 150) * 0.5 + sat(files_touched, 250) * 0.3
        + sat(active_days, 30) * 0.2,
        n_turns + files_touched,
        f"{sessions} 个会话、{n_turns} 轮有实质改动、{files_touched} 个文件、{lines} 行变更、"
        f"{active_days} 个活跃日")
    add("architecture_depth", sat(kinds.get("pattern", 0) * 2 + kinds.get("decision", 0) * 3, 25),
        kinds.get("pattern", 0) + kinds.get("decision", 0),
        f"沉淀 {kinds.get('pattern',0)} 条工程模式、{kinds.get('decision',0)} 条决策")
    dbg = kinds.get("troubleshooting", 0)
    add("debugging", sat(dbg, 20), dbg, f"沉淀 {dbg} 条排障经验")
    total_dom = sum(domains.values())
    dom_score = sat(len(domains), 4) * 0.5 + pct(max(domains.values()), total_dom or 1) * 0.5
    add("domain_focus", dom_score, total_dom,
        "改动文件领域分布：" + ", ".join(f"{k}{v}" for k, v in domains.most_common()))
    if texts:
        add("independence", sat(indep_evidence, 40) * 0.7 + sat(corrections, 6) * 0.3,
            indep_evidence,
            f"用户文本 {len(texts)} 条；主动提方案 {proposals}、纠正 AI {corrections}、"
            f"明确约束/偏好 {user_decisions + user_rules}；AI 代劳比（改文件/用户表达）{ai_delegation}")
    else:
        add("independence", 0, 0, "尚无云端会话文本，只有本地轨迹", sufficient=False)
    add("knowledge_asset", sat(len(atoms), 200), len(atoms),
        f"已沉淀 {len(atoms)} 条知识原子（"
        + ", ".join(f"{k}{v}" for k, v in kinds.most_common()) + "）")

    snap_id = "P" + uuid.uuid4().hex[:12]
    payload = {
        "generated_at": now,
        "window": {"from": min(ts_list) if ts_list else None, "to": max(ts_list) if ts_list else now},
        "sessions": sessions, "turns": n_turns, "files": files_touched,
        "lines_changed": lines, "active_days": active_days,
        "atoms": len(atoms), "kinds": dict(kinds),
        "projects": dict(projects.most_common(20)),
        "techs": dict(techs.most_common(20)),
        "domains": dict(domains.most_common()),
        "independence": {"user_texts": len(texts), "proposals": proposals,
                         "corrections": corrections,
                         "explicit_rules": user_decisions + user_rules,
                         "ai_delegation_ratio": ai_delegation},
        "dimensions": dims,
        "coverage_gap": [t for t in ("java", "sql", "frontend", "docker")
                         if t not in techs],
    }
    conn.execute("INSERT INTO profile_snapshot VALUES (?,?,?,?,?)",
                 (snap_id, now, payload["window"]["from"], payload["window"]["to"],
                  json.dumps(payload, ensure_ascii=False)))
    for d in dims:
        conn.execute("INSERT OR REPLACE INTO profile_dimension VALUES (?,?,?,?,?)",
                     (snap_id, d["dimension"], d["score"], d["evidence"], d["detail"]))
    conn.commit()
    return snap_id, payload


def render_markdown(payload, path):
    lines = ["# 能力画像", "",
             f"生成时间：{time.strftime('%Y-%m-%d %H:%M', time.localtime(payload['generated_at']))}",
             f"数据窗口：{time.strftime('%Y-%m-%d', time.localtime(payload['window']['from'] or 0))}"
             f" ~ {time.strftime('%Y-%m-%d', time.localtime(payload['window']['to']))}",
             "", "| 维度 | 分数 | 证据数 | 说明 |", "|---|---|---|---|"]
    for d in payload["dimensions"]:
        s = "—" if d["score"] is None else f"{d['score']:.0f}"
        lines.append(f"| {d['dimension']} | {s} | {d['evidence']} | {d['detail']} |")
    lines += ["", "## 主要项目", ""]
    lines += [f"- {k}: {v} 条知识" for k, v in payload["projects"].items()]
    lines += ["", "## 技术栈", ""]
    lines += [f"- {k}: {v}" for k, v in payload["techs"].items()]
    if payload["coverage_gap"]:
        lines += ["", "## 知识空白（本地无证据）", ""]
        lines += [f"- {t}" for t in payload["coverage_gap"]]
    open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")


def main():
    conn = connect()
    snap_id, payload = build(conn)
    out = os.path.join(paths.STORE, "profile.md")
    render_markdown(payload, out)
    print(json.dumps({"snap_id": snap_id, "dims": [
        {"d": d["dimension"], "score": d["score"], "ev": d["evidence"]}
        for d in payload["dimensions"]]}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
