"""适配器：Trae 记忆文件（~/.trae/memory）。

数据形态：projects/<repo>/<date>/{project_memory.md,topics.md,session_memory_*.jsonl}
          user_profile.md
采集粒度：文件级（内容寻址，未变文件自动跳过）
"""
import os
import sys

# collector/ 是 base 框架所在层；上溯三级，直接运行本文件也能调试
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
from base import MEMORY, SourceAdapter, put_raw, register  # noqa: E402


@register
class MemoryAdapter(SourceAdapter):
    name = "trae_memory"
    kind = "trae_memory"
    DEFAULT_INTERVAL_S = 1800
    priority = 40
    description = "Trae 清洗后的记忆文件（project_memory / topics / session_memory）"
    label = "Trae 记忆"
    fix_hint = "在 Trae 中产生记忆文件（~/.trae/memory）后可采集；否则可关闭此采集源"

    def probe(self):
        if not os.path.isdir(MEMORY):
            return False, f"目录不存在：{MEMORY}"
        return True, MEMORY

    def files(self):
        for dirpath, _dirs, files in os.walk(MEMORY):
            for fn in files:
                if fn.endswith((".md", ".jsonl", ".json")):
                    yield os.path.join(dirpath, fn)

    def collect(self):
        count = 0
        for full in self.files():
            with open(full, "rb") as f:
                data = f.read()
            if data:
                put_raw(self.conn, self.kind, os.path.relpath(full, MEMORY), data)
                count += 1
        return {"memory_files": count}
