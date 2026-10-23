"""常驻自动采集：按各适配器声明的 interval_s 持续采集。

用法：
  python run/autocollect.py               # 前台常驻（每 30s 检查一次，到点的源才跑）
  python run/autocollect.py --interval 60 # 自定义检查间隔

每次 tick 输出一行 JSON，便于 nohup / launchd 收集日志。
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "collector"))

import collect as C  # noqa: E402


def emit(res):
    print(json.dumps({"ts": int(time.time()), "tick": res}, ensure_ascii=False),
          flush=True)


def main():
    argv = sys.argv[1:]
    sleep_s = 30
    if "--interval" in argv:
        sleep_s = int(argv[argv.index("--interval") + 1])
    C.AutoCollector(C.init_db()).loop(sleep_s=sleep_s, on_tick=emit)


if __name__ == "__main__":
    main()
