"""长文本摘要：仅在 LLM 启用时使用，结果按内容哈希缓存。

调用方（normalize）对超过阈值的正文调用 summary()：
  - LLM 未启用：返回正文前 160 字（正文仍全文入库）
  - LLM 启用：生成不超过 200 字的摘要；失败或超出本次预算时退回前 160 字
"""
import hashlib
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SYSTEM = """你是知识库编辑。把用户提供的文本压缩成不超过 200 字的中文摘要。
要求：
- 只保留结论、约束、关键事实和数字
- 不复述修改过程，不添加原文没有的信息
- 直接输出摘要正文，不要标题、前缀或 Markdown 标记"""


class Summarizer:
    def __init__(self, conn, cfg):
        self.conn = conn
        self.llm = (cfg or {}).get("llm") or {}
        # 定时任务用 --no-llm 时，摘要也必须停，否则会拖住清洗并长时间占用写锁
        self.enabled = bool(self.llm.get("enabled")) and not os.environ.get(
            "MINIME_SUMMARY_DISABLED")
        self.threshold = int(self.llm.get("summary_threshold") or 1200)
        self.budget = int(self.llm.get("summary_max_per_run") or 0)
        self.model = self.llm.get("model") or ""
        self.used = 0          # 本次实际调用模型的次数
        self.cached = 0        # 命中缓存的次数
        self.failed = 0
        self._fails = 0
        self._ensure()

    def _ensure(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS summary_cache(
              sig        TEXT PRIMARY KEY,
              model      TEXT NOT NULL,
              summary    TEXT NOT NULL,
              created_at INTEGER NOT NULL)""")
        self.conn.commit()

    def _sig(self, text):
        raw = f"{self.model}\0{text}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def summary(self, text):
        body = (text or "").strip()
        if not body:
            return ""
        if len(body) <= self.threshold:
            return body[:160]
        sig = self._sig(body)
        row = self.conn.execute(
            "SELECT summary FROM summary_cache WHERE sig=?", (sig,)).fetchone()
        if row:
            self.cached += 1
            return row[0]
        # 未启用、预算用完或连续失败时都不调模型；有缓存则已在上面复用，
        # 这样 --no-llm 的定时清洗不会覆盖此前生成的 LLM 摘要
        if not self.enabled or (self.budget and self.used >= self.budget):
            return body[:160]
        if self._fails >= 3:           # 连续失败就当模型不可用，本轮回退
            return body[:160]
        self.used += 1
        try:
            # 调模型前先放开写锁，避免几十秒的网络等待卡住面板或其他流水线
            self.conn.commit()
            out = self._call(body[:8000])
        except Exception:  # noqa: BLE001
            self.failed += 1
            self._fails += 1
            return body[:160]
        self._fails = 0
        out = (out or "").strip().strip('"“”')
        if not out:
            return body[:160]
        out = out[:400]
        self.conn.execute(
            "INSERT OR REPLACE INTO summary_cache(sig,model,summary,created_at)"
            " VALUES (?,?,?,?)", (sig, self.model, out, int(time.time())))
        self.conn.commit()
        return out

    def _call(self, text):
        import llm_enrich as L  # noqa: PLC0415 — 延迟导入，避免与 normalize 循环依赖
        timeout = min(int(self.llm.get("timeout_s") or 120), 90)
        return L.llm_chat({"llm": self.llm}, [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": text},
        ], timeout=timeout)

    def stats(self):
        return {"enabled": self.enabled, "threshold": self.threshold,
                "used": self.used, "cached": self.cached, "failed": self.failed}
