"""AI 助手通道：WorkBuddy / WorkBuddy AI、Qoder、豆包（Doubao）。

  workbuddy.py  本地会话 JSONL + 记忆/身份文件 + sessions 元数据
  qoder.py      main.sqlite 里的会话与消息
  doubao.py     豆包导出 JSON / 本地 SQLite / 导出文本

import 本包即完成注册。
"""
from . import doubao, qoder, workbuddy  # noqa: F401
