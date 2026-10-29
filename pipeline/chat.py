"""基于知识库的多轮对话。

流程（RAG）：用户提问 -> 用现有检索打分取最相关知识 -> 作为上下文交给本地模型
-> 模型必须依据给定知识回答，并标注引用编号 -> 存进对话历史。

历史对话落 store/chat.db（与 kb.db 分开，避免重建知识库时受影响）：
  session(id, title, created_at, updated_at, model, n_turns)
  message(id, session_id, role, content, refs, created_at, elapsed_s)
refs 是消息引用的 atom_id 列表，前端据此渲染可点击的来源。

不依赖向量库：检索复用 pipeline/retrieval.py 的六项加权打分，与 Web/MCP 结论一致。
"""
import json
import os
import re
import sqlite3
import sys
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paths  # noqa: E402

CHAT_DB = paths.CHAT
TOP_K = 8              # 每次检索带进上下文的知识条数
HISTORY_TURNS = 6      # 多轮对话保留的最近轮数
SNIPPET = 700          # 单条知识送进模型的字数上限

SYSTEM = """你是用户的个人知识分身。你只依据下面提供的「知识片段」回答，并遵循：

1. 能用知识片段回答的，直接给结论，不要复述问题、不要客套。
2. 每个结论后用 [1] [2] 标注依据的片段编号，编号必须是给定的。
3. 知识片段不足以回答时，明确说「知识库里没有这条」，再给基于通用经验的建议，并标注「（通用经验）」。
4. 有相互矛盾的知识时，指出冲突并说明哪条证据更强（可信度/更新时间）。
5. 回答用中文，简洁，不超过 400 字；必要时用短列表。

知识片段：
{context}
"""


def connect():
    paths.ensure()
    c = paths.db_connect(CHAT_DB, row_factory=sqlite3.Row)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS session (
      id         TEXT PRIMARY KEY,
      title      TEXT NOT NULL,
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL,
      model      TEXT,
      n_turns    INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS message (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      session_id TEXT NOT NULL,
      role       TEXT NOT NULL,      -- user|assistant
      content    TEXT NOT NULL,
      refs       TEXT,               -- JSON: [{"atom_id","title","kind","score"}]
      created_at INTEGER NOT NULL,
      elapsed_s  REAL
    );
    CREATE INDEX IF NOT EXISTS idx_msg_session ON message(session_id, id);
    CREATE INDEX IF NOT EXISTS idx_session_updated ON session(updated_at DESC);
    """)
    c.commit()
    return c


# ---------------------------------------------------------------- 会话
def list_sessions(limit=50):
    c = connect()
    try:
        rows = c.execute("SELECT * FROM session ORDER BY updated_at DESC LIMIT ?",
                         (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()


def get_session(sid):
    c = connect()
    try:
        s = c.execute("SELECT * FROM session WHERE id=?", (sid,)).fetchone()
        if not s:
            return None
        msgs = []
        for m in c.execute("SELECT * FROM message WHERE session_id=? ORDER BY id", (sid,)):
            d = dict(m)
            d["refs"] = json.loads(d["refs"]) if d.get("refs") else []
            msgs.append(d)
        return {**dict(s), "messages": msgs}
    finally:
        c.close()


def new_session(title="新对话", model=None):
    sid = "S" + uuid.uuid4().hex[:12]
    now = int(time.time())
    c = connect()
    try:
        c.execute("INSERT INTO session(id,title,created_at,updated_at,model) VALUES (?,?,?,?,?)",
                  (sid, title[:60], now, now, model))
        c.commit()
        return sid
    finally:
        c.close()


def rename_session(sid, title):
    c = connect()
    try:
        c.execute("UPDATE session SET title=? WHERE id=?", (title[:60], sid))
        c.commit()
    finally:
        c.close()


def delete_session(sid):
    c = connect()
    try:
        c.execute("DELETE FROM message WHERE session_id=?", (sid,))
        c.execute("DELETE FROM session WHERE id=?", (sid,))
        c.commit()
    finally:
        c.close()


# ---------------------------------------------------------------- 检索 + 回答
def _context(query, top_k=TOP_K, use_vectors=True):
    import retrieval as R  # noqa: PLC0415
    conn = R.connect()
    try:
        # 命中不足时把「降权」知识也召回：冷门知识不能因为两次误标就彻底消失
        hits = R.search(conn, query, limit=top_k, include_dormant=True,
                        use_vectors=use_vectors)
        out = []
        for h in hits:
            a = h["atom"]
            out.append({
                "atom_id": a["atom_id"], "title": a["title"], "kind": a["kind"],
                "scope": a["scope"], "score": h["score"],
                "dormant": bool(h.get("dormant")),
                "confidence": a["confidence"], "veracity": a["veracity"],
                "body": (a["body"] or "")[:SNIPPET],
            })
        return out
    finally:
        conn.close()


def _format_context(refs):
    if not refs:
        return "（没有检索到相关知识）"
    parts = []
    for i, r in enumerate(refs, 1):
        mark = " / 曾降权" if r.get("dormant") else ""
        parts.append(f"[{i}] ({r['kind']} / 可信 {r['veracity']:.2f} / 相关度 {r['score']:.2f}{mark}) "
                     f"{r['title']}\n{r['body']}")
    return "\n\n".join(parts)


def _history_msgs(sid, limit=HISTORY_TURNS):
    s = get_session(sid)
    if not s:
        return []
    msgs = [m for m in s["messages"] if m["role"] in ("user", "assistant")]
    return [{"role": m["role"], "content": m["content"]} for m in msgs[-limit * 2:]]


def ask(sid, question, top_k=TOP_K):
    """一轮问答：检索 -> 组装 -> 作答（模型或关键词规则）-> 落库。返回 assistant 消息。"""
    import llm_enrich as L  # noqa: PLC0415
    import retrieval as R  # noqa: PLC0415
    cfg = L.config.load()
    mode = R.config.answer_mode(cfg)
    t0 = time.time()

    if mode == "llm":
        h = L.health(cfg)
        if not h["ok"]:
            raise RuntimeError(f"本地模型不可达：{h.get('error')}。"
                               f"检查设置页「接口地址」，或先启动本地模型服务")
        refs = _context(question, top_k)
        system = SYSTEM.replace("{context}", _format_context(refs))
        messages = [{"role": "system", "content": system}]
        messages += _history_msgs(sid)
        messages.append({"role": "user", "content": question})
        reply = L.llm_chat(cfg, messages, timeout=180)
        if not reply.strip():
            raise RuntimeError("模型返回为空，可能是超时或上下文过长")
        used = _extract_refs(reply, refs)
    else:
        # 规则作答：不加载模型，检索也只走关键词，整轮纯本地完成
        import answer as A  # noqa: PLC0415
        conn = R.connect()
        try:
            hits = R.search(conn, question, limit=top_k, include_dormant=True,
                            use_vectors=False)
        finally:
            conn.close()
        r = A.compose(question, hits, top_k)
        reply, used = r["answer"], r["refs"]
    elapsed = round(time.time() - t0, 1)
    now = int(time.time())
    c = connect()
    try:
        if not c.execute("SELECT 1 FROM session WHERE id=?", (sid,)).fetchone():
            c.execute("INSERT INTO session(id,title,created_at,updated_at) VALUES (?,?,?,?)",
                      (sid, question[:40], now, now))
        c.execute("INSERT INTO message(session_id,role,content,refs,created_at)"
                  " VALUES (?,?,?,?,?)", (sid, "user", question, None, now))
        cur = c.execute(
            "INSERT INTO message(session_id,role,content,refs,created_at,elapsed_s)"
            " VALUES (?,?,?,?,?,?)",
            (sid, "assistant", reply, json.dumps(used, ensure_ascii=False), now, elapsed))
        # 首轮用问题当标题；之后只更新计数与时间
        c.execute("""UPDATE session SET updated_at=?,
                     n_turns=n_turns+1,
                     title=CASE WHEN n_turns=0 THEN ? ELSE title END
                     WHERE id=?""", (now, question[:40], sid))
        c.commit()
        mid = cur.lastrowid
    finally:
        c.close()

    # 被引用的知识记一次 cited，参与「越用越好用」的权重
    if used:
        conn = R.connect()
        try:
            for r in used:
                R.record_feedback(conn, r["atom_id"], "cited", question[:200], surface="chat")
        finally:
            conn.close()
    return {"id": mid, "role": "assistant", "content": reply, "refs": used,
            "created_at": now, "elapsed_s": elapsed}


def _extract_refs(reply, refs):
    """只保留回答里真正引用到的片段（按 [n] 标记），避免把整批都算作引用。"""
    nums = {int(x) for x in re.findall(r"\[(\d{1,2})\]", reply)}
    out = []
    for n in sorted(nums):
        if 1 <= n <= len(refs):
            r = refs[n - 1]
            out.append({k: r[k] for k in ("atom_id", "title", "kind", "score")})
    return out


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "我的 macos 兼容性经验"
    sid = new_session()
    r = ask(sid, q)
    print(f"session {sid} · {r['elapsed_s']}s · refs {len(r['refs'])}")
    print(r["content"])
