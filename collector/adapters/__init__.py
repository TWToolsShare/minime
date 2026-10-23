"""采集适配器：按通道分目录，import 本包即完成全部注册。

  trae/   Trae 通道（桌面端数据目录 + 云端接口）
    cloud.py      云端会话消息
    snapshot.py   会话快照 git
    workspace.py  工作区索引 + 编辑器文件历史
    memory.py     Trae 记忆文件
  files/  本机文件通道（用户自己的目录与笔记库）
    docs.py       本地文档/笔记
    obsidian.py   Obsidian vault
  agents/ AI 助手通道（其他 AI 客户端的本地会话）
    workbuddy.py  WorkBuddy / WorkBuddy AI
    doubao.py     豆包（Doubao）

新增源：在对应通道目录下写一个模块并 @register，然后在该通道的
__init__.py 里 import 一行；通道本身不需要改动调度与流水线。
调试单个源用命令行：python collector/collect.py --only <adapter name>
"""
import os
import sys

# 适配器模块统一从 collector/ 导入 base（框架），避免逐层相对层级差异
_COLLECTOR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _COLLECTOR not in sys.path:
    sys.path.insert(0, _COLLECTOR)

from . import agents, files, trae  # noqa: E402,F401

__all__ = ["trae", "files", "agents"]