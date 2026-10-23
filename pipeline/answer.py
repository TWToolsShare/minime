"""关键词规则问答：不调用任何模型，直接用检索结果拼答案。
回答模式 answer.mode=rules 时由 chat.ask / minime_decide 使用：
检索强制只走关键词（不用向量），命中的知识按评分排序后原样摘录成答案，
并保留引用编号，让「结论可追溯」这条契约和模型作答时一致。
"""
import re

TOP_K = 5


def _cite(items):
    return " ".join(f"[{i}]" for i in range(1, len(items) + 1))


def compose(question, hits, top_k=TOP_K):
    """把检索命中拼成规则答案。返回 {answer, refs, sufficient}。

    hits 为 retrieval.search 的返回结构；不足 3 条时视为证据偏少。
    """
    items = []
    for h in hits[:top_k]:
        a = h["atom"]
        items.append({
            "atom_id": a["atom_id"], "title": a["title"], "kind": a["kind"],
            "score": h["score"], "dormant": bool(h.get("dormant")),
            "body": (a["summary"] or a["body"] or "").strip(),
        })
    if not items:
        return {
            "answer": "知识库里没有检索到与这个问题相关的内容。可以换个说法，"
                      "或先在设置里调整为模型作答。",
            "refs": [], "sufficient": False,
        }
    lines = []
    for i, it in enumerate(items, 1):
        text = it["body"] or it["title"]
        if len(text) > 200:
            text = text[:200].rstrip() + "…"
        mark = "（曾降权）" if it["dormant"] else ""
        lines.append(f"{i}. {it['title']}{mark}\n   {text}  [{i}]")
    answer = ("按关键词规则匹配到以下知识（未经过模型总结）：\n\n"
              + "\n".join(lines))
    return {"answer": answer, "refs": items, "sufficient": len(items) >= 3}
