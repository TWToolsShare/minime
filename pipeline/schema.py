"""知识层 schema。

分层：
  raw（原始归档，见 store/raw + manifest.db）
    -> atom（清洗归纳后的知识原子，最小可检索单位）
    -> entity / relation（知识图谱）
    -> atom_facet（多维标签）
    -> usage_event / feedback（学习型评分闭环）
    -> profile_*（能力画像快照）

建表分两条路，缺一不可：
  SCHEMA      —— 全新库一次建全（含索引）
  MIGRATIONS  —— 老库增量升级；`CREATE TABLE IF NOT EXISTS` 不会给已存在的表加列，
                 所以凡是「后加的表/索引/列」都必须登记成一个迁移，否则静默不生效。
"""
import os
import sqlite3
import time

import paths

KB = paths.KB

SCHEMA = """
PRAGMA journal_mode=WAL;

-- ---------- 知识原子 ----------
CREATE TABLE IF NOT EXISTS atom (
  atom_id       TEXT PRIMARY KEY,
  kind          TEXT NOT NULL,     -- decision|preference|fact|troubleshooting|pattern|event|insight
  title         TEXT NOT NULL,
  body          TEXT NOT NULL,
  summary       TEXT,              -- 一句话，供 MCP 返回
  scope         TEXT,              -- 作用域：global | project:<name> | tech:<x>
  confidence    REAL DEFAULT 0.6,  -- 抽取置信
  veracity      REAL DEFAULT 0.6,  -- 事实可靠度（被证据支持程度）
  static_importance REAL DEFAULT 0.5,
  trust_tier    INTEGER DEFAULT 2, -- 1=用户明确表达 2=AI归纳 3=推测
  half_life_days INTEGER DEFAULT 90,
  valid_from    INTEGER,
  valid_until   INTEGER,           -- NULL=长期有效
  supersedes    TEXT,              -- 被哪条 atom 取代
  state         TEXT DEFAULT 'active',  -- active|dormant|superseded|archived
  fingerprint   TEXT UNIQUE,       -- 去重：sha1(kind+normalized_title)
  created_at    INTEGER,
  updated_at    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_atom_kind ON atom(kind, state);
CREATE INDEX IF NOT EXISTS idx_atom_scope ON atom(scope, state);
CREATE INDEX IF NOT EXISTS idx_atom_state ON atom(state);

-- ---------- 知识来源（可追溯到 raw） ----------
CREATE TABLE IF NOT EXISTS atom_source (
  atom_id     TEXT NOT NULL,
  raw_sha256  TEXT NOT NULL,
  source_kind TEXT NOT NULL,       -- cloud_conversation|snapshot_turn|trae_memory|workbuddy_session|workbuddy_memory|qoder_session|doubao_conversation|manual
  source_ref  TEXT NOT NULL,       -- session_id / turn_id / 文件路径
  excerpt     TEXT,
  weight      REAL DEFAULT 1.0,
  PRIMARY KEY (atom_id, raw_sha256, source_ref)
);
CREATE INDEX IF NOT EXISTS idx_atomsrc_ref ON atom_source(source_ref);

-- ---------- 多维标签 ----------
CREATE TABLE IF NOT EXISTS facet (
  facet_id  TEXT PRIMARY KEY,      -- scene:project-dev
  dim       TEXT NOT NULL,         -- scene|project|topic|tech|activity|period|quality|自定义
  value     TEXT NOT NULL,
  parent    TEXT,
  aliases   TEXT,                  -- 逗号分隔
  UNIQUE(dim, value)
);
CREATE TABLE IF NOT EXISTS atom_facet (
  atom_id    TEXT NOT NULL,
  facet_id   TEXT NOT NULL,
  weight     REAL DEFAULT 1.0,
  confidence REAL DEFAULT 0.8,
  origin     TEXT DEFAULT 'auto',  -- auto|user|inherited
  PRIMARY KEY (atom_id, facet_id)
);
CREATE INDEX IF NOT EXISTS idx_atomfacet_facet ON atom_facet(facet_id, weight DESC);

-- ---------- 知识图谱 ----------
CREATE TABLE IF NOT EXISTS entity (
  entity_id TEXT PRIMARY KEY,
  type      TEXT NOT NULL,         -- project|tech|concept|person|tool|file|org
  name      TEXT NOT NULL,
  aliases   TEXT,
  UNIQUE(type, name)
);
CREATE TABLE IF NOT EXISTS relation (
  src_id    TEXT NOT NULL,
  dst_id    TEXT NOT NULL,
  rel       TEXT NOT NULL,         -- uses|part_of|solves|depends_on|prefers_over|derived_from
  weight    REAL DEFAULT 1.0,
  atom_id   TEXT,
  PRIMARY KEY (src_id, dst_id, rel)
);
CREATE INDEX IF NOT EXISTS idx_rel_src ON relation(src_id);
CREATE INDEX IF NOT EXISTS idx_rel_dst ON relation(dst_id);

-- ---------- 检索全文 ----------
-- contentless：只建倒排索引，不再存一份标题/正文副本（老库由 0006 迁移重建）。
-- 行号对应 atom.rowid，检索时 JOIN atom 取 atom_id。
CREATE VIRTUAL TABLE IF NOT EXISTS atom_fts USING fts5(
  title, summary, body, tags, content='', contentless_delete=1,
  tokenize='unicode61 remove_diacritics 2'
);

-- ---------- 使用与反馈（越用越好用） ----------
CREATE TABLE IF NOT EXISTS usage_event (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  atom_id    TEXT NOT NULL,
  event      TEXT NOT NULL,        -- retrieved|cited|accepted|rejected|corrected|verified
  surface    TEXT,                 -- mcp|web
  query      TEXT,
  delta      REAL DEFAULT 0,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_atom ON usage_event(atom_id, created_at DESC);

-- ---------- 知识向量（向量检索与语义闸门共用缓存） ----------
CREATE TABLE IF NOT EXISTS atom_embedding (
  atom_id    TEXT NOT NULL,
  model      TEXT NOT NULL,
  sig        TEXT NOT NULL,        -- 参与向量化的文本签名，变了才重算
  vec        TEXT NOT NULL,        -- base64(float32[])
  updated_at INTEGER NOT NULL,
  PRIMARY KEY(atom_id, model)
);
CREATE INDEX IF NOT EXISTS idx_atom_emb_model ON atom_embedding(model, updated_at);

-- ---------- 通用文本向量缓存（原型 / 查询等非知识文本） ----------
CREATE TABLE IF NOT EXISTS embedding_cache (
  key        TEXT NOT NULL,        -- proto:troubleshooting / query:<sha1>
  model      TEXT NOT NULL,
  sig        TEXT NOT NULL,
  vec        TEXT NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY(key, model)
);

-- ---------- 近似重复取代台账 ----------
CREATE TABLE IF NOT EXISTS supersede_log (
  loser_id   TEXT PRIMARY KEY,
  winner_id  TEXT NOT NULL,
  similarity REAL,
  created_at INTEGER NOT NULL
);

-- ---------- 迁移记录 ----------
CREATE TABLE IF NOT EXISTS _migrations (
  id         TEXT PRIMARY KEY,
  applied_at INTEGER NOT NULL
);

-- ---------- 语义闸门：被 embedding 判定为无关而归档的知识 ----------
CREATE TABLE IF NOT EXISTS semantic_archive (
  atom_id    TEXT PRIMARY KEY,
  score      REAL,
  model      TEXT,
  created_at INTEGER NOT NULL
);

-- ---------- 反馈降权：连续被标记为不相关而暂时下架的知识（state='dormant'） ----------
CREATE TABLE IF NOT EXISTS atom_demotion (
  atom_id       TEXT PRIMARY KEY,
  reason        TEXT NOT NULL,       -- rejected_twice 等
  reject_streak INTEGER,
  created_at    INTEGER NOT NULL
);

-- ---------- 能力画像 ----------
CREATE TABLE IF NOT EXISTS profile_snapshot (
  snap_id    TEXT PRIMARY KEY,
  created_at INTEGER NOT NULL,
  window_from INTEGER,
  window_to   INTEGER,
  payload    TEXT NOT NULL         -- JSON
);
CREATE TABLE IF NOT EXISTS profile_dimension (
  snap_id   TEXT NOT NULL,
  dimension TEXT NOT NULL,         -- tech_breadth|architecture|independence|delivery|debugging|...
  score     REAL,
  evidence  INTEGER,
  detail    TEXT,
  PRIMARY KEY (snap_id, dimension)
);

-- ---------- 采集进度 ----------
CREATE TABLE IF NOT EXISTS ingest_cursor (
  key   TEXT PRIMARY KEY,
  value TEXT
);

-- ---------- LLM 归纳产物（跨 normalize 重建持久保存） ----------
CREATE TABLE IF NOT EXISTS llm_atom (
  fingerprint TEXT PRIMARY KEY,
  source_ref  TEXT NOT NULL,
  payload     TEXT NOT NULL,       -- JSON: title/body/kind/scope/confidence/importance/tech/evidence_quote/project
  created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_cursor (
  source_ref   TEXT PRIMARY KEY,
  atoms        INTEGER DEFAULT 0,
  processed_at INTEGER NOT NULL
);
"""


def connect(path=KB):
    if path == KB:
        paths.ensure()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


# 每项是 (迁移 id, SQL 列表或可调用对象)；只增不改，已应用过的不会再跑。
# 新加表/索引/字段时追加一条，别再散落到各模块的 _ensure() 里。
def _fts_contentless(conn):
    """把老的 atom_fts（带 atom_id 列、存正文副本）换成 contentless 表。

    旧表仍带着列值，可以读出来按 atom.rowid 重灌，避免迁移后检索失效。
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='atom_fts'").fetchone()
    if not row or "content=''" in (row[0] or "").replace(" ", ""):
        return
    old = conn.execute(
        "SELECT atom_id, title, summary, body, tags FROM atom_fts").fetchall()
    conn.execute("DROP TABLE atom_fts")
    conn.execute("""CREATE VIRTUAL TABLE atom_fts USING fts5(
        title, summary, body, tags, content='', contentless_delete=1,
        tokenize='unicode61 remove_diacritics 2')""")
    for atom_id, title, summary, body, tags in old:
        hit = conn.execute("SELECT rowid FROM atom WHERE atom_id=?", (atom_id,)).fetchone()
        if hit:
            conn.execute(
                "INSERT INTO atom_fts(rowid,title,summary,body,tags) VALUES (?,?,?,?,?)",
                (hit[0], title, summary, body, tags))


def _blob_vectors(conn):
    """把历史 base64 向量原地转成 float32 BLOB（省 25% 空间，解码更快）。"""
    import base64  # noqa: PLC0415
    for t in ("atom_embedding", "embedding_cache"):
        rows = conn.execute(
            f"SELECT rowid, vec FROM {t} WHERE typeof(vec)='text'").fetchall()
        for rid, v in rows:
            try:
                blob = base64.b64decode(v)
            except (ValueError, TypeError):
                continue
            conn.execute(f"UPDATE {t} SET vec=? WHERE rowid=?", (blob, rid))


MIGRATIONS = (
    ("0001_semantic_gate", (
        """CREATE TABLE IF NOT EXISTS semantic_archive(
             atom_id TEXT PRIMARY KEY, score REAL, model TEXT, created_at INTEGER NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS atom_demotion(
             atom_id TEXT PRIMARY KEY, reason TEXT NOT NULL,
             reject_streak INTEGER, created_at INTEGER NOT NULL)""",
    )),
    ("0002_atom_embedding", (
        """CREATE TABLE IF NOT EXISTS atom_embedding(
             atom_id TEXT NOT NULL, model TEXT NOT NULL, sig TEXT NOT NULL,
             vec TEXT NOT NULL, updated_at INTEGER NOT NULL,
             PRIMARY KEY(atom_id, model))""",
        "CREATE INDEX IF NOT EXISTS idx_atom_emb_model ON atom_embedding(model, updated_at)",
    )),
    ("0003_embedding_cache", (
        """CREATE TABLE IF NOT EXISTS embedding_cache(
             key TEXT NOT NULL, model TEXT NOT NULL, sig TEXT NOT NULL,
             vec TEXT NOT NULL, updated_at INTEGER NOT NULL,
             PRIMARY KEY(key, model))""",
    )),
    ("0004_supersede_log", (
        """CREATE TABLE IF NOT EXISTS supersede_log(
             loser_id TEXT PRIMARY KEY, winner_id TEXT NOT NULL,
             similarity REAL, created_at INTEGER NOT NULL)""",
    )),
    ("0005_usage_indexes", (
        # 反馈分按 atom 聚合、按事件类型过滤，补一个覆盖索引避免回表
        "CREATE INDEX IF NOT EXISTS idx_usage_event ON usage_event(atom_id, event)",
    )),
    ("0006_fts_contentless", (_fts_contentless,)),
    ("0007_blob_vectors", (_blob_vectors,)),
)


def migrate(conn):
    """把老库升到最新：只补缺的表/索引，幂等，可重复调用。"""
    conn.execute("""CREATE TABLE IF NOT EXISTS _migrations(
                      id TEXT PRIMARY KEY, applied_at INTEGER NOT NULL)""")
    done = {r[0] for r in conn.execute("SELECT id FROM _migrations")}
    applied = []
    for mid, stmts in MIGRATIONS:
        if mid in done:
            continue
        for sql in stmts:
            try:
                sql(conn) if callable(sql) else conn.execute(sql)
            except sqlite3.OperationalError:
                pass  # 索引名冲突等：已有等价结构，不阻塞后续迁移
        conn.execute("INSERT OR REPLACE INTO _migrations(id, applied_at) VALUES (?,?)",
                     (mid, int(time.time())))
        applied.append(mid)
    if applied:
        conn.commit()
    return applied


def init(path=KB):
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    migrate(conn)
    return conn


if __name__ == "__main__":
    c = init()
    tables = [r[0] for r in c.execute(
        "select name from sqlite_master where type in ('table','view') order by name")]
    print(f"{KB}\n" + "\n".join(tables))
