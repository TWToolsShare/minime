"""资源包（pack）与知识集（collection）：本地多套并存、按集开关、结构化属性导出。

模型（两级）：
  资源包 pack         一组知识的容器，带作者/来源/创建时间
    └ 知识集 collection 包内可分多套（如「架构决策」「排障经验」），开关粒度到这里
        └ 成员 atom     通过 pack_atom 关联；一条知识可以同时在多个包里

关键约束：
  - 开关是「知识集」级；检索只看已开启的集（`visible_sql()` 生成过滤条件）
  - 只有 owner='self' 的包可被编辑/导出；导入的包 owner='imported'，只读可用
  - 同一个指纹在多包中重复时保留多份（各自独立的 atom 行），检索结果标注来源包
"""
import json
import os
import sqlite3
import time

import paths

KB = paths.KB

DEFAULT_PACK_ID = "P:self"
DEFAULT_COLLECTION_ID = "C:self:all"

SCHEMA = """
-- ---------- 资源包 ----------
CREATE TABLE IF NOT EXISTS pack (
  pack_id     TEXT PRIMARY KEY,     -- P:self / P:<slug>
  name        TEXT NOT NULL,
  description TEXT,
  author      TEXT,
  owner       TEXT DEFAULT 'self',  -- self=自己创建、可导出 | imported=他人包、只读
  origin      TEXT,                 -- 导入来源（文件名或包校验和）
  version     TEXT DEFAULT '1.0.0',
  created_at  INTEGER,
  updated_at  INTEGER
);
CREATE TABLE IF NOT EXISTS pack_collection (
  collection_id TEXT PRIMARY KEY,
  pack_id       TEXT NOT NULL,
  name          TEXT NOT NULL,
  description   TEXT,
  enabled       INTEGER DEFAULT 0,
  n_atoms       INTEGER DEFAULT 0,
  order_no      INTEGER DEFAULT 0,
  created_at    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_coll_pack ON pack_collection(pack_id);
-- 成员关系：主键带上 collection_id，同一条知识可在多个集里且各自独立开关
CREATE TABLE IF NOT EXISTS pack_atom (
  collection_id TEXT NOT NULL,
  atom_id       TEXT NOT NULL,
  added_at      INTEGER,
  PRIMARY KEY (collection_id, atom_id)
);
CREATE INDEX IF NOT EXISTS idx_packatom_atom ON pack_atom(atom_id);
"""


# ---------------------------------------------------------------- 属性目录
# 导出时的属性清单：key / 中文名 / 分组 / 隐私档 / 未勾选时的处理
#   sens:  导出时默认勾选（敏感，用户显式确认才带走）
#   open:  默认勾选（不敏感）
ATTRS = [
    # ---- 核心内容 ----
    ("title", "知识名称", "core", "open", "必选"),
    ("summary", "一句话摘要", "core", "open", "留空，正文仍可检索"),
    ("body", "事实内容（正文）", "core", "open", "只留摘要，正文不导出"),
    ("kind", "知识类型", "core", "open", "按 fact 处理"),
    ("scope", "作用域（含项目名）", "core", "sens", "置空"),
    # ---- 标签 ----
    ("facets_general", "标签：技术/主题/场景/活动", "facet", "open", "不导出这些维度"),
    ("facets_project", "标签：项目/工作区", "facet", "sens", "不导出项目标签"),
    ("facets_weight", "标签权重与置信度", "facet", "open", "只留标签名"),
    # ---- 来源 ----
    ("source_kind", "采集源类型", "source", "sens", "不导出"),
    ("source_ref", "来源引用（文件名/会话 ID）", "source", "sens", "不导出"),
    ("excerpt", "来源原文摘录", "source", "sens", "不导出"),
    ("raw_sha256", "原文归档指纹", "source", "sens", "不导出"),
    # ---- 可信度与时间 ----
    ("confidence", "置信度 / 事实可靠度 / 重要度", "meta", "open", "用默认值"),
    ("trust", "可信档位（用户明说/AI 归纳）", "meta", "open", "按 AI 归纳处理"),
    ("sensitivity", "敏感度等级", "meta", "open", "按 private 处理"),
    ("validity", "有效期与取代关系", "meta", "open", "视为长期有效"),
    ("created_at", "创建/更新时间（精确到秒）", "meta", "sens", "只留日期"),
    ("fingerprint", "知识指纹", "meta", "open", "按内容重算"),
    # ---- 使用反馈 ----
    ("usage", "使用反馈（被引用/采纳次数）", "usage", "open", "归零"),
]
ATTR_KEYS = [a[0] for a in ATTRS]
ATTR_MAP = {a[0]: a for a in ATTRS}


def attr_catalog():
    """给面板渲染属性勾选清单。"""
    return [{"key": k, "label": lb, "group": g, "privacy": p, "when_off": off,
             "default": False if p == "sens" else True}
            for k, lb, g, p, off in ATTRS]


# ---------------------------------------------------------------- 敏感度
# 仅规则判定，不调模型：命中强特征→private，命中弱特征→internal，否则 public
_PRIVATE_PAT = [
    ("密钥/令牌", r"(?i)(?:api[_-]?key|token|secret|password|passwd|bearer)\s*[:=]\s*\S+"),
    ("密钥前缀", r"(?<![A-Za-z0-9_-])(?:sk-[A-Za-z0-9_.\-]{20,}|ghp_[A-Za-z0-9]{20,})"),
    ("本机路径", r"/Users/[^/\s\"'`]+"),
    ("内网地址", r"(?<![\d.])(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}"
                 r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(?![\d.])"),
    ("联系方式", r"(?<![\d.])1[3-9]\d{9}(?![\d.])"),
]
_WEAK_PAT = [("项目专名", r"project:[^\s\"',，。]+")]

SENS_LEVELS = ("public", "internal", "private")


def judge_sensitivity(title="", body="", scope=None):
    """规则判定单条知识的敏感度。"""
    import re  # noqa: PLC0415
    text = "\n".join(x for x in (title, body, scope) if x)
    for _label, pat in _PRIVATE_PAT:
        if re.search(pat, text):
            return "private"
    for _label, pat in _WEAK_PAT:
        if re.search(pat, text):
            return "internal"
    return "public"


def _connect(path=KB):
    """统一连接：加忙等待，避免和后台采集/归纳抢写时报 database is locked。"""
    c = sqlite3.connect(path, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=15000")
    return c


# ---------------------------------------------------------------- 迁移 / 初始化
def _ready(conn):
    """只读快检：一切就绪时让 ensure() 直接返回，不在面板 GET 里开写事务。

    任何一项不满足（缺表、缺列、有知识没归属、敏感度未判）都返回 False，
    交给 _migrate 一次性补齐——新采进来的知识也会因此自动落入默认集。
    """
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"pack", "pack_collection", "pack_atom"} <= names:
        return False
    if "sensitivity" not in {r[1] for r in conn.execute("PRAGMA table_info(atom)")}:
        return False
    if not conn.execute("SELECT 1 FROM pack WHERE pack_id=?",
                        (DEFAULT_PACK_ID,)).fetchone():
        return False
    if not conn.execute("SELECT 1 FROM pack_collection WHERE collection_id=?",
                        (DEFAULT_COLLECTION_ID,)).fetchone():
        return False
    if conn.execute(
            "SELECT 1 FROM atom a WHERE a.state='active' AND NOT EXISTS"
            " (SELECT 1 FROM pack_atom pa WHERE pa.atom_id=a.atom_id) LIMIT 1").fetchone():
        return False
    if conn.execute("SELECT 1 FROM atom WHERE sensitivity IS NULL LIMIT 1").fetchone():
        return False
    return True


def ensure(conn=None):
    """建表 + 迁移：给 atom 补 sensitivity；把没有归属的知识放进默认包。"""
    close = conn is None
    conn = conn or _connect()
    if _ready(conn):
        if close:
            conn.close()
        return True
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(atom)")}
    if "sensitivity" not in cols:
        conn.execute("ALTER TABLE atom ADD COLUMN sensitivity TEXT DEFAULT 'internal'")
        conn.commit()
    now = int(time.time())
    conn.execute(
        "INSERT OR IGNORE INTO pack(pack_id,name,description,author,owner,created_at,updated_at)"
        " VALUES (?,?,?,?,'self',?,?)",
        (DEFAULT_PACK_ID, "本地知识", "本机采集与归纳的全部知识", "", now, now))
    conn.execute(
        "INSERT OR IGNORE INTO pack_collection(collection_id,pack_id,name,description,"
        "enabled,n_atoms,order_no,created_at) VALUES (?,?,?,?,1,0,0,?)",
        (DEFAULT_COLLECTION_ID, DEFAULT_PACK_ID, "全部知识", "默认知识集，始终保持开启", now))
    # 未归属任何集的知识 -> 默认集
    conn.execute(
        "INSERT OR IGNORE INTO pack_atom(collection_id,atom_id,added_at)"
        " SELECT ?, a.atom_id, ? FROM atom a WHERE NOT EXISTS"
        " (SELECT 1 FROM pack_atom pa WHERE pa.atom_id=a.atom_id)", (DEFAULT_COLLECTION_ID, now))
    # 敏感度未判定的补判
    todo = conn.execute(
        "SELECT atom_id,title,body,scope FROM atom WHERE sensitivity IS NULL").fetchall()
    for r in todo:
        conn.execute("UPDATE atom SET sensitivity=? WHERE atom_id=?",
                     (judge_sensitivity(r["title"], r["body"], r["scope"]), r["atom_id"]))
    refresh_counts(conn)
    conn.commit()
    if close:
        conn.close()
    return True


def refresh_counts(conn):
    for r in conn.execute("SELECT collection_id FROM pack_collection").fetchall():
        cid = r["collection_id"]
        n = conn.execute(
            "SELECT COUNT(*) FROM pack_atom pa JOIN atom a ON a.atom_id=pa.atom_id"
            " WHERE pa.collection_id=? AND a.state='active'", (cid,)).fetchone()[0]
        conn.execute("UPDATE pack_collection SET n_atoms=? WHERE collection_id=?", (n, cid))


def assign_sensitivity(conn=None, only_null=False):
    """给知识打敏感度等级，返回 {level: count}。"""
    close = conn is None
    conn = conn or _connect()
    sql = "SELECT atom_id,title,body,scope FROM atom"
    if only_null:
        sql += " WHERE sensitivity IS NULL"
    rows = conn.execute(sql).fetchall()
    for r in rows:
        conn.execute(
            "UPDATE atom SET sensitivity=? WHERE atom_id=? AND (? IS NOT NULL)",
            (judge_sensitivity(r["title"], r["body"], r["scope"]), r["atom_id"], r["title"]))
    conn.commit()
    out = {k: 0 for k in SENS_LEVELS}
    for r in conn.execute("SELECT sensitivity,COUNT(*) n FROM atom GROUP BY 1"):
        out[r["sensitivity"] or "internal"] = r["n"]
    if close:
        conn.close()
    return out


# ---------------------------------------------------------------- 可见性
def enabled_collections(conn):
    return [r["collection_id"] for r in conn.execute(
        "SELECT collection_id FROM pack_collection WHERE enabled=1")]


def visible_sql(conn, alias="a"):
    """返回 (sql 片段, 参数)：只允许已开启知识集里的知识。

    没有任何已开启集时返回 (None, [])，表示不过滤（避免用户误关全部导致完全搜不到东西）。
    """
    cids = enabled_collections(conn)
    if not cids:
        return None, []
    ph = ",".join("?" * len(cids))
    return (f"EXISTS (SELECT 1 FROM pack_atom pa WHERE pa.atom_id={alias}.atom_id"
            f" AND pa.collection_id IN ({ph}))", cids)


def pack_of_atoms(conn, atom_ids):
    """给检索结果标注来源：{atom_id: [{pack, collection}]}"""
    if not atom_ids:
        return {}
    ph = ",".join("?" * len(atom_ids))
    out = {}
    for r in conn.execute(
            f"SELECT pa.atom_id, p.pack_id, p.name AS pack_name, p.owner,"
            f" c.collection_id, c.name AS coll_name FROM pack_atom pa"
            f" JOIN pack_collection c ON c.collection_id=pa.collection_id"
            f" JOIN pack p ON p.pack_id=c.pack_id WHERE pa.atom_id IN ({ph})",
            list(atom_ids)):
        out.setdefault(r["atom_id"], []).append(
            {"pack_id": r["pack_id"], "pack": r["pack_name"], "owner": r["owner"],
             "collection_id": r["collection_id"], "collection": r["coll_name"]})
    return out


# ---------------------------------------------------------------- 包管理
def list_packs(conn=None):
    close = conn is None
    conn = conn or _connect()
    ensure(conn)
    packs = []
    for p in conn.execute("SELECT * FROM pack ORDER BY owner, created_at").fetchall():
        colls = [dict(c) for c in conn.execute(
            "SELECT collection_id,name,description,enabled,n_atoms,created_at"
            " FROM pack_collection WHERE pack_id=? ORDER BY order_no, created_at",
            (p["pack_id"],)).fetchall()]
        d = dict(p)
        d["collections"] = colls
        d["n_atoms"] = len({r[0] for r in conn.execute(
            "SELECT pa.atom_id FROM pack_atom pa JOIN pack_collection c"
            " ON c.collection_id=pa.collection_id WHERE c.pack_id=?", (p["pack_id"],))})
        d["enabled_collections"] = sum(1 for c in colls if c["enabled"])
        packs.append(d)
    if close:
        conn.close()
    return packs


def set_enabled(collection_id, enabled, conn=None):
    close = conn is None
    conn = conn or _connect()
    conn.execute("UPDATE pack_collection SET enabled=? WHERE collection_id=?",
                 (1 if enabled else 0, collection_id))
    conn.commit()
    if close:
        conn.close()
    return {"collection_id": collection_id, "enabled": bool(enabled)}


def _slug(name):
    import re  # noqa: PLC0415
    s = re.sub(r"[^\w\-]+", "-", (name or "").strip()).strip("-").lower()
    return s or f"pack{int(time.time())}"


def create_pack(name, description="", author="", conn=None):
    close = conn is None
    conn = conn or _connect()
    ensure(conn)
    pid = f"P:{_slug(name)}"
    if conn.execute("SELECT 1 FROM pack WHERE pack_id=?", (pid,)).fetchone():
        pid = f"{pid}-{int(time.time()) % 100000}"
    now = int(time.time())
    conn.execute(
        "INSERT INTO pack(pack_id,name,description,author,owner,created_at,updated_at)"
        " VALUES (?,?,?,?,'self',?,?)", (pid, name, description, author, now, now))
    conn.commit()
    if close:
        conn.close()
    return {"pack_id": pid, "name": name}


def add_collection(pack_id, name, description="", enabled=False, conn=None):
    """新建知识集。filters 形如 {facets:[{dim,value}], kinds:[...], sensitivity:[...],
    query:"关键词", collection:""}，用于从现有知识里筛出成员。"""
    close = conn is None
    conn = conn or _connect()
    ensure(conn)
    now = int(time.time())
    n = conn.execute("SELECT COUNT(*) FROM pack_collection WHERE pack_id=?",
                     (pack_id,)).fetchone()[0]
    cid = f"C:{pack_id[2:]}:{_slug(name)}-{n + 1}"
    conn.execute(
        "INSERT INTO pack_collection(collection_id,pack_id,name,description,enabled,"
        "n_atoms,order_no,created_at) VALUES (?,?,?,?,?,0,?,?)",
        (cid, pack_id, name, description, 1 if enabled else 0, n + 1, now))
    conn.commit()
    if close:
        conn.close()
    return {"collection_id": cid, "name": name}


def collection_atoms(conn, cid, limit=100, offset=0):
    return [dict(r) for r in conn.execute(
        "SELECT a.atom_id,a.title,a.kind,a.summary,a.sensitivity,a.created_at"
        " FROM pack_atom pa JOIN atom a ON a.atom_id=pa.atom_id"
        " WHERE pa.collection_id=? AND a.state='active'"
        " ORDER BY a.created_at DESC LIMIT ? OFFSET ?", (cid, limit, offset))]


def fill_collection(cid, filters=None, conn=None):
    """按筛选条件把知识加入集（不动已在集里的）。返回新增条数。"""
    close = conn is None
    conn = conn or _connect()
    filters = filters or {}
    where, args = _filter_sql(filters)
    ids = [r[0] for r in conn.execute(
        f"SELECT a.atom_id FROM atom a WHERE a.state='active'{where}", args)]
    now = int(time.time())
    conn.executemany(
        "INSERT OR IGNORE INTO pack_atom(collection_id,atom_id,added_at) VALUES (?,?,?)",
        [(cid, aid, now) for aid in ids])
    refresh_counts(conn)
    conn.commit()
    if close:
        conn.close()
    return len(ids)


def _filter_sql(f):
    """把面板上的筛选条件翻译成 SQL（复用与检索一致的口径）。"""
    where, args = "", []
    for fc in f.get("facets") or []:
        dim, val = fc.get("dim"), fc.get("value")
        if not dim or not val:
            continue
        where += (" AND EXISTS (SELECT 1 FROM atom_facet af JOIN facet x"
                  " ON x.facet_id=af.facet_id WHERE af.atom_id=a.atom_id"
                  " AND x.dim=? AND x.value=?)")
        args += [dim, val]
    if f.get("kinds"):
        where += f" AND a.kind IN ({','.join('?' * len(f['kinds']))})"
        args += list(f["kinds"])
    if f.get("sensitivity"):
        where += f" AND a.sensitivity IN ({','.join('?' * len(f['sensitivity']))})"
        args += list(f["sensitivity"])
    if f.get("max_trust"):
        where += " AND a.trust_tier<=?"
        args.append(int(f["max_trust"]))
    if f.get("since"):
        where += " AND a.created_at>=?"
        args.append(int(f["since"]))
    if f.get("query"):
        like = f"%{f['query'].strip()}%"
        where += " AND (a.title LIKE ? OR a.body LIKE ?)"
        args += [like, like]
    return where, args


def preview_filter(filters=None, limit=20, conn=None):
    """建集时预览会纳入哪些知识（含敏感度分布）。"""
    close = conn is None
    conn = conn or _connect()
    where, args = _filter_sql(filters or {})
    rows = conn.execute(
        f"SELECT a.atom_id,a.title,a.kind,a.sensitivity,a.created_at FROM atom a"
        f" WHERE a.state='active'{where} ORDER BY a.created_at DESC LIMIT ?",
        args + [limit]).fetchall()
    total = conn.execute(
        f"SELECT COUNT(*) FROM atom a WHERE a.state='active'{where}", args).fetchone()[0]
    dist = {r[0]: r[1] for r in conn.execute(
        f"SELECT a.sensitivity,COUNT(*) FROM atom a WHERE a.state='active'{where}"
        f" GROUP BY 1", args)}
    if close:
        conn.close()
    return {"total": total, "sensitivity": dist, "samples": [dict(r) for r in rows]}


def collection_atom_ids(conn, cids):
    if not cids:
        return []
    ph = ",".join("?" * len(cids))
    return [r[0] for r in conn.execute(
        f"SELECT atom_id FROM pack_atom WHERE collection_id IN ({ph})", list(cids))]


def delete_pack(pack_id, conn=None):
    """删包（只允许自己创建的包）：连带删成员关系；知识本身保留在默认包里。"""
    close = conn is None
    conn = conn or _connect()
    p = conn.execute("SELECT owner,name FROM pack WHERE pack_id=?", (pack_id,)).fetchone()
    if not p:
        raise ValueError("包不存在")
    if p["owner"] != "self":
        raise ValueError("他人导入的包不可删除（可关闭它）")
    if pack_id == DEFAULT_PACK_ID:
        raise ValueError("默认包不可删除")
    cids = [r[0] for r in conn.execute(
        "SELECT collection_id FROM pack_collection WHERE pack_id=?", (pack_id,))]
    if cids:
        ph = ",".join("?" * len(cids))
        conn.execute(f"DELETE FROM pack_atom WHERE collection_id IN ({ph})", cids)
        conn.execute(f"DELETE FROM pack_collection WHERE collection_id IN ({ph})", cids)
    conn.execute("DELETE FROM pack WHERE pack_id=?", (pack_id,))
    conn.commit()
    if close:
        conn.close()
    return {"deleted": pack_id, "name": p["name"]}


def drop_collection(cid, conn=None):
    close = conn is None
    conn = conn or _connect()
    if cid == DEFAULT_COLLECTION_ID:
        raise ValueError("默认知识集不可删除")
    conn.execute("DELETE FROM pack_atom WHERE collection_id=?", (cid,))
    conn.execute("DELETE FROM pack_collection WHERE collection_id=?", (cid,))
    conn.commit()
    if close:
        conn.close()
    return {"deleted": cid}


if __name__ == "__main__":
    c = _connect()
    ensure(c)
    print(json.dumps(list_packs(c), ensure_ascii=False, indent=1)[:1500])
