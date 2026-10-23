"""采集器命令行入口（适配器模式）。

用法：
  python collector/collect.py                # 只跑到点的源（自动采集）
  python collector/collect.py --force        # 全部源立即重跑
  python collector/collect.py --status       # 各源健康度与进度
  python collector/collect.py --only snapshot,trae_memory
  python collector/collect.py --watch        # 常驻自动采集（每 30s 检查一次）
  python collector/collect.py --once         # 常驻模式下只跑一轮

各信息源按通道实现在 collector/adapters/ 下（trae/ Trae 数据、files/ 本机文件、
agents/ WorkBuddy 与豆包等 AI 助手），新增源只需写一个 SourceAdapter 子类并
@register，无需改动本文件。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 兼容旧引用：from collect import DB / SNAPSHOT / TRAE ...
from base import (HISTORY, MANIFEST as DB, MEMORY, RAW, SNAPSHOT,  # noqa: F401
                  TRAE, WS_STORAGE, AutoCollector, init_db, put_raw, registry)
from base import all_adapters  # noqa: E402,F401
import adapters  # noqa: F401,E402  —— 触发注册


def main():
    argv = sys.argv[1:]
    names = None
    if "--only" in argv:
        names = {x.strip() for x in argv[argv.index("--only") + 1].split(",") if x.strip()}
    conn = init_db()
    ac = AutoCollector(conn, names)

    if "--status" in argv:
        print(json.dumps(ac.status(), ensure_ascii=False, indent=2))
        return
    if "--watch" in argv:
        ac.loop(once="--once" in argv)
        return

    results = ac.tick(force="--force" in argv)
    report = {r["adapter"]: {k: v for k, v in r.items() if k != "adapter"}
              for r in results}
    n, size = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(size),0) FROM raw_object").fetchone()
    report["archive"] = {"objects": n, "bytes": size}
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
