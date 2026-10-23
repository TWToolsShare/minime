"""本机文件通道：读取用户自己的目录与笔记库。

  docs.py      本地文档/笔记（目录与关键词过滤在 config.collect.local 配置）
  obsidian.py  Obsidian vault（frontmatter / 双链 / 标签）

import 本包即完成注册。
"""
from . import docs, obsidian  # noqa: F401