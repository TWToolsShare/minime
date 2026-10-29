"""资源包（cyber-worker pack）：把个人知识与决策能力打包，可分享给别人导入。

一个包 = 一个 zip，内含：
  manifest.json     包元信息（名称/版本/作者/创建时间/知识集/逐属性隐私声明）
  collections.json  知识集清单（包内可分多套，导入后按集开关）
  atoms.jsonl       知识原子；字段按导出时勾选的属性取舍
  decisions.jsonl   决策样例（问题 → 判定 + 证据），供他人直接复用判断
  sources.json      来源清单（只保留「来源类型 + 计数」，不含原始对话与绝对路径）

隐私保护：
  - 默认不打包 store/raw 原始归档、不打包会话原文
  - 逐属性可选：来源引用/摘录/项目标签/精确时间戳 默认不带，勾选才带
  - 带上时仍做严格脱敏（路径→~，密钥/邮箱/手机号/IP/长数字串→***）
  - 导出后自查残留，写进 manifest.privacy.residual_risk

只允许导出「自己的包」（owner='self'）；他人导入的包只读可用、不可再导出。

用法：
  python pipeline/pack.py export --name my-worker --out my-worker.minime.zip
  python pipeline/pack.py export --name my-worker --collectors C:self:all --attrs title,body
  python pipeline/pack.py inspect my-worker.minime.zip
  python pipeline/pack.py import my-worker.minime.zip --dry-run
"""
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import packs  # noqa: E402
import paths  # noqa: E402

FORMAT = "minime-pack/1"
KB = paths.KB
MANIFEST = paths.MANIFEST

SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|passwd|bearer)\s*[:=]\s*\S+")
USER_PATH_RE = re.compile(r"/Users/[^/\s\"'`]+")
# 常见密钥前缀；sk-ws- 形态（含 . 与 -）是 Trae/OpenAI 系工作区密钥
# 前面不能紧邻字母/数字/连字符，否则 task-types、ask-xxx 这类词会被误判
SK_RE = re.compile(r"(?<![A-Za-z0-9_-])(sk-[A-Za-z0-9_.\-]{20,}|ghp_[A-Za-z0-9]{20,}|"
                   r"gho_[A-Za-z0-9]{20,}|xox[bap]-[A-Za-z0-9-]{10,})\b")


def scrub(text):
    """脱敏：绝对路径、用户名、密钥类片段。"""
    if not isinstance(text, str):
        return text
    text = USER_PATH_RE.sub("~", text)
    text = SECRET_RE.sub(lambda m: f"{m.group(1)}=***", text)
    text = SK_RE.sub("***", text)
    return text


# ---------------------------------------------------------------- 脱敏体检
# 导出前把「命中项」原样回给用户确认，避免不知道自己漏了什么
AUDIT_PATTERNS = [
    ("本地用户路径", re.compile(r"/Users/[^/\s\"'`]+")),
    ("绝对盘符/挂载路径", re.compile(r"(?<![\w.])/(?:Volumes|home|root|opt|srv)/[^\s\"'`]+")),
    # 只认「键=值」且值像真密钥的；键名单独出现（如 API_KEY）不算泄露
    # 同时排除代码引用形态（obj.token = x、TokenUtil.generateAccessToken）
    ("密钥/令牌片段", re.compile(
        r"(?i)(?<![\w.])(?:api[_-]?key|token|secret|password|passwd|bearer)"
        r"\s*[:=]\s*(?!\*{3}|\$|\{|<|\[)[\"']?([A-Za-z0-9_\-\.]{16,})")),
    ("常见密钥前缀", SK_RE),
    # 邮箱：排除 git@host、npm 包名、以及 "tools\\n@mcp" 这类跨行误判（@ 前必须是字母数字）
    ("邮箱", re.compile(
        r"(?<![A-Za-z0-9._%+-])([A-Za-z0-9._%+-]+@(?![\d.]+[\\n\"'`])"
        r"(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})(?![\w])")),
    # 手机号：前后不能紧邻数字，也不能是 10 位时间戳的一部分
    ("中国大陆手机号", re.compile(r"(?<![\d.])1[3-9]\d{9}(?![\d.])")),
    # IP：只认私网/回环，排除 1.1.0.0 这类版本号
    ("内网/公网 IP", re.compile(
        r"(?<![\d.])(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
        r"|192\.168\.\d{1,3}\.\d{1,3}"
        r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
        r"|127\.0\.0\.1)(?![\d.])")),
    # 长数字串（sha / user_id / 毫秒时间戳）：严格模式下也打掉，避免变相带走 ID
    ("长数字串", re.compile(r"(?<!\d)\d{16,}(?!\d)")),
]
STRICT_EXTRA = ("邮箱", "中国大陆手机号", "内网/公网 IP", "绝对盘符/挂载路径", "长数字串")


def _mask(s):
    """体检样例自身必须打码：只留首尾，中间用 *** 盖掉。"""
    s = str(s)
    if len(s) <= 4:
        return "*" * len(s)
    head = 2 if len(s) < 12 else 3
    return s[:head] + "***" + s[-2:]


def audit_atoms(limit_samples=5):
    """预览导出的脱敏情况，供面板展示「会带走什么、会隐去什么」。"""
    kb = _conn(KB)
    try:
        rows = kb.execute(
            "SELECT atom_id,title,body,summary,scope FROM atom WHERE state='active'").fetchall()
    finally:
        kb.close()
    hits = {}
    samples = []
    for r in rows:
        text = "\n".join(filter(None, [r["title"], r["body"], r["summary"], r["scope"]]))
        for label, pat in AUDIT_PATTERNS:
            found = pat.findall(text)
            if not found:
                continue
            hits.setdefault(label, {"count": 0, "examples": set()})
            hits[label]["count"] += len(found)
            for f in found[:3]:
                if len(hits[label]["examples"]) < limit_samples:
                    raw = f if isinstance(f, str) else " ".join(x for x in f if x)
                    hits[label]["examples"].add(_mask(raw))
            if label == "本地用户路径":
                samples.append({"atom_id": r["atom_id"], "title": r["title"][:80],
                                "example": _mask(found[0])})
    out = []
    for label, v in hits.items():
        out.append({"label": label, "count": v["count"],
                    "examples": sorted(v["examples"])[:limit_samples],
                    "action": "替换为 ~" if label == "本地用户路径"
                    else ("替换为 ***（严格模式；关闭严格则原样保留）"
                          if label in STRICT_EXTRA else "替换为 ***")})
    return {"atoms": len(rows), "findings": out,
            "path_samples": samples[:limit_samples]}


def _conn(path):
    return paths.db_connect(path, row_factory=sqlite3.Row)


def _sha(rows):
    h = hashlib.sha256()
    for r in rows:
        h.update(json.dumps(r, ensure_ascii=False, sort_keys=True).encode())
    return h.hexdigest()[:16]


def scrub_strict(text):
    """严格脱敏：在普通脱敏基础上，额外抹掉邮箱、手机号、IP、挂载路径、长数字串。"""
    text = scrub(text)
    if not isinstance(text, str):
        return text
    for label, pat in AUDIT_PATTERNS:
        if label in STRICT_EXTRA:
            text = pat.sub("***", text)
    return text


# ---------------------------------------------------------------- 导出自查
# 注意 atoms.jsonl 是 JSON 文本，换行会写成字面量 \n，
# 所以自查不能直接对原始文件跑正则，必须逐条解析后用语义正则。
_RESIDUAL = [
    ("本地用户路径", re.compile(r"/Users/[A-Za-z0-9_.-]+")),
    ("挂载路径", re.compile(r"/Volumes/[^\s\"'`]*")),
    ("密钥", re.compile(r"sk-[A-Za-z0-9_.\-]{20,}|ghp_[A-Za-z0-9]{20,}")),
    ("邮箱", re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@"
                      r"(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?![\w])")),
    ("手机号", re.compile(r"(?<![\d.])1[3-9]\d{9}(?![\d.])")),
    ("内网 IP", re.compile(
        r"(?<![\d.])(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
        r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})")),
]


def residual_check(rows):
    """逐条检查导出内容是否还残留敏感信息，返回 [{field,label,sample,atom}]。"""
    hits = []
    for r in rows:
        fields = [("title", r.get("title")), ("body", r.get("body")),
                  ("summary", r.get("summary")), ("scope", r.get("scope"))]
        fields += [(f"facet:{f['dim']}", f["value"]) for f in (r.get("facets") or [])]
        for name, text in fields:
            if not isinstance(text, str):
                continue
            for label, pat in _RESIDUAL:
                m = pat.search(text)
                if m:
                    hits.append({"field": name, "label": label,
                                 "sample": m.group(0)[:60],
                                 "atom": (r.get("title") or "")[:60]})
    return hits


# ---------------------------------------------------------------- 导出
def _default_attrs():
    return {k: bool(ATTR_DEFAULT[k]) for k in packs.ATTR_KEYS}


ATTR_DEFAULT = {a["key"]: a["default"] for a in packs.attr_catalog()}


def _attr_row(a, facets, sources, attrs, clean):
    """按属性开关组装一条导出知识：未勾选的属性按 `when_off` 处理。"""
    out = {"atom_id": a["atom_id"], "kind": a["kind"] if attrs.get("kind") else "fact",
           "title": clean(a["title"])}
    body_on, sum_on = attrs.get("body"), attrs.get("summary")
    out["body"] = clean(a["body"]) if body_on else ""
    out["summary"] = clean(a["summary"] or "") if sum_on else ""
    out["scope"] = clean(a["scope"] or "") if attrs.get("scope") else ""
    out["sensitivity"] = ((a.get("sensitivity") or "private")
                          if attrs.get("sensitivity") else "private")
    out["confidence"] = a["confidence"] if attrs.get("confidence") else 0.7
    out["veracity"] = a["veracity"] if attrs.get("confidence") else 0.7
    out["importance"] = a["static_importance"] if attrs.get("confidence") else 0.5
    out["trust_tier"] = a["trust_tier"] if attrs.get("trust") else 2
    out["half_life_days"] = a["half_life_days"]
    out["valid_from"] = a.get("valid_from") if attrs.get("validity") else None
    out["valid_until"] = a.get("valid_until") if attrs.get("validity") else None
    out["supersedes"] = a.get("supersedes") if attrs.get("validity") else None
    if attrs.get("created_at"):
        out["created_at"] = a["created_at"]
    else:
        # 只留日期，不泄露精确工作作息
        out["created_at"] = int(a["created_at"] or 0) // 86400 * 86400
    if attrs.get("fingerprint"):
        out["fingerprint"] = a["fingerprint"]
    if attrs.get("usage"):
        out["usage"] = a.get("usage") or {}

    keep = []
    for f in facets:
        dim = f["dim"]
        if dim == "project" and not attrs.get("facets_project"):
            continue
        if dim != "project" and not attrs.get("facets_general"):
            continue
        item = {"dim": dim, "value": clean(f["value"])}
        if attrs.get("facets_weight"):
            item["weight"] = f.get("weight")
            item["confidence"] = f.get("confidence")
        keep.append(item)
    out["facets"] = keep

    srcs = []
    if attrs.get("source_kind") or attrs.get("source_ref") or attrs.get("excerpt"):
        for s in sources:
            item = {}
            if attrs.get("source_kind"):
                item["kind"] = s["source_kind"]
            if attrs.get("source_ref"):
                item["ref"] = clean(s["source_ref"] or "")
            if attrs.get("excerpt"):
                item["excerpt"] = clean(s["excerpt"] or "")
            if attrs.get("raw_sha256"):
                item["raw_sha256"] = s["raw_sha256"]
            if item:
                srcs.append(item)
    out["sources"] = srcs
    return out


def export(name="my-worker", out=None, include_raw=False, with_profile=True,
           kinds=None, min_trust_tier=2, author="", strict=True,
           collections=None, attrs=None, description="", pack_id=None):
    """导出资源包。

    collections: 只导出这些知识集（默认取该包全部集）；空列表=该包全部知识
    attrs:       属性开关 {key: bool}，默认按 packs.ATTRS 的 default
    """
    packs.ensure()
    kb = _conn(KB)
    clean = scrub_strict if strict else scrub
    chosen = _default_attrs()
    if attrs:
        chosen.update({k: bool(v) for k, v in attrs.items() if k in chosen})

    # 校验：只能导出自己的包
    if pack_id:
        p = kb.execute("SELECT * FROM pack WHERE pack_id=?", (pack_id,)).fetchone()
        if not p:
            raise ValueError(f"包不存在：{pack_id}")
        if p["owner"] != "self":
            raise ValueError("他人导入的资源包只可使用，不可再导出")
    coll_rows = []
    if collections:
        ph = ",".join("?" * len(collections))
        coll_rows = kb.execute(
            f"SELECT * FROM pack_collection WHERE collection_id IN ({ph})",
            list(collections)).fetchall()
        for c in coll_rows:
            owner = kb.execute("SELECT owner FROM pack WHERE pack_id=?",
                               (c["pack_id"],)).fetchone()
            if owner and owner["owner"] != "self":
                raise ValueError(f"知识集「{c['name']}」属于他人导入的包，不可导出")
    else:
        pid = pack_id or packs.DEFAULT_PACK_ID
        coll_rows = kb.execute(
            "SELECT * FROM pack_collection WHERE pack_id=? ORDER BY order_no",
            (pid,)).fetchall()

    ids = packs.collection_atom_ids(kb, [c["collection_id"] for c in coll_rows])
    if not ids:
        kb.close()
        raise ValueError("选中的知识集里没有知识，先给知识集填充内容再导出")
    ph = ",".join("?" * len(ids))
    atoms = [dict(r) for r in kb.execute(
        f"SELECT * FROM atom WHERE atom_id IN ({ph}) AND state='active'"
        + (f" AND kind IN ({','.join('?' * len(kinds))})" if kinds else "")
        + " AND trust_tier<=? ORDER BY created_at",
        ids + (list(kinds) if kinds else []) + [min_trust_tier])]

    facets, sources = {}, {}
    for i in range(0, len(atoms), 400):
        chunk = [a["atom_id"] for a in atoms[i:i + 400]]
        mk = ",".join("?" * len(chunk))
        for r in kb.execute(
                f"SELECT af.atom_id, f.dim, f.value, af.weight, af.confidence"
                f" FROM atom_facet af JOIN facet f ON f.facet_id=af.facet_id"
                f" WHERE af.atom_id IN ({mk})", chunk):
            facets.setdefault(r["atom_id"], []).append(dict(r))
        for r in kb.execute(
                f"SELECT atom_id, source_kind, source_ref, excerpt, raw_sha256"
                f" FROM atom_source WHERE atom_id IN ({mk})", chunk):
            sources.setdefault(r["atom_id"], []).append(dict(r))

    # 使用反馈统计（被检索/引用/采纳次数），供他人判断哪些知识被验证过
    usage = {}
    for r in kb.execute(
            f"SELECT atom_id, event, COUNT(*) n FROM usage_event WHERE atom_id IN ({ph})"
            f" GROUP BY atom_id, event", ids):
        usage.setdefault(r["atom_id"], {})[r["event"]] = r["n"]

    rows = []
    for a in atoms:
        a = dict(a)
        a["usage"] = usage.get(a["atom_id"], {})
        rows.append(_attr_row(a, facets.get(a["atom_id"], []),
                              sources.get(a["atom_id"], []), chosen, clean))

    decisions = [{"question": r["title"], "verdict": r["kind"],
                  "answer": r["summary"] or r["body"][:200],
                  "confidence": r["confidence"], "atom_id": r["atom_id"]}
                 for r in rows
                 if r["kind"] in ("decision", "preference", "troubleshooting")
                 and r["trust_tier"] == 1][:500]

    coll_meta = [{"collection_id": c["collection_id"], "name": c["name"],
                  "description": c["description"],
                  "n_atoms": kb.execute(
                      "SELECT COUNT(*) FROM pack_atom pa JOIN atom a ON a.atom_id=pa.atom_id"
                      " WHERE pa.collection_id=? AND a.state='active'",
                      (c["collection_id"],)).fetchone()[0]}
                 for c in coll_rows]
    hidden = [{"key": k, "label": packs.ATTR_MAP[k][1], "when_off": packs.ATTR_MAP[k][4]}
              for k in packs.ATTR_KEYS if not chosen.get(k)]
    src_counter = {}
    for v in sources.values():
        for s in v:
            src_counter[s["source_kind"]] = src_counter.get(s["source_kind"], 0) + 1

    manifest = {
        "format": FORMAT,
        "name": name,
        "version": "1.0.0",
        "description": description or "可插拔的个人知识/决策资源包（cyber-worker）",
        "author": author or "anonymous",
        "owner": "self",
        "created_at": int(time.time()),
        "generator": "minime",
        "collections": coll_meta,
        "privacy": {
            "raw_archived": bool(include_raw),
            "redacted": True,
            "strict": bool(strict),
            "attrs": chosen,
            "hidden": hidden,
            "note": ("严格脱敏：路径→~，密钥/令牌/邮箱/手机号/IP→***"
                     if strict else "基础脱敏：路径→~，密钥/令牌→***"),
        },
        "coverage": {
            "atoms": len(rows), "decisions": len(decisions),
            "kinds": _count(rows, "kind"),
            "sensitivity": _count(rows, "sensitivity"),
            "sources": src_counter if chosen.get("source_kind") else {},
        },
        "capabilities": sorted({r["kind"] for r in rows}),
        "checksum": _sha(rows),
    }
    if with_profile:
        try:
            import profile as P  # noqa: PLC0415
            _sid, payload = P.build(kb)
            manifest["profile"] = {
                "dimensions": payload.get("dimensions"),
                "projects": list((payload.get("projects") or {}).keys())[:30]
                if chosen.get("facets_project") else [],
                "techs": list((payload.get("techs") or {}).keys())[:30],
            }
        except Exception:  # noqa: BLE001
            pass

    hits = residual_check(rows)
    manifest["privacy"]["residual_risk"] = sorted({h["label"] for h in hits})
    manifest["privacy"]["residual_samples"] = hits[:10]

    out = out or os.path.join(paths.PACKS, f"{name}.minime.zip")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        z.writestr("collections.json", json.dumps(coll_meta, ensure_ascii=False, indent=2))
        z.writestr("atoms.jsonl", "\n".join(json.dumps(r, ensure_ascii=False) for r in rows))
        z.writestr("decisions.jsonl", "\n".join(json.dumps(r, ensure_ascii=False)
                                                for r in decisions))
        if include_raw:
            m = _conn(MANIFEST)
            raw = [{"sha256": r["sha256"], "kind": r["kind"],
                    "size": r["size"], "created_at": r["created_at"]}
                   for r in m.execute("SELECT * FROM raw_object")]
            z.writestr("sources.json", json.dumps({"raw_objects": raw,
                                                   "note": "仅元信息，原文需另附"},
                                                  ensure_ascii=False, indent=2))
    kb.close()
    return {"pack": out, "size_kb": round(os.path.getsize(out) / 1024, 1),
            "atoms": len(rows), "decisions": len(decisions),
            "collections": coll_meta,
            "privacy": manifest["privacy"], "strict": bool(strict),
            "hidden_attrs": [h["label"] for h in hidden]}


def _count(rows, key):
    out = {}
    for r in rows:
        out[r[key]] = out.get(r[key], 0) + 1
    return out


# ---------------------------------------------------------------- inspect
def inspect(path):
    with zipfile.ZipFile(path) as z:
        manifest = json.loads(z.read("manifest.json"))
        names = z.namelist()
    return {"manifest": manifest, "files": names,
            "size_kb": round(os.path.getsize(path) / 1024, 1)}


# ---------------------------------------------------------------- import
def import_pack(path, dry_run=True, conflict="merge", enable=False):
    """导入资源包：落成独立资源包 + 包内知识集，默认关闭（由用户选择开启）。

    导入的包 owner='imported'，不可再导出、不可编辑；与他人知识重复时保留两份。
    """
    with zipfile.ZipFile(path) as z:
        manifest = json.loads(z.read("manifest.json"))
        if not str(manifest.get("format", "")).startswith("minime-pack/"):
            raise ValueError(f"不支持的包格式: {manifest.get('format')}")
        atoms = [json.loads(ln) for ln in z.read("atoms.jsonl").decode().split("\n")
                 if ln.strip()]
        colls = []
        if "collections.json" in z.namelist():
            colls = json.loads(z.read("collections.json"))
        elif manifest.get("collections"):
            colls = manifest["collections"]
    if dry_run:
        return {"dry_run": True, "pack": manifest.get("name"),
                "author": manifest.get("author"), "created_at": manifest.get("created_at"),
                "atoms": len(atoms), "kinds": _count(atoms, "kind"),
                "collections": colls, "privacy": manifest.get("privacy"),
                "conflict": conflict}

    from normalize import HALF_LIFE, Writer  # noqa: PLC0415
    packs.ensure()
    kb = _conn(KB)
    now = int(time.time())
    base = re.sub(r"[^\w\-]+", "-", str(manifest.get("name") or "imported")).strip("-")
    pid = f"P:imp-{base or 'pack'}"
    n = 1
    while kb.execute("SELECT 1 FROM pack WHERE pack_id=?", (pid,)).fetchone():
        n += 1
        pid = f"P:imp-{base or 'pack'}-{n}"
    kb.execute(
        "INSERT INTO pack(pack_id,name,description,author,owner,origin,version,"
        "created_at,updated_at) VALUES (?,?,?,?,'imported',?,?,?,?)",
        (pid, f"[导入] {manifest.get('name') or base}", manifest.get("description") or "",
         manifest.get("author") or "", os.path.basename(path),
         manifest.get("version") or "1.0.0", manifest.get("created_at") or now, now))
    coll_map = {}
    for i, c in enumerate(colls or [{"name": "导入内容"}]):
        cid = f"C:{pid[2:]}:{i + 1}"
        old = c.get("collection_id") or c.get("name")
        coll_map[old] = {"collection_id": cid, "name": c.get("name") or f"知识集 {i + 1}"}
        kb.execute(
            "INSERT INTO pack_collection(collection_id,pack_id,name,description,enabled,"
            "n_atoms,order_no,created_at) VALUES (?,?,?,?,?,0,?,?)",
            (cid, pid, coll_map[old]["name"], c.get("description") or "",
             1 if enable else 0, i, now))
    default_cid = next(iter(coll_map.values()))["collection_id"]
    kb.commit()

    w = Writer.__new__(Writer)          # 只借用写方法，不清空本地知识库
    w.c = kb
    w.full = False                      # 增量写入（T() 直接落正式表）
    w.manifest = paths.db_connect(MANIFEST)
    w.stats = {"atom": 0, "dup": 0, "source": 0, "facet": 0}
    imported = merged = 0
    for a in atoms:
        if a.get("kind") not in HALF_LIFE:
            continue
        facets = []
        for f in a.get("facets") or []:
            if not f.get("dim") or not f.get("value"):
                continue
            facets.append(w.put_facet(f["dim"], f["value"]))
        if conflict == "skip":
            from normalize import fingerprint  # noqa: PLC0415
            fp = fingerprint(a["kind"], a["title"])
            if kb.execute("SELECT 1 FROM atom WHERE fingerprint=?", (fp,)).fetchone():
                merged += 1
                continue
        # 标题加包名前缀：与他人同名知识共存，不因指纹相同而被合并丢失
        title = f"[{manifest.get('name') or base}] {a['title']}"
        aid = w.add_atom(
            kind=a["kind"], title=title, body=a.get("body") or "",
            summary=a.get("summary"), scope=a.get("scope"),
            confidence=a.get("confidence") or 0.7,
            veracity=min(0.99, a.get("veracity") or 0.7),
            importance=a.get("importance") or 0.5, trust_tier=2,
            raw_sha=None, source_kind="imported_pack",
            source_ref=f"{manifest.get('name')}@{a.get('atom_id')}",
            excerpt=(a.get("summary") or "")[:400], facets=facets)
        s = w.c.execute("SELECT sensitivity FROM atom WHERE atom_id=?", (aid,)).fetchone()
        if not s or not s[0]:
            w.c.execute("UPDATE atom SET sensitivity=? WHERE atom_id=?",
                        (a.get("sensitivity") or "internal", aid))
        kb.execute(
            "INSERT OR IGNORE INTO atom_source(atom_id,raw_sha256,source_kind,"
            "source_ref,excerpt) VALUES (?,?,?,?,?)",
            (aid, manifest.get("checksum", ""), "imported_pack",
             f"{manifest.get('name')}@{a.get('atom_id')}",
             (a.get("summary") or "")[:400]))
        kb.execute("INSERT OR IGNORE INTO pack_atom(collection_id,atom_id,added_at)"
                   " VALUES (?,?,?)", (default_cid, aid, now))
        imported += 1
    kb.commit()
    w.rebuild_fts_tags()
    packs.refresh_counts(kb)
    kb.commit()
    n_atoms = kb.execute("SELECT COUNT(*) FROM atom").fetchone()[0]
    kb.close()
    return {"imported": imported, "merged": merged, "total_atoms": n_atoms,
            "pack_id": pid, "pack": manifest.get("name"),
            "collections": list(coll_map.values()),
            "enabled": bool(enable)}


# ---------------------------------------------------------------- 包文件
PACKS_DIR = paths.PACKS


def list_files():
    os.makedirs(PACKS_DIR, exist_ok=True)
    items = []
    for fn in sorted(os.listdir(PACKS_DIR)):
        if not fn.endswith(".minime.zip"):
            continue
        full = os.path.join(PACKS_DIR, fn)
        item = {"file": fn, "size_kb": round(os.path.getsize(full) / 1024, 1),
                "mtime": int(os.path.getmtime(full))}
        try:
            with zipfile.ZipFile(full) as z:
                m = json.loads(z.read("manifest.json"))
            item.update({"name": m.get("name"), "author": m.get("author"),
                         "atoms": (m.get("coverage") or {}).get("atoms"),
                         "collections": len(m.get("collections") or []),
                         "created_at": m.get("created_at")})
        except Exception:  # noqa: BLE001
            item["name"] = fn
        items.append(item)
    return items


def main():
    argv = sys.argv[1:]
    cmd = argv[0] if argv else ""
    def opt(flag, default=None):
        return argv[argv.index(flag) + 1] if flag in argv else default
    if cmd == "export":
        attrs = None
        if opt("--attrs"):
            attrs = {k: k in opt("--attrs").split(",") for k in packs.ATTR_KEYS}
        out = export(name=opt("--name", "my-worker"), out=opt("--out"),
                     include_raw="--include-raw" in argv,
                     with_profile="--no-profile" not in argv,
                     author=opt("--author", ""),
                     collections=[x for x in (opt("--collections") or "").split(",") if x],
                     attrs=attrs,
                     strict="--loose" not in argv)
        print(json.dumps(out, ensure_ascii=False, indent=2))
    elif cmd == "audit":
        print(json.dumps(audit_atoms(), ensure_ascii=False, indent=2))
    elif cmd == "attrs":
        print(json.dumps(packs.attr_catalog(), ensure_ascii=False, indent=2))
    elif cmd == "inspect":
        print(json.dumps(inspect(argv[1]), ensure_ascii=False, indent=2))
    elif cmd == "import":
        print(json.dumps(import_pack(argv[1],
                                     dry_run="--dry-run" in argv,
                                     conflict=opt("--conflict", "merge"),
                                     enable="--enable" in argv),
                         ensure_ascii=False, indent=2))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
