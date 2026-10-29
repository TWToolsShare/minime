"""知识图谱抽取：从 atom + facet 生成 entity / relation。

不依赖 LLM，靠结构推断：
  - 每个 project / tech / tool facet 成为一个 entity
  - atom 与 entity 的关联写成 relation(atom_entity) 便于图谱渲染
  - 技术共现 -> 实体之间的关系（co_used，权重=共现次数）
  - 项目 -> 使用了哪些技术（uses）
"""
import itertools
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from retrieval import connect  # noqa: E402

ENTITY_TYPE = {"project": "project", "tech": "tech", "topic": "concept",
               "activity": "concept", "scene": "concept"}


def ensure_entities(conn):
    for r in conn.execute("""
        SELECT f.facet_id, f.dim, f.value FROM facet f
        WHERE f.dim IN ('project','tech','topic','scene','activity')"""):
        etype = ENTITY_TYPE[r["dim"]]
        eid = f"E:{r['dim']}:{r['value']}"
        conn.execute(
            "INSERT OR IGNORE INTO entity(entity_id,type,name,aliases) VALUES (?,?,?,?)",
            (eid, etype, r["value"], None))


def build_relations(conn):
    """重建关系表。

    写入 relation_new，最后用单条事务原子替换 relation：
    旧实现先 DELETE 再逐条插入 20 多万行且只在结尾提交，整个期间
    面板的 /api/graph 都拿不到读锁，表现为图谱打不开或长时间转圈。
    """
    conn.execute("DROP TABLE IF EXISTS relation_new")
    conn.execute("CREATE TABLE relation_new("
                 "src_id TEXT NOT NULL, dst_id TEXT NOT NULL, rel TEXT NOT NULL,"
                 "weight REAL NOT NULL DEFAULT 1.0, atom_id TEXT,"
                 "PRIMARY KEY(src_id, dst_id, rel))")
    # atom -> entity
    rows = conn.execute("""
        SELECT af.atom_id, f.dim, f.value FROM atom_facet af
        JOIN facet f ON f.facet_id=af.facet_id WHERE f.dim != 'topic'""").fetchall()
    conn.executemany(
        "INSERT OR IGNORE INTO relation_new(src_id,dst_id,rel,weight,atom_id)"
        " VALUES (?,?,?,?,?)",
        ((f"A:{r['atom_id']}", f"E:{r['dim']}:{r['value']}", "mentions", 1.0, r["atom_id"])
         for r in rows))
    # project uses tech（同一 atom 同时带 project 与 tech 标签）
    rows = conn.execute("""
        SELECT p.value AS project, t.value AS tech, COUNT(*) n
        FROM atom_facet ap JOIN facet p ON p.facet_id=ap.facet_id AND p.dim='project'
        JOIN atom_facet at ON at.atom_id=ap.atom_id
        JOIN facet t ON t.facet_id=at.facet_id AND t.dim='tech'
        GROUP BY 1,2""").fetchall()
    conn.executemany(
        "INSERT OR REPLACE INTO relation_new(src_id,dst_id,rel,weight,atom_id)"
        " VALUES (?,?,?,?,?)",
        ((f"E:project:{r['project']}", f"E:tech:{r['tech']}", "uses", float(r["n"]), None)
         for r in rows))
    # tech co_used
    rows = conn.execute("""
        SELECT a.value AS t1, b.value AS t2, COUNT(*) n
        FROM atom_facet fa JOIN facet a ON a.facet_id=fa.facet_id AND a.dim='tech'
        JOIN atom_facet fb ON fb.atom_id=fa.atom_id
        JOIN facet b ON b.facet_id=fb.facet_id AND b.dim='tech'
        WHERE a.value < b.value GROUP BY 1,2 HAVING n > 1""").fetchall()
    conn.executemany(
        "INSERT OR REPLACE INTO relation_new(src_id,dst_id,rel,weight,atom_id)"
        " VALUES (?,?,?,?,?)",
        ((f"E:tech:{r['t1']}", f"E:tech:{r['t2']}", "co_used", float(r["n"]), None)
         for r in rows))
    conn.commit()
    # 原子换表：面板要么看到旧图谱，要么看到新图谱，不会看到中间态
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM relation")
        conn.execute("INSERT INTO relation(src_id,dst_id,rel,weight,atom_id)"
                     " SELECT src_id,dst_id,rel,weight,atom_id FROM relation_new")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("DROP TABLE IF EXISTS relation_new")
        conn.commit()


REL_CN = {"uses": "使用", "co_used": "同时使用", "part_of": "属于",
          "depends_on": "依赖", "solves": "解决", "prefers_over": "优先于",
          "derived_from": "衍生自"}


def node_detail(conn, entity_id, atom_limit=8):
    """单个图谱节点：基本信息 + 邻居关系 + 挂在它下面的知识。"""
    ent = conn.execute("SELECT entity_id, type, name FROM entity WHERE entity_id=?",
                       (entity_id,)).fetchone()
    if not ent:
        return {"error": "节点不存在或已失效，请重新加载图谱"}
    parts = entity_id.split(":", 2)
    dim, value = (parts[1], parts[2]) if len(parts) == 3 else ("", ent["name"])

    atom_total = conn.execute("""
        SELECT COUNT(*) FROM facet f
        JOIN atom_facet af ON af.facet_id=f.facet_id
        JOIN atom a ON a.atom_id=af.atom_id
        WHERE f.dim=? AND f.value=? AND a.state='active'""", (dim, value)).fetchone()[0]
    atoms = [dict(r) for r in conn.execute("""
        SELECT a.atom_id, a.kind, a.title
        FROM facet f
        JOIN atom_facet af ON af.facet_id=f.facet_id
        JOIN atom a ON a.atom_id=af.atom_id
        WHERE f.dim=? AND f.value=? AND a.state='active'
        ORDER BY a.static_importance DESC, a.updated_at DESC LIMIT ?""",
        (dim, value, atom_limit))]

    rels = []
    for r in conn.execute("""
        SELECT r.rel, r.weight, r.src_id, r.dst_id,
               se.name AS src_name, se.type AS src_type,
               de.name AS dst_name, de.type AS dst_type
        FROM relation r
        JOIN entity se ON se.entity_id=r.src_id
        JOIN entity de ON de.entity_id=r.dst_id
        WHERE r.rel != 'mentions' AND (r.src_id=? OR r.dst_id=?)
        ORDER BY r.weight DESC LIMIT 40""", (entity_id, entity_id)):
        out = r["src_id"] == entity_id
        rels.append({
            "rel": r["rel"], "rel_cn": REL_CN.get(r["rel"], r["rel"]),
            "dir": "out" if out else "in", "weight": r["weight"],
            "node_id": r["dst_id"] if out else r["src_id"],
            "name": r["dst_name"] if out else r["src_name"],
            "type": r["dst_type"] if out else r["src_type"],
        })

    degree = conn.execute("SELECT COUNT(*) FROM relation"
                          " WHERE rel != 'mentions' AND (src_id=? OR dst_id=?)",
                          (entity_id, entity_id)).fetchone()[0]
    return {"node": {"entity_id": entity_id, "type": ent["type"], "name": ent["name"],
                     "degree": degree, "atoms": atom_total},
            "relations": rels, "atoms": atoms,
            "facet": {"dim": dim, "value": value}}


def graph_json(conn, limit_nodes=120, around=None):
    """给 Web 的力导向图准备节点/边。

    around 指定某个实体时，把它的直接邻居一并带上，保证从详情面板跳过去能定位到。
    """
    # 全新数据目录里 kb.db 还没建表，此时按空图谱返回，不要让面板首屏报 500
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table'"
                        " AND name='entity'").fetchone():
        return {"nodes": [], "edges": []}
    nodes = [dict(r) for r in conn.execute("""
        SELECT e.entity_id, e.type, e.name,
               (SELECT COUNT(*) FROM relation r WHERE r.dst_id=e.entity_id) AS degree
        FROM entity e ORDER BY degree DESC LIMIT ?""", (limit_nodes,))]
    keep = {n["entity_id"] for n in nodes}
    if around and around not in keep:
        extra = [r[0] for r in conn.execute(
            "SELECT dst_id FROM relation WHERE src_id=?"
            " UNION SELECT src_id FROM relation WHERE dst_id=?", (around, around))]
        missing = [e for e in extra if e not in keep]
        if around not in keep:
            missing.append(around)
        if missing:
            rows = conn.execute(f"""
                SELECT e.entity_id, e.type, e.name,
                       (SELECT COUNT(*) FROM relation r WHERE r.dst_id=e.entity_id) AS degree
                FROM entity e WHERE e.entity_id IN ({','.join('?' * len(missing))})""",
                missing)
            for r in rows:
                nodes.append(dict(r))
                keep.add(r["entity_id"])
    edges = [dict(r) for r in conn.execute(
        "SELECT src_id, dst_id, rel, weight FROM relation WHERE rel != 'mentions'")
        if r["src_id"] in keep and r["dst_id"] in keep]
    return {"nodes": nodes, "edges": edges}


def main():
    conn = connect()
    ensure_entities(conn)
    build_relations(conn)
    n = conn.execute("SELECT COUNT(*) FROM entity").fetchone()[0]
    e = conn.execute("SELECT COUNT(*) FROM relation").fetchone()[0]
    g = graph_json(conn)
    print(json.dumps({"entities": n, "relations": e,
                      "graph_nodes": len(g["nodes"]), "graph_edges": len(g["edges"])},
                     ensure_ascii=False))
    for r in conn.execute("""SELECT src_id,dst_id,weight FROM relation
                             WHERE rel IN ('uses','co_used') ORDER BY weight DESC LIMIT 8"""):
        print(f"  {r[0]} --{r[2]:.0f}--> {r[1]}")


if __name__ == "__main__":
    main()
