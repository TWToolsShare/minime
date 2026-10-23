"""Trae 通道：读取 Trae 桌面端数据目录与云端接口。

  cloud.py      云端会话消息（本地登录态）
  snapshot.py   会话快照 git（逐轮文件变更）
  workspace.py  workspaceStorage 会话索引 + 编辑器文件历史
  memory.py     ~/.trae/memory 清洗后的记忆文件

import 本包即完成注册。
"""
from . import cloud, memory, snapshot, workspace  # noqa: F401