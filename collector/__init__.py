"""采集器包：导入即注册全部适配器。

对外暴露：
  base.SourceAdapter / register   扩展新信息源的唯一入口
  base.AutoCollector              自动采集调度
  collect_cli.main()              命令行入口（collect.py 复用）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from base import (AutoCollector, SourceAdapter, all_adapters, init_db,  # noqa: E402,F401
                  put_raw, register, registry)

# 导入 adapters 即完成注册；各通道（trae / files）在 adapters/__init__.py 里汇总
import adapters  # noqa: E402,F401
