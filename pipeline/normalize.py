"""清洗归纳：raw -> atom。

三类知识抽取器，全部可追溯到 raw：
  1. trae_memory/{project_memory,topics}.md  —— 已清洗的高质量结论，按条目切分成 atom
  2. snapshot turn_file                       —— AI 改文件的轨迹，按「文件族 + 时段」聚合成改动模式
  3. cloud_messages                           —— 用户原话/决策，抽取 user 消息为 event/decision
  4. workbuddy_session / workbuddy_memory      —— WorkBuddy 的会话提问、长期记忆与身份
  5. qoder_session                            —— Qoder 的会话提问
  6. doubao_conversation                      —— 豆包导出的会话提问与文档

不做 LLM 调用：规则 + 结构抽取，保证任何环境都能跑。
LLM 增强作为可选阶段（pipeline/llm_enrich.py）后置。
kind 的细分类（踩坑/决定/偏好）不在这里做：规则只能靠词表命中，命中率有限，
统一交给清洗末尾的语义校准（pipeline/semantic_gate.py，用原型向量改判）。
"""
import hashlib
import json
import os
import re
import sqlite3
import sys
import time

import paths

RAW = paths.RAW
MANIFEST = paths.MANIFEST
TURNS_DB = MANIFEST
KB = paths.KB

_SUMM = None   # 本轮长文摘要器（LLM 未启用时为 None）


def _summary(text):
    """长文摘要：启用 LLM 时生成摘要，否则取前 160 字；正文本身不截断。"""
    text = (text or "").strip()
    return _SUMM.summary(text) if _SUMM is not None else text[:160]

HALF_LIFE = {
    "preference": 180, "decision": 90, "fact": 30,
    "troubleshooting": 365, "pattern": 120, "event": 14, "insight": 240,
}

# 「踩坑类」句子标记：命中即初步判为 troubleshooting，其余留给语义校准细化
LESSON_MARKERS = ("踩坑", "坑", "失败", "报错", "修复", "问题", "错误",
                  "root cause", "原因", "导致", "而不是")


def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def norm_title(s: str) -> str:
    return re.sub(r"[\s`*_#>:\-—（）()\[\]【】,，。.]+", "", s).lower()[:80]


def bigrams(text: str) -> str:
    """中文按二元组切词，英文/数字按原词保留（供 FTS5 unicode61 检索）。"""
    out = []
    for chunk in re.findall(r"[A-Za-z0-9_./+-]+|[\u4e00-\u9fff]+", text or ""):
        if re.match(r"[A-Za-z0-9]", chunk):
            out.append(chunk.lower())
        else:
            out.extend(chunk[i:i + 2] for i in range(len(chunk) - 1))
    return " ".join(out)


def fingerprint(kind: str, title: str) -> str:
    return sha1(f"{kind}::{norm_title(title)}")


# ---------------------------------------------------------------- facet 工具
def facet_id(dim: str, value: str) -> str:
    return f"{dim}:{value.strip().lower().replace(' ', '-')}"


TECH_ALIASES = {
    "python": ["python", ".py", "pip", "venv"],
    "typescript": ["typescript", ".ts", " ts ", "tsx", "node", "npm", "pnpm"],
    "java": ["java", ".java", "maven", "pom.xml", "spring", "mybatis"],
    "sql": ["sql", "sqlite", "mysql", "postgres", "ddl"],
    "docker": ["docker", "dockerfile", "镜像", "容器", "compose"],
    "mcp": ["mcp", "mcpserver", "model context protocol"],
    "llm": ["llm", "agent", "prompt", "token", "模型", "推理"],
    "git": ["git", "commit", "分支", "rebase", "merge"],
    "frontend": ["html", "css", "react", "vue", "web 页面", "前端"],
    "excel": ["excel", "xlsx", "openpyxl", "表格"],
    "ppt": ["ppt", "pptx", "幻灯片", "演示文稿"],
    "macos": ["macos", "darwin", "zsh", "launchd", "keychain"],
}


def detect_tech(text: str):
    low = text.lower()
    return [t for t, keys in TECH_ALIASES.items() if any(k in low for k in keys)]


def detect_scene(text: str):
    out = []
    if any(k in text for k in ("方案", "架构", "设计", "选型", "取舍")):
        out.append("architecture")
    if any(k in text for k in ("实现", "开发", "编码", "重构", "修复", "bug")):
        out.append("coding")
    if any(k in text for k in ("部署", "上线", "发布", "运维", "docker")):
        out.append("delivery")
    if any(k in text for k in ("排查", "报错", "异常", "日志", "定位")):
        out.append("troubleshooting")
    if any(k in text for k in ("整理", "汇总", "报告", "文档", "PPT", "计划")):
        out.append("documentation")
    return out or ["general"]


# ---------------------------------------------------------------- 写库
class Writer:
    """知识层写入器。

    full=True （默认）：重建模式，写临时表，最后 swap_in() 一次性替换——服务不中断。
    full=False        ：增量模式，直接追加到正式表——只补几条时不该动已有数据。
    """

    def __init__(self, conn, manifest=None, full=True):
        self.c = conn
        self.full = full
        self.manifest = manifest or sqlite3.connect(MANIFEST)
        self.stats = {"atom": 0, "dup": 0, "source": 0, "facet": 0}
        if not full:
            return
        # 重建前不备份旧表：atom_new 由本轮清洗从零生成，旧数据只在 swap_in 时整体替换
        self.c.executescript("""
        DROP TABLE IF EXISTS atom_new; DROP TABLE IF EXISTS atom_facet_new;
        DROP TABLE IF EXISTS atom_source_new; DROP TABLE IF EXISTS facet_new;
        DROP TABLE IF EXISTS entity_new; DROP TABLE IF EXISTS relation_new;
        """)
        for t in ("atom", "atom_facet", "atom_source", "facet", "entity", "relation"):
            self.c.execute(f"CREATE TEMP TABLE {t}_new AS SELECT * FROM {t} WHERE 0")
        self.c.execute("DELETE FROM atom_fts")
        self.c.commit()

    def _target(self, table):
        """重建模式写临时表，避免污染正在对外服务的旧数据；增量模式直接写正式表。"""
        return f"{table}_new" if self.full else table

    def T(self, table):
        return self._target(table)

    def put_facet(self, dim, value, parent=None):
        fid = facet_id(dim, value)
        self.c.execute(
            f"INSERT OR IGNORE INTO {self.T('facet')}(facet_id,dim,value,parent)"
            " VALUES (?,?,?,?)",
            (fid, dim, value, parent))
        return fid

    def add_atom(self, *, kind, title, body, summary=None, scope=None,
                 confidence=0.6, veracity=0.6, importance=0.5, trust_tier=2,
                 raw_sha=None, source_kind=None, source_ref=None, excerpt=None,
                 facets=()):
        fp = fingerprint(kind, title)
        T = self.T
        row = self.c.execute(f"SELECT atom_id FROM {T('atom')} WHERE fingerprint=?",
                             (fp,)).fetchone()
        now = int(time.time())
        if row:
            aid = row[0]
            self.stats["dup"] += 1
            # 同一来源的新版本：指纹只看 kind+标题，正文变了也要刷新，
            # 否则老库永远停在首次清洗时的内容快照。
            prev = None
            if raw_sha and source_kind and source_ref:
                prev = self.c.execute(
                    f"SELECT raw_sha256 FROM {T('atom_source')}"
                    " WHERE atom_id=? AND source_kind=? AND source_ref=?",
                    (aid, source_kind, source_ref)).fetchone()
            if prev and prev[0] != raw_sha:
                self.c.execute(
                    f"UPDATE {T('atom')} SET body=?, summary=?, confidence=?, "
                    "static_importance=?, trust_tier=?, updated_at=?, "
                    "veracity=MIN(0.99, veracity+0.05) WHERE atom_id=?",
                    (body.strip(), summary, confidence, importance, trust_tier, now, aid))
                self.c.execute(
                    f"DELETE FROM {T('atom_source')}"
                    " WHERE atom_id=? AND source_kind=? AND source_ref=?",
                    (aid, source_kind, source_ref))
            else:
                # 同一知识被再次确认 -> 提升 veracity；元数据跟着清洗规则走，
                # 这样调权重（如源码降级）重跑即可生效，不必重建
                self.c.execute(
                    f"UPDATE {T('atom')} SET veracity=MIN(0.99, veracity+0.05), "
                    "confidence=?, static_importance=?, trust_tier=?, updated_at=? "
                    "WHERE atom_id=?",
                    (confidence, importance, trust_tier, now, aid))
        else:
            aid = "A" + sha1(f"{kind}{norm_title(title)}")[:15]
            self.c.execute(
                f"""INSERT INTO {T('atom')}(atom_id,kind,title,body,summary,scope,confidence,
                   veracity,static_importance,trust_tier,half_life_days,valid_from,
                   state,created_at,updated_at,fingerprint)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (aid, kind, title.strip()[:200], body.strip(), summary,
                 scope, confidence, veracity, importance, trust_tier,
                 HALF_LIFE.get(kind, 90), now, "active", now, now, fp))
            self.stats["atom"] += 1
        # 重建模式下 FTS 最后统一全量重建，逐条写只会白算一遍 bigram
        if not self.full:
            self._fts_index(aid)
        if raw_sha:
            self.c.execute(
                f"""INSERT OR IGNORE INTO {T('atom_source')}(atom_id,raw_sha256,source_kind,
                   source_ref,excerpt) VALUES (?,?,?,?,?)""",
                (aid, raw_sha, source_kind, source_ref, (excerpt or "")[:500]))
            self.stats["source"] += 1
        for fid in facets:
            self.c.execute(
                f"INSERT OR IGNORE INTO {T('atom_facet')}"
                "(atom_id,facet_id,weight,confidence,origin) VALUES (?,?,?,?,?)",
                (aid, fid, 1.0, 0.8, "auto"))
            self.stats["facet"] += 1
        return aid

    def _fts_index(self, aid):
        """写入 FTS。中文用 bigram 展开，规避 unicode61 不分词的问题。

        atom_fts 是 contentless 表（不存列值），行号对应 atom.rowid，
        因此必须在 atom 正式表里查，重建流程要等 swap_in 之后再跑。
        """
        row = self.c.execute(
            "SELECT rowid, title, COALESCE(summary,''), body FROM atom WHERE atom_id=?",
            (aid,)).fetchone()
        if not row:
            return
        tags = " ".join(r[0] for r in self.c.execute(
            "SELECT f.value FROM atom_facet af JOIN facet f"
            " ON f.facet_id=af.facet_id WHERE af.atom_id=?", (aid,)))
        text = f"{row[1]} {row[2]} {row[3]} {tags}"
        self.c.execute("DELETE FROM atom_fts WHERE rowid=?", (row[0],))
        self.c.execute(
            "INSERT INTO atom_fts(rowid,title,summary,body,tags) VALUES (?,?,?,?,?)",
            (row[0], row[1], row[2], row[3], bigrams(text)))

    def rebuild_fts_tags(self):
        """重建全部 FTS（标签/中文 bigram 需要最终一致性）。"""
        if not self.full:
            # 增量模式不能清空整表 FTS，只补当前新写入的原子
            return
        self.c.execute("INSERT INTO atom_fts(atom_fts) VALUES('delete-all')")
        ids = [r[0] for r in self.c.execute("SELECT atom_id FROM atom")]
        for aid in ids:
            self._fts_index(aid)

    def swap_in(self):
        """一次性把临时表换成正式表（单事务，面板不会看到中间态）。

        entity/relation 由 graph.py 维护；这里若直接删会丢图谱，
        因此临时表里没有的实体保持原样。
        """
        cols = {
            "atom": ("atom_id", "kind", "title", "body", "summary", "scope", "confidence",
                     "veracity", "static_importance", "trust_tier", "half_life_days",
                     "valid_from", "valid_until", "supersedes", "state", "fingerprint",
                     "created_at", "updated_at"),
            "atom_facet": ("atom_id", "facet_id", "weight", "confidence", "origin"),
            "atom_source": ("atom_id", "raw_sha256", "source_kind", "source_ref", "excerpt",
                            "weight"),
            "facet": ("facet_id", "dim", "value", "parent", "aliases"),
        }
        self.c.commit()          # 先落掉前面未提交的写，才能开启显式事务
        # 重建时把「已归档 / 已取代」的状态带过去：否则每次清洗都会把被否定的知识复活
        self.c.execute("DROP TABLE IF EXISTS keep_state")
        self.c.execute("CREATE TEMP TABLE keep_state AS"
                       " SELECT atom_id,state,supersedes,valid_until FROM atom"
                       " WHERE state!='active'")
        self.c.execute("BEGIN IMMEDIATE")
        try:
            # entity/relation 由 graph.py 从 facet 推导，必须在 facet 换入后重建；
            # 这里先清空，随后 main() 调用 graph.main() 补齐，避免图谱指向已消失的标签。
            self.c.execute("DELETE FROM relation")
            self.c.execute("DELETE FROM entity")
            for t, cs in cols.items():
                self.c.execute(f"DELETE FROM {t}")
                self.c.execute(f"INSERT OR IGNORE INTO {t}({','.join(cs)})"
                               f" SELECT {','.join(cs)} FROM {t}_new")
            self.c.execute("""
                UPDATE atom SET
                  state=(SELECT k.state FROM keep_state k WHERE k.atom_id=atom.atom_id),
                  supersedes=(SELECT k.supersedes FROM keep_state k WHERE k.atom_id=atom.atom_id),
                  valid_until=(SELECT k.valid_until FROM keep_state k WHERE k.atom_id=atom.atom_id)
                WHERE atom_id IN (SELECT atom_id FROM keep_state)""")
            # 已被删掉的原子，其图谱边与反馈保留但不再被检索命中；关系表不重建。
            # FTS 由 swap_in 之后的 rebuild_fts_tags() 整体重建，这里不逐条清理。
            self.c.commit()
        except Exception:
            self.c.rollback()
            raise
        finally:
            for t in cols:
                self.c.execute(f"DROP TABLE IF EXISTS {t}_new")
            self.c.execute("DROP TABLE IF EXISTS keep_state")
            self.c.commit()


# ---------------------------------------------------------------- 1. trae memory
def _raw_path(rel):
    return os.path.join(RAW, rel)


def load_manifest(conn):
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT sha256,kind,source,path FROM raw_object ORDER BY rowid").fetchall()
    return rows


def project_of(source: str):
    """从 trae memory 路径推断项目名。

    目录名形如 -Users-<用户名>-works-<组织>-<仓库>，编码时 '/' 变成 '-'。
    这里按已知根（works / Desktop / data）反推：works/work/<org>/<repo>/...
    """
    m = re.match(r"projects/([^/]+)/", source)
    if not m:
        return "personal" if source == "user_profile.md" else None
    seg = re.sub(r"--p2-[0-9a-f]+$", "", m.group(1)).strip("-")
    parts = seg.split("-")
    for root in ("works", "Desktop", "data", "notes"):
        if root in parts:
            i = parts.index(root)
            tail = parts[i + 1:]
            if tail and tail[0] in ("work", "development"):
                tail = tail[1:]
            # org 只有一层（VBD / weave-spark 等），repo 可能多层，用 / 拼接前两段
            if len(tail) >= 2:
                return f"{tail[0]}/{ '-'.join(tail[1:]) }"
            return "-".join(tail) if tail else seg
    return seg


def ingest_trae_memory(w):
    for r in load_manifest(w.manifest):
        if r["kind"] != "trae_memory":
            continue
        src = r["source"]
        text = open(_raw_path(r["path"]), encoding="utf-8", errors="replace").read()
        proj = project_of(src)
        proj_facet = w.put_facet("project", proj) if proj else None
        if src == "user_profile.md":
            _ingest_profile(w, r, text, None)
        elif src.endswith("project_memory.md"):
            _ingest_sections(w, r, text, proj, proj_facet, trust=1)
        elif src.endswith("topics.md"):
            _ingest_topics(w, r, text, proj, proj_facet)
        elif src.endswith(".jsonl"):
            _ingest_session_memory(w, r, text, proj, proj_facet)


def _ingest_profile(w, r, text, proj_facet):
    section = "general"
    for line in text.split("\n"):
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            section = s.strip("# ").strip()
            continue
        if s.startswith(("-", "*")):
            _one_atom(w, r, s.lstrip("-* ").strip(),
                      kind="preference", scope="global", trust=1,
                      importance=0.85, facets=[proj_facet] if proj_facet else [],
                      section=section)


SECTION_KIND = {
    "hard constraints": ("preference", 0.9),
    "硬约束": ("preference", 0.9),
    "guidelines": ("pattern", 0.7),
    "工程约定": ("pattern", 0.75),
    "lessons learned": ("troubleshooting", 0.85),
    "经验教训": ("troubleshooting", 0.85),
    "项目上下文": ("fact", 0.6),
    "偏好": ("preference", 0.85),
    "约束": ("preference", 0.85),
    "工作领域": ("fact", 0.7),
}


def _ingest_sections(w, r, text, proj, proj_facet, trust=1):
    section = "context"
    for line in text.split("\n"):
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            section = s.strip("# ").strip().lower()
            continue
        if s.startswith(("-", "*")):
            body = s.lstrip("-* ").strip()
            kind, imp = SECTION_KIND.get(section, ("fact", 0.5))
            if any(k in body for k in LESSON_MARKERS):
                kind = "troubleshooting"
            _one_atom(w, r, body, kind=kind, scope=f"project:{proj}" if proj else None,
                      trust=trust, importance=imp,
                      facets=[proj_facet] if proj_facet else [], section=section)


def _ingest_topics(w, r, text, proj, proj_facet):
    for line in text.split("\n"):
        s = line.strip()
        m = re.match(r"\[session_id:\s*(\S+?)\s*\|[^\]]*\]\s*(.+)", s)
        if not m:
            continue
        sid, body = m.group(1), m.group(2).strip()
        if len(body) < 10:
            continue
        _one_atom(w, r, body, kind="event", scope=f"project:{proj}" if proj else None,
                  trust=2, importance=0.45, session=sid,
                  facets=[proj_facet] if proj_facet else [], section="topic")


def _ingest_session_memory(w, r, text, proj, proj_facet):
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        content = obj.get("content") or obj.get("text") or ""
        if isinstance(content, list):
            content = " ".join(str(x) for x in content)
        if len(str(content)) < 20:
            continue
        _one_atom(w, r, str(content), kind="event",
                  scope=f"project:{proj}" if proj else None, trust=2,
                  importance=0.4, facets=[proj_facet] if proj_facet else [],
                  section="session")


def _one_atom(w, r, body, *, kind, scope, trust, importance, facets,
              section="", session=None):
    body = body.strip()
    if len(body) < 8:
        return
    # 标题：取第一句/第一个分句
    title = re.split(r"[。；;\n]| — | -- ", body)[0].strip()
    title = title[:120] if title else body[:120]
    summary = _summary(body)
    techs = detect_tech(body)
    scenes = detect_scene(body)
    fs = list(facets) + [w.put_facet("tech", t) for t in techs]
    fs += [w.put_facet("scene", s) for s in scenes]
    fs += [w.put_facet("topic", section)] if section else []
    conf = 0.9 if trust == 1 else 0.7
    w.add_atom(kind=kind, title=title, body=body, summary=summary, scope=scope,
               confidence=conf, veracity=0.8 if trust == 1 else 0.6,
               importance=importance, trust_tier=trust,
               raw_sha=r["sha256"], source_kind="trae_memory",
               source_ref=r["source"] + (f"#{session}" if session else ""),
               excerpt=body[:400], facets=fs)


# ---------------------------------------------------------------- 2. snapshot turns
DOMAIN_HINT = {
    "test": ["test", "spec", "tests/"],
    "config": [".yml", ".yaml", ".json", ".toml", ".env", "config"],
    "docs": [".md", "docs/", "readme"],
    "schema": ["sql", "schema", "migration", "ddl"],
    "ui": [".css", ".scss", ".tsx", ".jsx", ".html", ".vue"],
    "infra": ["dockerfile", "docker-compose", "deploy", ".sh", "k8s", "helm"],
}


def file_domain(path):
    low = path.lower()
    for d, keys in DOMAIN_HINT.items():
        if any(k in low for k in keys):
            return d
    ext = os.path.splitext(low)[1]
    return {"py": "backend", "ts": "backend", "js": "backend", "java": "backend",
            "go": "backend", "rs": "backend"}.get(ext.lstrip("."), "other")


def ingest_snapshot_turns(w):
    """把每轮的文件改动聚成「工作单元」，再抽成 pattern/event atom。"""
    index = {r["source"]: r for r in load_manifest(w.manifest) if r["kind"] == "snapshot_log"}
    tdb = w.manifest
    tdb.row_factory = sqlite3.Row
    rows = tdb.execute("""
        SELECT session_id, turn_id, ts, n_files, n_added, n_modified, n_deleted,
               additions, deletions
        FROM turn ORDER BY session_id, ts""").fetchall()
    by_session = {}
    for r in rows:
        by_session.setdefault(r[0], []).append(r)
    for sid, turns in by_session.items():
        files = tdb.execute(
            "SELECT turn_id,path,action,additions,deletions FROM turn_file WHERE session_id=?",
            (sid,)).fetchall()
        per_turn = {}
        for f in files:
            per_turn.setdefault(f["turn_id"], []).append(f)
        raw = index.get(sid)
        # 会话级汇总
        all_paths = {f["path"] for f in files}
        domains = {}
        for p in all_paths:
            d = file_domain(p)
            domains[d] = domains.get(d, 0) + 1
        if not all_paths:
            continue
        top_dirs = {}
        for p in all_paths:
            seg = p.split("/")[0] if "/" in p else ""
            if seg:
                top_dirs[seg] = top_dirs.get(seg, 0) + 1
        title = f"会话 {sid[:8]} 的工作范围"
        body = (f"共 {len(turns)} 轮、改动 {len(all_paths)} 个文件。"
                f"领域分布：{', '.join(f'{k}×{v}' for k, v in sorted(domains.items(), key=lambda x: -x[1])[:6])}。"
                f"主要目录：{', '.join(f'{k}({v})' for k, v in sorted(top_dirs.items(), key=lambda x: -x[1])[:5])}。")
        fs = [w.put_facet("project", _project_from_paths(all_paths)),
              w.put_facet("activity", "coding")]
        for d in domains:
            fs.append(w.put_facet("topic", f"file:{d}"))
        techs = set()
        for p in all_paths:
            techs.update(detect_tech(p))
        fs += [w.put_facet("tech", t) for t in techs]
        w.add_atom(kind="event", title=title, body=body,
                   summary=f"{len(turns)} 轮 AI 改动 {len(all_paths)} 个文件",
                   scope=f"project:{_project_from_paths(all_paths)}",
                   confidence=0.9, veracity=0.95, importance=0.35, trust_tier=1,
                   raw_sha=raw["sha256"] if raw else None,
                   source_kind="snapshot_turn", source_ref=sid,
                   excerpt=body, facets=fs)
        # 轮次级：只记录有实质改动（>=5 行或新文件）的轮
        for t in turns:
            fl = per_turn.get(t["turn_id"], [])
            if not fl:
                continue
            if t["additions"] + t["deletions"] < 5 and t["n_added"] == 0 and len(fl) < 3:
                continue
            paths = [f["path"] for f in fl][:8]
            body = (f"{t['n_added']} 新增 / {t['n_modified']} 修改 / {t['n_deleted']} 删除，"
                    f"+{t['additions']}/-{t['deletions']} 行。文件："
                    + "、".join(paths))
            w.add_atom(kind="event", title=f"改动 {paths[0]} 等 {len(fl)} 个文件",
                       body=body, summary=body[:120],
                       scope=f"project:{_project_from_paths(set(paths))}",
                       confidence=0.95, veracity=0.95, importance=0.3, trust_tier=1,
                       raw_sha=raw["sha256"] if raw else None,
                       source_kind="snapshot_turn", source_ref=f"{sid}/{t['turn_id']}",
                       excerpt=body,
                       facets=[w.put_facet("activity", "coding"),
                               w.put_facet("project", _project_from_paths(set(paths)))])


def _project_from_paths(paths):
    """从真实文件路径反推项目名（works/<org>/<repo> 或顶层目录）。"""
    votes = {}
    for p in paths:
        parts = p.split("/")
        if "/" not in p:
            continue  # 根目录下的散文件不作为项目名
        if "works" in parts:
            i = parts.index("works")
            tail = parts[i + 1:]
            if tail and tail[0] in ("work", "development"):
                tail = tail[1:]
            name = f"{tail[0]}/{'-'.join(tail[1:2])}" if len(tail) >= 2 else (tail[0] if tail else "")
        elif parts[0] in ("tmp", "Users", "private", "var", "opt"):
            name = ""
        else:
            name = parts[0]
        if name:
            votes[name] = votes.get(name, 0) + 1
    return max(votes, key=votes.get) if votes else "unknown"


# ---------------------------------------------------------------- 3. cloud messages
def ingest_cloud(w):
    c = w.c
    raw = {}
    for r in load_manifest(w.manifest):
        if r["kind"] == "cloud_messages":
            raw[r["source"]] = r
    for sid, r in raw.items():
        data = json.load(open(_raw_path(r["path"]), encoding="utf-8"))
        items = data.get("data", {}).get("items", [])
        for it in items:
            if it.get("role") != "user":
                continue
            try:
                blocks = json.loads(it.get("content") or "[]")
            except ValueError:
                continue
            text = " ".join(b.get("text_content", "") for b in blocks
                            if isinstance(b, dict) and b.get("type") == "text")
            text = text.strip()
            if len(text) < 8:
                continue
            kind = "decision" if any(k in text for k in (
                "按你建议", "就这样", "确定", "同意", "改成", "用这个", "继续处理")) else "event"
            fs = [w.put_facet("scene", s) for s in detect_scene(text)]
            fs += [w.put_facet("tech", t) for t in detect_tech(text)]
            w.add_atom(kind=kind, title=text[:120], body=text,
                       summary=_summary(text), scope=None, confidence=0.95,
                       veracity=0.9, importance=0.6, trust_tier=1,
                       raw_sha=r["sha256"], source_kind="cloud_conversation",
                       source_ref=f"{sid}/{it.get('message_id')}",
                       excerpt=text[:400], facets=fs)


# ---------------------------------------------------------------- 4. 全工作区索引
def ingest_workspace_index(w):
    """把「每个工作区有多少会话/多少历史文件」写成项目级事实，补齐只改了文件但没进快照的会话。"""
    idx = paths.INDEX
    if not os.path.exists(idx):
        return
    db = sqlite3.connect(idx)
    db.row_factory = sqlite3.Row
    rows = db.execute("""
        SELECT w.workspace_id, w.path, w.name, w.kind, w.sessions,
               COUNT(s.session_id) AS n_sessions,
               MIN(s.first_ts) AS first_ts, MAX(s.last_ts) AS last_ts,
               SUM(CASE WHEN s.has_snapshot=1 THEN 1 ELSE 0 END) AS with_snap,
               (SELECT COUNT(*) FROM file_history f WHERE f.workspace_id=w.workspace_id) AS n_files
        FROM workspace w LEFT JOIN session s ON s.workspace_id=w.workspace_id
        GROUP BY w.workspace_id""").fetchall()
    for r in rows:
        if not r["name"] or r["name"] == "unknown":
            continue
        if not (r["n_sessions"] or r["n_files"]):
            continue
        body = (f"工作区 {r['path'] or r['name']}（{r['kind']}）共有 "
                f"{r['n_sessions']} 个 AI 会话，其中 {r['with_snap'] or 0} 个留有逐轮文件快照；"
                f"编辑器文件历史 {r['n_files']} 条。")
        w.add_atom(kind="fact", title=f"{r['name']} 工作区规模", body=body,
                   summary=body[:120], scope=f"project:{r['name']}",
                   confidence=0.95, veracity=0.95, importance=0.3, trust_tier=1,
                   source_kind="workspace_index", source_ref=r["workspace_id"],
                   excerpt=body,
                   facets=[w.put_facet("project", r["name"]),
                           w.put_facet("activity", "workspace_profile")])
    db.close()


# ---------------------------------------------------------------- 5. LLM 归纳回放
def write_llm_atom(w, a, project, source_ref):
    """把一条 LLM 归纳结果写成 atom。a 为 llm_atom.payload 反序列化后的 dict。

    返回 atom_id；数据不完整返回 None。
    """
    kind = a.get("kind") if a.get("kind") in HALF_LIFE else "insight"
    title = (a.get("title") or "").strip()
    body = (a.get("body") or "").strip()
    if not title or not body:
        return None
    techs = [x for x in (a.get("tech") or []) if isinstance(x, str)]
    facets = [w.put_facet("project", project)] if project else []
    facets += [w.put_facet("tech", x) for x in (techs or detect_tech(body))]
    facets += [w.put_facet("scene", s) for s in detect_scene(body)]
    facets.append(w.put_facet("activity", "llm_enriched"))
    aid = w.add_atom(
        kind=kind, title=title[:200], body=body, summary=_summary(body),
        scope=a.get("scope") or (f"project:{project}" if project else None),
        confidence=float(a.get("confidence") or 0.7),
        veracity=0.75, importance=float(a.get("importance") or 0.6),
        trust_tier=2, raw_sha=None, source_kind="llm_enrich",
        source_ref=source_ref,
        excerpt=(a.get("evidence_quote") or "")[:400], facets=facets)
    # raw_sha 为空时 add_atom 不写来源，这里单独补一条，保证「可追溯」
    w.c.execute(
        f"INSERT OR IGNORE INTO {w.T('atom_source')}"
        "(atom_id,raw_sha256,source_kind,source_ref,excerpt) VALUES (?,?,?,?,?)",
        (aid, "", "llm_enrich", source_ref, (a.get("evidence_quote") or "")[:500]))
    return aid


def replay_llm_atoms(w):
    """normalize 重建后，把已归纳的 LLM 原子重放回来（不再调用模型）。"""
    try:
        rows = w.c.execute(
            "SELECT source_ref, payload FROM llm_atom ORDER BY created_at").fetchall()
    except sqlite3.Error:
        return 0
    n = 0
    for ref, payload in rows:
        try:
            a = json.loads(payload)
        except ValueError:
            continue
        if write_llm_atom(w, a, a.get("project"), ref):
            n += 1
    return n


# ---------------------------------------------------------------- 5. 本地文档
def ingest_local_docs(w):
    """本地文档/笔记：按 Markdown 标题切段，每段成为可检索知识。"""
    for r in load_manifest(w.manifest):
        if r["kind"] != "local_doc":
            continue
        try:
            text = open(_raw_path(r["path"]), encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        if not text.strip():
            continue
        src = r["source"]
        # 只有 Markdown 类文本按标题切段；代码/JSON 里以 # 开头的注释（含 shebang）
        # 不是章节，否则每个 `# xxx` 注释行都会被切成一条独立知识。
        ext = os.path.splitext(src)[1].lower()
        is_md = ext in (".md", ".markdown")
        # 文档与代码分级：源码同样可检索，但排序要让位于文档（importance 0.28 / trust 3）
        is_code = ext not in (".md", ".markdown", ".txt", ".rst", ".org")
        parts = re.split(r"\n(?=#{1,4}[ \t]+\S)", text) if is_md else [text]
        proj = os.path.splitext(os.path.basename(src))[0]
        fs = [w.put_facet("activity", "local_doc")]
        fs += [w.put_facet("tech", t) for t in detect_tech(text[:4000])]
        for chunk in parts[:40]:
            chunk = chunk[:12000].strip()
            if len(chunk) < 20:
                continue
            head = re.match(r"#{1,4}[ \t]+(.+)", chunk) if is_md else None
            title = head.group(1).strip() if head else src
            body = chunk
            w.add_atom(kind="fact", title=f"{title}（{proj}）", body=body,
                       summary=_summary(body), scope=None,
                       confidence=0.65 if is_code else 0.8,
                       veracity=0.8 if is_code else 0.85,
                       importance=0.28 if is_code else 0.5,
                       trust_tier=3 if is_code else 2,
                       raw_sha=r["sha256"], source_kind="local_doc",
                       source_ref=src, excerpt=body[:400],
                       facets=fs + [w.put_facet("project", proj)])


# ---------------------------------------------------------------- 6. Obsidian
def ingest_obsidian(w):
    """Obsidian 笔记：按标题切段成原子，frontmatter 标签/双链转成 facet 与关系。"""
    for r in load_manifest(w.manifest):
        if r["kind"] != "obsidian_note":
            continue
        try:
            data = json.load(open(_raw_path(r["path"]), encoding="utf-8"))
        except (OSError, ValueError):
            continue
        body_text = data.get("body") or ""
        if len(body_text.strip()) < 20:
            continue
        vault = data.get("vault") or "vault"
        rel = data.get("path") or r["source"]
        stem = os.path.splitext(os.path.basename(rel))[0]
        fm = data.get("frontmatter") or {}
        tags = [t for t in (data.get("tags") or []) if t]
        fs = [w.put_facet("activity", "obsidian"),
              w.put_facet("project", stem)]
        fs += [w.put_facet("topic", t) for t in tags]
        fs += [w.put_facet("tech", t) for t in detect_tech(body_text[:4000])]
        fs += [w.put_facet("scene", s) for s in detect_scene(body_text[:2000])]

        parts = re.split(r"\n(?=#{1,4}[ \t]+\S)", body_text)
        title_base = str(fm.get("title") or stem)
        for idx, chunk in enumerate(parts[:40]):
            chunk = chunk.strip()
            if len(chunk) < 20:
                continue
            head = re.match(r"#{1,4}[ \t]+(.+)", chunk)
            sub = head.group(1).strip() if head else ""
            title = title_base if (idx == 0 and not sub) else (
                f"{title_base} · {sub}" if sub else f"{title_base} · {idx}")
            scope = str(fm.get("scope") or "") or f"project:{stem}"
            w.add_atom(kind="fact", title=title, body=chunk,
                       summary=_summary(chunk), scope=scope, confidence=0.85,
                       veracity=0.9, importance=0.55, trust_tier=2,
                       raw_sha=r["sha256"], source_kind="obsidian",
                       source_ref=f"{vault}/{rel}", excerpt=chunk[:400], facets=fs)
        # 双链 → 关系（由 graph.py 落地为实体边）
        wiki = data.get("wiki_links") or []
        if wiki:
            w.c.execute(
                f"INSERT OR IGNORE INTO {w.T('atom_source')}(atom_id,raw_sha256,source_kind,"
                f"source_ref,excerpt) SELECT atom_id,?,?,?,? FROM {w.T('atom')}"
                " WHERE fingerprint=?", (r["sha256"], "obsidian_links",
                                         f"{vault}/{rel}", ",".join(wiki[:20]),
                                         fingerprint("fact", title_base)))


# ---------------------------------------------------------------- 7. AI 助手会话
AGENT_LABELS = {"workbuddy_session": "WorkBuddy", "qoder_session": "Qoder"}
DECISION_MARKERS = ("按你建议", "就这样", "确定", "同意", "改成", "用这个", "继续处理",
                    "决定", "必须", "不要用", "选这个", "采用")


def _project_of_cwd(cwd, fallback):
    """WorkBuddy 的工作区通常在用户目录下，优先取目录名，其次用产品名。"""
    name = _project_from_paths({cwd}) if cwd else ""
    if name == "unknown":
        name = ""
    if not name and cwd:
        try:
            rel = os.path.relpath(str(cwd), os.path.expanduser("~"))
        except ValueError:
            rel = ""
        if rel and not rel.startswith(".."):
            name = rel.split(os.sep)[0]      # ~/WorkBuddy AI/... -> WorkBuddy AI
        if not name:
            name = os.path.basename(str(cwd).rstrip("/\\"))
    return name or fallback


def _conversation_facets(w, proj, text, topic):
    fs = [w.put_facet("activity", "conversation"),
          w.put_facet("topic", topic)]
    if proj:
        fs.append(w.put_facet("project", proj))
    fs += [w.put_facet("scene", s) for s in detect_scene(text)]
    fs += [w.put_facet("tech", t) for t in detect_tech(text)]
    return fs


def ingest_agent_sessions(w):
    """WorkBuddy / Qoder 会话：用户提问 + 会话级摘要。

    只抽用户自己说过的话（助手回复容易变成闲聊噪音）；会话标题、工作区、
    模型与消息规模单独成一条摘要，保证“我在这个工作区做过什么”可被检索。
    """
    for r in load_manifest(w.manifest):
        label = AGENT_LABELS.get(r["kind"])
        if not label:
            continue
        try:
            data = json.load(open(_raw_path(r["path"]), encoding="utf-8"))
        except (OSError, ValueError):
            continue
        product = data.get("product") or label.lower()
        topic = "qoder" if label == "Qoder" else "workbuddy"
        sid = str(data.get("session_id") or r["source"])
        messages = data.get("messages") or []
        user_msgs = [m for m in messages if m.get("role") == "user"]
        title = (data.get("title") or "").strip()
        if not title and user_msgs:
            title = (user_msgs[0].get("text") or "").strip()[:60]
        title = title or f"{label} 会话 {sid[:8]}"
        cwd = data.get("cwd") or ""
        proj = _project_of_cwd(cwd, product)
        model = data.get("model") or "未知"
        biz = label if product == topic else f"{label}（{product}）"
        body = (f"{biz} 会话「{title}」：{len(user_msgs)} 个提问、"
                f"{len(messages)} 条消息；工作区 {cwd or '未知'}；模型 {model}。")
        summary_title = (title if product == topic
                         else f"{title}（{label} · {product}）")
        w.add_atom(kind="event", title=summary_title, body=body,
                   summary=body, scope=f"project:{proj}", confidence=0.9,
                   veracity=0.85, importance=0.35, trust_tier=2,
                   raw_sha=r["sha256"], source_kind=r["kind"],
                   source_ref=f"{product}/{sid}", excerpt=body,
                   facets=_conversation_facets(w, proj, body, topic))
        for idx, msg in enumerate(user_msgs):
            text = (msg.get("text") or "").strip()
            if len(text) < 6 and not any(k in text for k in DECISION_MARKERS):
                continue
            kind = "decision" if any(k in text for k in DECISION_MARKERS) else "event"
            w.add_atom(kind=kind, title=text[:120], body=text, summary=_summary(text),
                       scope=f"project:{proj}", confidence=0.9, veracity=0.8,
                       importance=0.45, trust_tier=2, raw_sha=r["sha256"],
                       source_kind=r["kind"],
                       source_ref=f"{product}/{sid}/{msg.get('id') or idx}",
                       excerpt=text[:400],
                       facets=_conversation_facets(w, proj, text, topic))


def _memory_lines(text):
    """从记忆/身份 Markdown 里取有内容的条目（列表项或长段落）。"""
    text = re.sub(r"<!--.*?-->", "", text or "", flags=re.S)
    text = re.sub(r"\A---\s*\n.*?\n---\s*\n", "", text, flags=re.S)
    section = "general"
    for raw in text.split("\n"):
        line = raw.strip()
        if not line or line.startswith((">", "```", "RAW_JSON", "---")):
            continue
        if line.startswith("#"):
            section = line.strip("# ").strip()
            continue
        if line.startswith(("-", "*")):
            body = re.sub(r"^[-*]\s+", "", line).strip()
        elif len(line) >= 30 and not line.startswith(("|", "<")):
            body = line
        else:
            continue
        if not body or body.endswith("_") or body.startswith("_("):
            continue
        if re.match(r"^\*\*[^*]+:\*\*\s*$", body):   # 模板里的空字段
            continue
        if body.startswith("_"):
            continue
        if any(k in body for k in ("Fill this in", "pick something you like",
                                   "What do they care about", "This isn't just metadata")):
            continue
        yield section, body


def _identity_lines(text):
    """身份文件只取「字段: 已填写值」，模板里的说明文字不当作知识。"""
    for raw in (text or "").split("\n"):
        m = re.match(r"^\s*[-*]\s+\*\*([^*]+):\*\*\s*(.+?)\s*$", raw)
        if not m:
            continue
        name, value = m.group(1).strip(), m.group(2).strip()
        if not value or value.startswith("_(") or value.endswith("_"):
            continue
        if any(k in value for k in ("Fill this in", "pick something you like")):
            continue
        yield "identity", f"{name}: {value}"


def ingest_workbuddy_memory(w):
    """WorkBuddy 的长期记忆与身份文件：按条目拆成偏好/事实。"""
    for r in load_manifest(w.manifest):
        if r["kind"] != "workbuddy_memory":
            continue
        try:
            data = json.load(open(_raw_path(r["path"]), encoding="utf-8"))
        except (OSError, ValueError):
            continue
        body = data.get("body") or ""
        product = data.get("product") or "workbuddy"
        is_memory = data.get("kind") == "user_memory"
        is_identity = data.get("kind") == "identity"
        kind = "preference" if is_memory or is_identity else "fact"
        importance = 0.75 if is_memory else 0.6
        lines = _identity_lines(body) if is_identity else _memory_lines(body)
        for section, line in lines:
            title = line[:120]
            fs = [w.put_facet("activity", "memory"),
                  w.put_facet("topic", "workbuddy"),
                  w.put_facet("topic", section)]
            fs += [w.put_facet("tech", t) for t in detect_tech(line)]
            w.add_atom(kind=kind, title=title, body=line, summary=_summary(line),
                       scope="global", confidence=0.85, veracity=0.75,
                       importance=importance, trust_tier=2, raw_sha=r["sha256"],
                       source_kind="workbuddy_memory",
                       source_ref=f"{product}/{data.get('path') or r['source']}"
                                  f"#{norm_title(section) or 'general'}",
                       excerpt=line[:400], facets=fs)


# ---------------------------------------------------------------- 8. 豆包
def _doc_chunks(text, max_chars=4000):
    """长文档按标题/段落切块，保证单条知识不会无限长。"""
    parts = re.split(r"\n(?=#{1,4}[ \t]+\S)", text)
    out, buf = [], ""
    for part in parts:
        if buf and len(buf) + len(part) > max_chars:
            out.append(buf)
            buf = ""
        if len(part) > max_chars:
            for para in re.split(r"\n\s*\n", part):
                if len(para) > max_chars:
                    if buf:
                        out.append(buf)
                        buf = ""
                    out.extend(para[i:i + max_chars]
                               for i in range(0, len(para), max_chars))
                    continue
                if buf and len(buf) + len(para) > max_chars:
                    out.append(buf)
                    buf = ""
                buf = f"{buf}\n\n{para}".strip() if buf else para
        else:
            buf = f"{buf}\n\n{part}".strip() if buf else part
    if buf:
        out.append(buf)
    return [x.strip() for x in out if len(x.strip()) >= 20]


def ingest_doubao(w):
    """豆包会话：用户提问 + 会话摘要；导出的文档按段落成知识。"""
    for r in load_manifest(w.manifest):
        if r["kind"] != "doubao_conversation":
            continue
        try:
            data = json.load(open(_raw_path(r["path"]), encoding="utf-8"))
        except (OSError, ValueError):
            continue
        messages = data.get("messages") or []
        conv_id = str(data.get("conversation_id") or r["source"])
        ref = f"{data.get('source') or r['source']}#{conv_id}"
        user_msgs = [m for m in messages if m.get("role") == "user"]
        docs = [m for m in messages if m.get("role") == "document"]
        title = (data.get("title") or "").strip()
        if not title and user_msgs:
            title = (user_msgs[0].get("text") or "").strip()[:60]
        title = title or f"豆包会话 {conv_id[:8]}"
        facets = [w.put_facet("activity", "conversation"),
                  w.put_facet("topic", "doubao")]
        facets += [w.put_facet("tech", t) for t in detect_tech(title)]
        body = (f"豆包会话「{title}」：{len(user_msgs)} 个提问、"
                f"{len(messages)} 条消息。")
        w.add_atom(kind="event", title=f"{title}（豆包）", body=body, summary=body,
                   scope=None, confidence=0.9, veracity=0.8, importance=0.3,
                   trust_tier=2, raw_sha=r["sha256"],
                   source_kind="doubao_conversation", source_ref=ref,
                   excerpt=body, facets=facets)
        for idx, msg in enumerate(user_msgs):
            text = (msg.get("text") or "").strip()
            if len(text) < 6 and not any(k in text for k in DECISION_MARKERS):
                continue
            kind = "decision" if any(k in text for k in DECISION_MARKERS) else "event"
            w.add_atom(kind=kind, title=text[:120], body=text, summary=_summary(text),
                       scope=None, confidence=0.9, veracity=0.8, importance=0.45,
                       trust_tier=2, raw_sha=r["sha256"],
                       source_kind="doubao_conversation",
                       source_ref=f"{ref}/{msg.get('id') or idx}",
                       excerpt=text[:400],
                       facets=[w.put_facet("activity", "conversation"),
                               w.put_facet("topic", "doubao")]
                       + [w.put_facet("tech", t) for t in detect_tech(text)])
        for msg in docs:
            for i, chunk in enumerate(_doc_chunks(msg.get("text") or "")[:40]):
                head = re.match(r"#{1,4}[ \t]+(.+)", chunk)
                sub = head.group(1).strip() if head else ""
                w.add_atom(kind="fact",
                           title=f"{title} · {sub}" if sub else f"{title} · {i + 1}",
                           body=chunk, summary=_summary(chunk), scope=None,
                           confidence=0.85, veracity=0.85, importance=0.5,
                           trust_tier=2, raw_sha=r["sha256"],
                           source_kind="doubao_conversation",
                           source_ref=f"{ref}/doc/{i}",
                           excerpt=chunk[:400],
                           facets=[w.put_facet("activity", "local_doc"),
                                   w.put_facet("topic", "doubao")]
                           + [w.put_facet("tech", t) for t in detect_tech(chunk[:2000])])


# ---------------------------------------------------------------- main
def main():
    global _SUMM
    from schema import init  # noqa: PLC0415
    import config as C  # noqa: PLC0415
    import summarize as S  # noqa: PLC0415
    conn = init()
    _SUMM = S.Summarizer(conn, C.load())
    w = Writer(conn)
    ingest_trae_memory(w)
    ingest_snapshot_turns(w)
    ingest_cloud(w)
    ingest_workspace_index(w)
    ingest_local_docs(w)
    ingest_obsidian(w)
    ingest_agent_sessions(w)
    ingest_workbuddy_memory(w)
    ingest_doubao(w)
    replay_llm_atoms(w)
    w.swap_in()
    # FTS 是 contentless 表，行号绑定 atom.rowid；必须等正式表换入后再整体重建
    w.rebuild_fts_tags()
    # 语义闸门：开启 embedding 后，把与工作主题无关的条目归档（失败不影响清洗结果）
    semantic = None
    try:
        import semantic_gate  # noqa: PLC0415
        semantic = semantic_gate.run(conn)
    except Exception as e:  # noqa: BLE001
        semantic = {"error": f"{type(e).__name__}: {e}"}
        print(f"[semantic] skipped: {type(e).__name__}: {e}", file=sys.stderr)
    # 定期压缩：到期归档 + 近似重复取代（失败不影响清洗结果）
    consolidation = None
    try:
        import consolidate  # noqa: PLC0415
        consolidation = consolidate.run(conn)
    except Exception as e:  # noqa: BLE001
        consolidation = {"error": f"{type(e).__name__}: {e}"}
        print(f"[consolidate] skipped: {type(e).__name__}: {e}", file=sys.stderr)
    # 图谱在归档/取代之后重建，避免把已下线的知识继续算成节点
    import graph  # noqa: PLC0415
    graph.ensure_entities(conn)
    graph.build_relations(conn)
    conn.execute("INSERT OR REPLACE INTO ingest_cursor VALUES ('last_ingest', ?)",
                 (str(int(time.time())),))
    conn.commit()
    n = conn.execute("SELECT kind, COUNT(*) FROM atom GROUP BY kind").fetchall()
    print(json.dumps({
        "stats": w.stats,
        "atoms_by_kind": {r[0]: r[1] for r in n},
        "total_atoms": conn.execute("SELECT COUNT(*) FROM atom").fetchone()[0],
        "facets": conn.execute("SELECT COUNT(*) FROM facet").fetchone()[0],
        "semantic": semantic,
        "consolidate": consolidation,
        "summary": _SUMM.stats(),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
