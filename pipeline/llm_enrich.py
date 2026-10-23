"""LLM 二次归纳：把「结构性事件」（文件改了什么）提炼成「决策/偏好/教训」。

开关：config.json -> llm.enabled，或环境变量 MINIME_LLM_ENABLED=1。
未启用时本阶段直接跳过，不影响主流水线。

输入：turn（本轮改了哪些文件、增删行）+ 可选的用户原话 + 现存相关知识
输出：新 atom（kind=decision/preference/troubleshooting/pattern/insight），
      或对已有 atom 的强化（supersede / 提升 veracity）。
"""
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
import paths  # noqa: E402
import schema  # noqa: E402
from normalize import (HALF_LIFE, Writer, bigrams, detect_scene, detect_tech,  # noqa: E402
                       fingerprint, write_llm_atom)
from retrieval import connect, record_feedback  # noqa: E402

MANIFEST = paths.MANIFEST
INDEX = paths.INDEX
RAW = paths.RAW

SYSTEM = """你是个人知识库的归纳器。给你一段工程师与 AI 编程助手的工作片段（改了哪些文件、增删多少行、可能的用户原话），
请提炼其中**可复用的知识**，而不是复述做了什么。

输出严格 JSON：{"atoms": [ ... ]}，每个元素：
{"title": "一句话结论（<=60字，必须可独立阅读）",
 "body": "补充说明：为什么这么做、约束是什么、如何验证（<=300字）",
 "kind": "decision|preference|fact|troubleshooting|pattern|insight",
 "scope": "global 或 project:<项目名>",
 "confidence": 0~1,
 "importance": 0~1,
 "tech": ["涉及技术"],
 "evidence_quote": "支撑该结论的原文片段（必须逐字来自输入，不得编造）"}

规则：
- 只提炼有复用价值的结论；日常琐碎改动输出 {"atoms": []}
- evidence_quote 必须能在输入原文里找到；找不到就不要输出该条
- 宁少勿滥，一次最多 3 条
"""


def llm_chat(cfg, messages, timeout=None):
    c = cfg["llm"]
    url = c["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": c["model"],
        "messages": messages,
        "temperature": c.get("temperature", 0.2),
        "stream": False,
        "max_tokens": c.get("max_tokens", 2048),
    }
    # 推理型模型（Qwen3 等）会先产出 reasoning_content，归纳任务关掉思考更稳更快
    if c.get("disable_thinking", True):
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {c.get('api_key') or 'none'}",
    }, method="POST")
    with urllib.request.urlopen(req, timeout=timeout or c.get("timeout_s", 120)) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    msg = data["choices"][0]["message"]
    return msg.get("content") or msg.get("reasoning_content") or ""


def parse_json_block(text):
    """从模型输出里抠出第一个 JSON 对象（兼容 ```json 包裹）。"""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start = text.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i, ch in enumerate(text[start:], start):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except ValueError:
                    return None
    return None


def health(cfg):
    """探测本地模型是否可用。"""
    try:
        url = cfg["llm"]["base_url"].rstrip("/") + "/models"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {cfg['llm'].get('api_key') or 'none'}"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode())
        ids = [m.get("id") for m in data.get("data", [])]
        return {"ok": True, "models": ids, "using": cfg["llm"]["model"]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def pick_targets(conn, cfg, limit):
    """挑还没被 LLM 归纳过的、有实质改动的轮次（大改动 + 最近优先）。"""
    tdb = sqlite3.connect(MANIFEST)
    tdb.row_factory = sqlite3.Row
    done = {r[0] for r in conn.execute("SELECT source_ref FROM llm_cursor")}
    rows = tdb.execute("""
        SELECT t.turn_id, t.session_id, t.ts, t.n_files, t.n_added, t.n_modified,
               t.additions, t.deletions
        FROM turn t
        WHERE (t.additions + t.deletions) >= 5 OR t.n_added >= 2
        ORDER BY (t.additions + t.deletions) DESC, t.ts DESC""").fetchall()
    targets = []
    for r in rows:
        ref = f"{r['session_id']}/{r['turn_id']}"
        if ref in done:
            continue
        files = [dict(x) for x in tdb.execute(
            "SELECT path,action,additions,deletions FROM turn_file"
            " WHERE turn_id=? LIMIT 12", (r["turn_id"],))]
        targets.append({**dict(r), "ref": ref, "files": files})
        if limit and len(targets) >= limit:  # limit 为空表示不限量（归纳全部）
            break
    tdb.close()
    return targets


def project_of_session(session_id):
    try:
        db = sqlite3.connect(INDEX)
        row = db.execute("""SELECT w.name FROM session s JOIN workspace w
                            ON w.workspace_id=s.workspace_id WHERE s.session_id=?""",
                         (session_id,)).fetchone()
        db.close()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def build_prompt(target, project):
    lines = [f"项目：{project or 'unknown'}",
             f"这一轮改动：新增 {target['n_added']} / 修改 {target['n_modified']} 个文件，"
             f"+{target['additions']}/-{target['deletions']} 行"]
    for f in target["files"][:10]:
        lines.append(f"- [{f['action']}] {f['path']} (+{f['additions']}/-{f['deletions']})")
    # 附上被改文件的开头，让模型看到真实内容而不是只有文件名
    snippets = []
    for f in target["files"][:3]:
        path = os.path.join(paths.SNIPPETS, f["path"].replace("/", "_")[:120])
        if os.path.exists(path):
            snippets.append(f"--- {f['path']} 开头 ---\n" + open(
                path, encoding="utf-8", errors="replace").read()[:1200])
    return "\n".join(lines) + ("\n\n" + "\n\n".join(snippets) if snippets else "")


def ingest(w, conn, cfg, targets):
    created = skipped = errors = 0
    batch = []
    for t in targets:
        project = project_of_session(t["session_id"])
        prompt = build_prompt(t, project)
        batch.append((t, project, prompt))
        if len(batch) < cfg["llm"]["batch_size"]:
            continue
        created, skipped, errors = _run_batch(w, conn, cfg, batch, created, skipped, errors)
        batch = []
    if batch:
        created, skipped, errors = _run_batch(w, conn, cfg, batch, created, skipped, errors)
    return {"created": created, "skipped": skipped, "errors": errors}


def _run_batch(w, conn, cfg, batch, created, skipped, errors):
    for t, project, prompt in batch:
        try:
            reply = llm_chat(cfg, [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt},
            ])
        except (urllib.error.URLError, KeyError, ValueError, OSError) as e:
            errors += 1
            print(f"[llm] {t['ref']} failed: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        data = parse_json_block(reply) or {}
        atoms = data.get("atoms") or []
        if not atoms:
            skipped += 1
            _mark_done(conn, t, 0)   # 无结论也记进度，避免每轮重试同一批
            continue
        lower = prompt.lower()
        kept = 0
        for a in atoms[:3]:
            quote = (a.get("evidence_quote") or "").strip()
            # 反幻觉：引用必须真的出现在输入里
            if quote and quote[:60].lower() not in lower:
                skipped += 1
                continue
            kind = a.get("kind") if a.get("kind") in HALF_LIFE else "insight"
            body = (a.get("body") or "").strip()
            title = (a.get("title") or "").strip()
            if not title or not body:
                skipped += 1
                continue
            payload = {
                "title": title, "body": body, "kind": kind,
                "scope": a.get("scope") or (f"project:{project}" if project else None),
                "confidence": float(a.get("confidence") or 0.7),
                "importance": float(a.get("importance") or 0.6),
                "tech": [x for x in (a.get("tech") or []) if isinstance(x, str)],
                "evidence_quote": quote or prompt[:400],
                "project": project,
            }
            # 先落持久表：normalize 重建 atom 后由 replay_llm_atoms 回放
            conn.execute(
                "INSERT OR REPLACE INTO llm_atom VALUES (?,?,?,?)",
                (fingerprint(payload["kind"], title), t["ref"],
                 json.dumps(payload, ensure_ascii=False), int(time.time())))
            write_llm_atom(w, payload, project, t["ref"])
            kept += 1
            created += 1
        _mark_done(conn, t, kept)
    conn.commit()
    return created, skipped, errors


def _mark_done(conn, target, n_atoms):
    """记录已归纳，避免重复跑（normalize 重建 atom 时不影响本表）。"""
    conn.execute("INSERT OR REPLACE INTO llm_cursor VALUES (?,?,?)",
                 (target["ref"], n_atoms, int(time.time())))


def ensure_schema(conn):
    """兜底建表（schema.init 已含，这里防止旧库缺表）。"""
    conn.execute("""CREATE TABLE IF NOT EXISTS llm_atom (
        fingerprint TEXT PRIMARY KEY,
        source_ref  TEXT NOT NULL,
        payload     TEXT NOT NULL,
        created_at  INTEGER NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS llm_cursor (
        source_ref TEXT PRIMARY KEY,
        atoms      INTEGER DEFAULT 0,
        processed_at INTEGER NOT NULL)""")
    conn.commit()


def run(force=False):
    cfg = config.load()
    if not cfg["llm"]["enabled"] and not force:
        return {"skipped": "llm.enabled=false（在 config.json 或 MINIME_LLM_ENABLED=1 打开）"}
    h = health(cfg)
    if not h["ok"]:
        return {"skipped": "LLM 不可达", "detail": h}
    conn = schema.init()  # 确保 atom/facet/llm_atom/llm_cursor 齐全
    # 增量模式：只往现有知识库里补几条，绝不能用重建模式的 Writer（会整表替换掉其余知识）
    w = Writer(conn, full=False)
    targets = pick_targets(conn, cfg, cfg["llm"]["max_atoms_per_run"])
    if not targets:
        return {"targets": 0, "note": "没有待归纳的新轮次"}
    res = ingest(w, conn, cfg, targets)
    w.rebuild_fts_tags()
    conn.commit()
    return {"targets": len(targets), **res}


if __name__ == "__main__":
    print(json.dumps(run(force="--force" in sys.argv), ensure_ascii=False, indent=1))
