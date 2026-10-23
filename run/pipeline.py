"""minime 全量流水线：采集 -> 全工作区索引 -> 轨迹 -> 清洗 -> 图谱 -> LLM 归纳 -> 画像 -> 记忆同步。

采集步骤基于适配器（collector/adapters/，按通道分为 trae/、files/ 与 agents/），
按各源 interval_s 自动判断是否到点：
  到点的源才采集，未到点直接跳过；因此重复运行本脚本是廉价的。

用法：
  python run/pipeline.py                    # 全量（采集按适配器间隔自动判断）
  python run/pipeline.py --force-collect    # 忽略间隔，所有源立即重采
  python run/pipeline.py --fast             # 跳过轨迹重扫
  python run/pipeline.py --no-llm           # 本次不跑 LLM 归纳
  python run/pipeline.py --sync             # 本次强制尝试记忆同步（仍需 enabled）
  python run/pipeline.py --only normalize   # 只跑指定步骤
"""
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import paths  # noqa: E402

STEPS = [
    ("collect", ["collector/collect.py"] + (["--force"] if "--force-collect" in sys.argv else [])),
    ("index", ["collector/index_all.py"]),
    ("turns", ["pipeline/extract_turns.py"]),
    ("normalize", ["pipeline/normalize.py"]),
    ("graph", ["pipeline/graph.py"]),
    ("llm", ["pipeline/llm_enrich.py"]),
    ("profile", ["pipeline/profile.py"]),
    ("sync", ["pipeline/memory_sync.py"]),
]


def run(cmd, env=None):
    p = subprocess.run([PY, *cmd], cwd=ROOT, capture_output=True, text=True,
                       env={**os.environ, **(env or {})})
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed:\n{p.stderr[-2000:]}")
    return p.stdout, p.stderr


def main():
    argv = sys.argv[1:]
    fast = "--fast" in argv
    no_llm = "--no-llm" in argv
    only = None
    if "--only" in argv:
        only = argv[argv.index("--only") + 1]

    # 并发锁：定时任务与手动执行同时跑会互相清空 kb 表，必须串行
    lock = paths.acquire_lock()
    if lock is None:
        print(json.dumps({"skipped": "另一个流水线正在运行（数据目录 .pipeline.lock）"},
                         ensure_ascii=False))
        return

    t0 = time.time()
    report = {}
    for name, cmd in STEPS:
        if only and name != only:
            continue
        if fast and name in ("turns",):
            report[name] = "skipped(--fast)"
            continue
        if no_llm and name == "llm":
            report[name] = "skipped(--no-llm)"
            continue
        out, err = run(cmd, {"MINIME_SUMMARY_DISABLED": "1"} if no_llm else None)
        try:
            start, end = out.find("{"), out.rfind("}")
            report[name] = json.loads(out[start:end + 1]) if start >= 0 else out.strip()
        except ValueError:
            report[name] = out.strip().split("\n")[-1][:200]
        if err.strip():
            report.setdefault("_stderr", {})[name] = err.strip().split("\n")[-5:]
    report["elapsed_s"] = round(time.time() - t0, 1)
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
