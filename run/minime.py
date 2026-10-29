#!/usr/bin/env python3
"""minime 统一命令行入口。

用法：minime <命令> [参数]
  setup             首次构建（采集 + 清洗 + 图谱 + 画像）
  update            增量更新（采集按适配器间隔自动判断，未到点跳过）
  collect           只采集
  status            现状 + 服务探活 + 「改了没重启」提示
  doctor            自检（逐项给出通过/失败与下一步）
  web               启动 Web 面板（默认 http://127.0.0.1:8765/）
  mcp [--http]      启动 MCP 服务（默认 stdio，--http 跨 AI 共享）
  watch             常驻自动采集
  logs [-f] [-n N]  查看运行日志（默认尾部 80 行，-f 跟随）
  llm [--run N|--all]  LLM 归纳进度；--run 后台跑一批，--all 归纳全部待处理轮次
  embed [--wait]    重算当前模型的全部向量（换模型/换文本模式后用）；--status 看进度
  config            以文本查看当前生效配置
  set KEY VALUE     修改配置项（key 用点号路径，如 llm.enabled true）
  diag              导出脱敏诊断包（排障用，可发给他人）
  restore           从持久层重建知识库（误删/清空后恢复）
  pack              数据源包：export / import / inspect
  install           安装/重装定时任务（首次运行会自动安装）
  uninstall         移除定时任务（保留数据）
"""
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
sys.path.insert(0, os.path.join(ROOT, "pipeline"))
sys.path.insert(0, os.path.join(ROOT, "collector"))

import paths  # noqa: E402

LOG_PATH = os.path.join(paths.LOGS, "minime.jsonl")
TASK_NAME = "ai.minime.collect"
PLIST = os.path.expanduser("~/Library/LaunchAgents/ai.minime.collect.plist")

USAGE = """minime — 个人知识库 / 决策分身

  minime setup              首次构建（采集 + 清洗 + 图谱 + 画像）
  minime update             增量更新
  minime collect [--force]  只采集
  minime status             现状与健康
  minime doctor             自检
  minime web                启动 Web 面板
  minime mcp [--http]       启动 MCP 服务
  minime watch              常驻自动采集
  minime logs [-f] [-n 80]  查看运行日志
  minime chat [问题]        基于知识库对话（--list 历史 / --session 续聊）
  minime llm [--run N|--all]  LLM 归纳进度 / 跑一批 / 归纳全部
  minime config            查看生效配置
  minime set KEY VALUE     修改配置项
  minime diag              导出诊断包
  minime restore           从持久层重建知识库
  minime pack <export|import|inspect> ...   数据源包
  minime migrate --from <旧目录> [--merge]  把旧代码目录里的 store/config.json 搬到数据目录
  minime link               注册 minime 命令到 PATH
  minime install            安装/重装定时任务（首次运行会自动安装）
  minime uninstall          移除定时任务

面板：http://127.0.0.1:8765/
"""


def _run(script, *args, quiet=False):
    path = os.path.join(ROOT, script)
    if not os.path.exists(path):
        print(f"minime: 找不到 {script}（安装包不完整？）", file=sys.stderr)
        return 3
    cmd = [PY, path, *args]
    p = subprocess.run(cmd, cwd=ROOT, text=True)
    if p.returncode < 0:  # 被 Ctrl-C / kill 终止（web/mcp/watch 常见），不算失败
        return 0
    if p.returncode != 0 and not quiet:
        print(f"minime: {script} 执行失败（退出码 {p.returncode}）", file=sys.stderr)
        print("  下一步：minime doctor 查看具体哪一项不可用", file=sys.stderr)
    return p.returncode


def _preflight():
    """首次运行引导：环境/依赖/初始化缺什么就直说该做什么。"""
    tips = []
    if sys.version_info < (3, 10):
        tips.append(f"Python 版本过低（{sys.version.split()[0]}），需要 3.10+")
    if not os.path.isdir(os.path.join(ROOT, ".git")) and not os.path.isdir(
            os.path.join(ROOT, "collector")):
        tips.append("工程目录不完整：请重新 clone 仓库")
    try:
        import sqlite3  # noqa: F401, PLC0415
        if not sqlite3.connect(":memory:").execute(
                "SELECT 1 FROM pragma_compile_options WHERE compile_options LIKE '%FTS5%'"
        ).fetchone():
            tips.append("当前 Python 的 SQLite 未启用 FTS5，检索会失败")
    except Exception:  # noqa: BLE001
        tips.append("SQLite 不可用")
    if not os.path.exists(paths.KB):
        tips.append("还没初始化知识库 → 先跑：minime setup")
    tips += _missing_deps()
    return tips


# 可选依赖：缺了不影响主流程，只影响单个能力，所以给出「装什么 + 换来什么」
_OPTIONAL_DEPS = (
    ("mcp", "MCP 服务不可用（minime mcp / 接进 Trae）", "mcp"),
    ("cryptography", "Trae 云端会话采集不可用（其他源正常）", "cryptography"),
    ("numpy", "向量检索/语义闸门退回逐条余弦（结果一致，2000 条以上明显变慢）", "numpy"),
)


def _missing_deps():
    missing = []
    for mod, impact, pkg in _OPTIONAL_DEPS:
        try:
            __import__(mod)
        except ImportError:
            missing.append(f"缺少 {mod}：{impact} → {PY} -m pip install {pkg}")
    return missing


def _check_port():
    """端口被占用时给出明确处置，而不是抛 traceback。"""
    import socket  # noqa: PLC0415
    import settings_schema as SS  # noqa: PLC0415
    port = int((SS.load().get("web") or {}).get("port") or 8765)
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", port))
    except OSError:
        print(f"端口 {port} 已被占用。", file=sys.stderr)
        print(f"  如果是旧面板：先结束它（或直接访问 http://127.0.0.1:{port}/）", file=sys.stderr)
        print(f"  换端口：minime set web.port 8766 && minime web", file=sys.stderr)
        sys.exit(1)
    finally:
        s.close()


def _sched_installed():
    """定时任务是否已注册（macOS launchd / Windows 计划任务）。"""
    if sys.platform == "darwin":
        return os.path.exists(PLIST)
    if os.name == "nt":
        return subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME],
                              capture_output=True).returncode == 0
    return False


def cmd_status():
    import sqlite3  # noqa: PLC0415
    import progress as P  # noqa: PLC0415
    kb = paths.db_connect(paths.KB, row_factory=sqlite3.Row)
    try:
        atoms = kb.execute("SELECT COUNT(*) FROM atom WHERE state='active'").fetchone()[0]
        ents = kb.execute("SELECT COUNT(*) FROM entity").fetchone()[0]
    except sqlite3.Error:
        kb.close()
        print("minime 状态")
        print("  知识库为空或未初始化 → 先跑：minime setup")
        print(f"  数据目录：{paths.DATA_ROOT}")
        return 1
    pr = P.llm_progress()
    print("minime 状态")
    print(f"  知识 {atoms} 条 · 实体 {ents} 个 · LLM 归纳产物 {pr['llm_atoms']} 条")

    # ---- 采集
    print("  采集源：")
    for c in pr["collect"]:
        st = c["last_status"] or "-"
        last = time.strftime("%m-%d %H:%M", time.localtime(c["last_run"] or 0)) \
            if c.get("last_run") else "未运行"
        if not c.get("enabled", True):
            print(f"    ○ {c['adapter']:16s} {'已停用':12s} {last}")
            continue
        mark = "✓" if st == "ok" else ("!" if st == "unavailable" else "✗")
        print(f"    {mark} {c['adapter']:16s} {st:12s} {last}"
              + (f"  失败 {c['fails']} 次" if c.get("fails") else ""))
    if any(not c.get("enabled", True) for c in pr["collect"]):
        print("    → 已停用的源不参与自动采集；在面板「采集源」页勾选可重新启用")

    # ---- 归纳
    bar_w = 24
    filled = int(pr["percent"] / 100 * bar_w)
    print("  LLM 归纳：" + ("已启用 " + str(pr["model"]) if pr["enabled"] else "未启用（设置页打开）"))
    print(f"    [{'█' * filled}{'·' * (bar_w - filled)}] {pr['percent']}%"
          f"  {pr['processed']}/{pr['candidates']} 轮 · 待处理 {pr['pending']}")
    print(f"    快照轮次 {pr['turns']} · 有实质改动才作候选"
          f"（每轮改动 ≥5 行或新增 ≥2 文件）")
    if pr["last_processed_at"]:
        print(f"    最近归纳 {time.strftime('%m-%d %H:%M', time.localtime(pr['last_processed_at']))}"
              f" · 每批上限 {pr['per_run']} 轮")
    if pr.get("running"):
        print(f"    ! 正在后台归纳：{', '.join(pr['running'])}  → minime llm 查看细进度")
    if pr["pending"]:
        print(f"    → 跑一批：minime llm --run 20 · 全部：minime llm --all")

    # ---- 定时
    import settings_schema as SS  # noqa: PLC0415
    cfg = SS.load()
    sched = cfg.get("schedule") or {}
    loaded = _sched_installed()
    print("  定时采集：" + ("已安装" if loaded else "未安装"))
    if loaded and sys.platform == "darwin":
        out = subprocess.run(["launchctl", "list", TASK_NAME],
                             capture_output=True, text=True).stdout
        lo = re.search(r'"LastExitStatus"\s*=\s*(-?\d+)', out)
        print(f"    间隔 {sched.get('interval_minutes', 30)} 分钟"
              f" · 上次退出码 {lo.group(1) if lo else '未知'}")
        if lo and lo.group(1) != "0":
            print(f"    ! 上次执行失败 → 看 {paths.LOGS}，或 minime doctor")
    elif loaded:
        print(f"    间隔 {sched.get('interval_minutes', 30)} 分钟 · 任务 {TASK_NAME}")
    else:
        print("    → 安装：minime install")

    # ---- 服务
    import urllib.request  # noqa: PLC0415
    try:
        with urllib.request.urlopen("http://127.0.0.1:8765/api/health", timeout=3) as r:
            h = json.loads(r.read().decode())
        print(f"  Web 面板：运行中 · 端口 {h.get('port')} · 已运行 {h.get('uptime_s')}s")
        if h.get("code_stale"):
            print(f"    ! 代码已更新但服务在跑旧版本（{', '.join(h['code_stale'])}）"
                  f" → 重启：minime web")
        if h.get("jobs"):
            print(f"    后台任务：{', '.join(h['jobs'])}")
    except Exception:  # noqa: BLE001
        print("  Web 面板：未运行  → 启动：minime web")
    print(f"  日志：{LOG_PATH}")
    print("  下一步：minime doctor 做完整自检")
    return 0


def cmd_llm(argv):
    """LLM 归纳进度；--run N 时后台跑一批（可 Ctrl-C 断开，任务继续）。"""
    import progress as P  # noqa: PLC0415
    if "--run" not in argv:
        pr = P.llm_progress()
        mark = "已启用" if pr["enabled"] else "未启用"
        print(f"LLM 归纳：{mark} · 模型 {pr['model']} · {pr['base_url']}")
        print(f"  进度 {pr['percent']}%  {pr['processed']}/{pr['candidates']} 轮"
              f" · 待处理 {pr['pending']}")
        print(f"  快照轮次 {pr['turns']} · 归纳产物 {pr['llm_atoms']} 条"
              f" · 知识库 {pr['atoms']} 条")
        src = " · ".join(f"{k} {v}" for k, v in list(pr["by_source"].items())[:6])
        print(f"  知识来源：{src}")
        print(f"  来源明细：minime llm --by-source")
        if pr["pending"]:
            print(f"  → 跑一批：minime llm --run 20 · 全部：minime llm --all")
        return 0
    if "--by-source" in argv:
        pr = P.llm_progress()
        for k, v in pr["by_source"].items():
            print(f"  {k:20s} {v}")
        return 0
    n = 0 if "--all" in argv else 20
    if "--run" in argv:
        try:
            n = int(argv[argv.index("--run") + 1])
        except (IndexError, ValueError):
            print("  ! --run 后面需要数字，例如 --run 20", file=sys.stderr)
            return 2
    j = P.start("llm", P.run_llm_job, n)
    scope = "全部待处理轮次" if n == 0 else f"上限 {n} 轮"
    print(f"已在后台启动归纳：{j['job_id']}（{scope}）")
    print("  跟踪进度：minime llm   或不打断地看日志：minime logs -f")
    return 0


def cmd_embed(argv):
    """重算向量：默认后台跑并立即返回；--wait 前台跑完再退出。"""
    import progress as P  # noqa: PLC0415
    import embed as E  # noqa: PLC0415
    import config as CFG  # noqa: PLC0415
    c = CFG.load().get("embedding") or {}
    if not c.get("enabled"):
        print("! 向量检索未启用：minime set embedding.enabled true", file=sys.stderr)
        return 2
    mdl = E.model(c)
    if "--status" in argv:
        jobs = [j for j in P.list_jobs(30) if j["kind"] == "embed"]
        if not jobs:
            print(f"没有向量重算记录（当前模型 {mdl}）")
            return 0
        j = jobs[0]
        print(f"{j['job_id']} · {j['status']} · {j['done']}/{j['total'] or '?'}"
              f" · {j['elapsed_s']}s")
        if j.get("message"):
            print(f"  {j['message']}")
        return 0
    if "--wait" in argv:
        j = P.start("embed", P.run_embed_job)
        jid = j["job_id"]
        if j.get("reused"):
            print(f"已有向量重算任务在跑：{jid}，改为跟踪它的进度")
        else:
            print(f"向量重算中：{jid}（模型 {mdl}）")
        last = -1
        while True:
            time.sleep(2)
            j = P.job(jid)
            if not j:
                break
            done, total = j.get("done") or 0, j.get("total") or 0
            if total and done != last:
                last = done
                pct = done * 100 // total
                bar = "█" * (pct // 5) + "·" * (20 - pct // 5)
                print(f"\r  [{bar}] {pct:3d}%  {done}/{total}  {j.get('message') or ''}"[:120],
                      end="", flush=True)
            if j["status"] != "running":
                print()
                break
        res = (j or {}).get("result") or {}
        if j and j["status"] == "ok":
            print(f"完成：模型 {res.get('model')} · 处理 {res.get('processed')} 条"
                  f" · 新算向量 {res.get('embedded')} 条")
            return 0
        if j and j["status"] == "cancelled":
            print("已取消")
            return 130
        print(f"! {res.get('error') or (j or {}).get('message') or '未知错误'}", file=sys.stderr)
        return 1
    j = P.start("embed", P.run_embed_job)
    if j.get("reused"):
        print(f"已有向量重算任务在跑：{j['job_id']}（模型 {mdl}）")
    else:
        print(f"已在后台启动向量重算：{j['job_id']}（模型 {mdl}）")
    print("  跟踪进度：minime embed --status")
    return 0


def cmd_config(argv):
    """查看当前生效配置（敏感值脱敏）。"""
    import settings_schema as S  # noqa: PLC0415
    cur = S.current_view()
    if "--json" in argv:
        print(json.dumps(cur, ensure_ascii=False, indent=2))
        return 0
    group = None
    for f in S.FIELDS:
        if f["group"] != group:
            group = f["group"]
            print(f"\n[{group}]")
        v = cur.get(f["key"])
        if isinstance(v, list):
            v = " , ".join(map(str, v)) or "（空）"
        print(f"  {f['key']:36s} {'' if v is None else v}")
    print(f"\n配置文件：{S.PATH}")
    print("修改：minime set <key> <value>（bool 用 true/false，list 用逗号分隔）")
    return 0


def cmd_set(argv):
    """按 schema 修改单个配置项，写完立刻按新配置校验。"""
    import settings_schema as S  # noqa: PLC0415
    if len(argv) < 2:
        print("用法：minime set <key> <value>", file=sys.stderr)
        print("  例：minime set llm.enabled true", file=sys.stderr)
        print("  例：minime set collect.local.roots ~/Documents,/tmp/notes", file=sys.stderr)
        print("  查看可配置项：minime config", file=sys.stderr)
        return 2
    key, value = argv[0], " ".join(argv[1:])
    if key not in S.BY_KEY:
        near = [k for k in S.BY_KEY if argv[0].split(".")[-1] in k][:5]
        print(f"未知配置项：{key}", file=sys.stderr)
        if near:
            print("  你是不是想设：" + "、".join(near), file=sys.stderr)
        return 2
    field = S.BY_KEY[key]
    if field["type"] == "list" and argv[1:]:
        value = [x.strip() for x in value.split(",") if x.strip()]
    elif field["type"] == "list" or value.lower() in ("null", "none", ""):
        value = None
    r = S.save({key: value})
    if r["errors"]:
        print(f"保存失败：{list(r['errors'].values())[0]}", file=sys.stderr)
        return 2
    shown = r["applied"].get(key)
    print(f"已保存 {key} = {shown if shown is not None else '（已清空，回退默认）'}")
    if key.startswith("web."):
        print("  → 需重启面板生效：minime web")
    elif key == "answer.mode":
        print("  → 已开关：llm=大模型作答（向量检索开启）/ rules=关键词规则作答（不调用模型）")
    elif key in ("embedding.model", "embedding.text_mode"):
        print("  → 向量需重算：minime update（旧向量按模型/文本模式缓存，不会自动迁移）")
    elif key.startswith("schedule."):
        print("  → 需重新注册定时任务：minime install")
    elif key.startswith("collect.") and key != "collect.intervals":
        print("  → 下次采集自动生效；要立刻采一次：minime collect --force")
    return 0


def cmd_restore():
    """从持久层重建知识库：raw 归档 + llm_atom 都还在，重建不会丢知识。"""
    import sqlite3  # noqa: PLC0415
    kb_path = paths.KB
    before = 0
    if os.path.exists(kb_path):
        c = paths.db_connect(kb_path)
        try:
            before = c.execute("SELECT COUNT(*) FROM atom WHERE state='active'").fetchone()[0]
        except sqlite3.Error:
            before = 0
        finally:
            c.close()
    print(f"当前知识库 {before} 条；将用 raw 归档与 llm_atom 重建（不删任何归档）")
    rc = _run("pipeline/normalize.py")
    if rc == 0:
        print("  → 重建完成，接着执行：minime update 补齐图谱/画像")
    return rc


def cmd_migrate(argv):
    """把旧代码目录里的 store/ 与 config.json 搬到当前数据目录（源码 → pipx 升级用）。"""
    if "--from" not in argv:
        print("用法：minime migrate --from <旧目录>", file=sys.stderr)
        print("  例：minime migrate --from ~/code/minime", file=sys.stderr)
        return 2
    try:
        src = os.path.abspath(os.path.expanduser(argv[argv.index("--from") + 1]))
    except IndexError:
        print("--from 后面需要旧目录路径", file=sys.stderr)
        return 2
    if not os.path.isdir(os.path.join(src, "store")):
        print(f"没找到 {os.path.join(src, 'store')}", file=sys.stderr)
        return 1
    if paths.has_kb() and "--merge" not in argv:
        print(f"数据目录已有知识库：{paths.KB}", file=sys.stderr)
        print("  为避免覆盖，先备份并移走它，或用 MINIME_HOME 指定另一个数据目录", file=sys.stderr)
        print("  只补搬缺失的归档/日志：加 --merge", file=sys.stderr)
        return 1
    try:
        moved = paths.migrate_from(src)
    except OSError as e:
        print(f"迁移失败：{e}", file=sys.stderr)
        return 1
    print(f"已从 {src} 迁移 {len(moved)} 项到 {paths.DATA_ROOT}")
    print("  下一步：minime status")
    return 0


def cmd_diag():
    """导出诊断包：状态 + 配置（脱敏）+ 最近日志，便于排障与反馈。"""
    import zipfile  # noqa: PLC0415
    import settings_schema as S  # noqa: PLC0415
    import selftest  # noqa: PLC0415
    import progress as P  # noqa: PLC0415
    out_dir = paths.DIAG
    os.makedirs(out_dir, exist_ok=True)
    fn = os.path.join(out_dir, time.strftime("diag-%Y%m%d-%H%M%S.zip"))
    with zipfile.ZipFile(fn, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("selftest.json", json.dumps(selftest.run(), ensure_ascii=False, indent=1))
        z.writestr("progress.json", json.dumps(P.snapshot(), ensure_ascii=False, indent=1))
        z.writestr("config.redacted.json",
                   json.dumps(S.current_view(), ensure_ascii=False, indent=1))
        z.writestr("README.txt",
                   "minime 诊断包（已脱敏）\n"
                   "selftest.json 自检结果 / progress.json 处理进度 / "
                   "config.redacted.json 配置（密钥已替换为 ***）\n"
                   "logs.jsonl 最近 500 条运行日志\n")
        if os.path.exists(LOG_PATH):
            with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
                z.writestr("logs.jsonl", "".join(f.readlines()[-500:]))
    print(f"诊断包已生成：{fn}")
    print(f"  大小 {os.path.getsize(fn) / 1024:.0f} KB · 已脱敏（不含原始对话与密钥）")
    return 0


def cmd_chat(argv):
    """命令行对话：minime chat [问题] / --list / --show <id>。"""
    import chat as C  # noqa: PLC0415
    if "--list" in argv:
        for s in C.list_sessions(20):
            when = time.strftime("%m-%d %H:%M", time.localtime(s["updated_at"]))
            print(f"  {s['id']}  {when}  {s['n_turns']} 轮  {s['title']}")
        if not C.list_sessions(1):
            print("  还没有对话 → minime chat 你的问题")
        return 0
    if "--show" in argv:
        try:
            s = C.get_session(argv[argv.index("--show") + 1])
        except (IndexError, ValueError):
            print("用法：minime chat --show <session_id>", file=sys.stderr)
            return 2
        if not s:
            print("会话不存在", file=sys.stderr)
            return 2
        print(f"{s['title']} · {s['id']}")
        for m in s["messages"]:
            who = "你" if m["role"] == "user" else "分身"
            print(f"\n[{who}] {m['content']}")
        return 0
    q = " ".join(argv).strip()
    if not q:
        print("用法：minime chat <问题>", file=sys.stderr)
        print("      minime chat --list            查看历史对话", file=sys.stderr)
        print("      minime chat --show <id>       看某段对话", file=sys.stderr)
        print("      minime web → 「对话」页        多轮连续对话", file=sys.stderr)
        return 2
    # 未指定会话时新建；连续追问用 --session 续上
    sid = None
    if "--session" in argv:
        try:
            sid = argv[argv.index("--session") + 1]
            q = " ".join(a for i, a in enumerate(argv)
                         if i not in (argv.index("--session"),
                                      argv.index("--session") + 1)).strip()
        except (IndexError, ValueError):
            print("用法：minime chat --session <id> <问题>", file=sys.stderr)
            return 2
    if not sid:
        sid = C.new_session(q[:40])
    try:
        r = C.ask(sid, q)
    except RuntimeError as e:
        print(f"对话失败：{e}", file=sys.stderr)
        return 1
    print(r["content"])
    if r["refs"]:
        print("\n依据：")
        for i, ref in enumerate(r["refs"], 1):
            print(f"  [{i}] {ref['title'][:60]}  ({ref['atom_id']})")
    print(f"\n{sid} · {r['elapsed_s']}s · 继续追问：minime chat --session {sid} <问题>")
    return 0


def cmd_logs(argv):
    follow = "-f" in argv
    n = 80
    if "-n" in argv:
        try:
            n = int(argv[argv.index("-n") + 1])
        except (IndexError, ValueError):
            n = 80
    if not os.path.exists(LOG_PATH):
        print("还没有日志。执行 minime collect 或打开面板后生成。")
        return 0
    with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    for ln in lines[-n:]:
        _print_log_line(ln)
    if not follow:
        return 0
    try:
        with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
            f.seek(0, os.SEEK_END)
            while True:
                chunk = f.readline()
                if chunk:
                    _print_log_line(chunk)
                else:
                    time.sleep(0.7)
    except KeyboardInterrupt:
        return 0


def _print_log_line(ln):
    ln = ln.strip()
    if not ln:
        return
    try:
        r = json.loads(ln)
    except ValueError:
        print(ln)
        return
    t = time.strftime("%H:%M:%S", time.localtime(r.get("ts") or 0))
    lvl = str(r.get("level", "info")).upper()[:4]
    rest = " ".join(f"{k}={v}" for k, v in r.items()
                    if k not in ("id", "ts", "level", "event"))
    print(f"{t} {lvl:4s} {r.get('event',''):24s} {rest}")


def cmd_link():
    """把 minime 注册成 PATH 里的命令（写一个可执行的 shim）。"""
    if os.name == "nt":
        target = os.path.join(os.environ.get("LOCALAPPDATA")
                              or os.path.expanduser("~"), "minime", "bin")
        os.makedirs(target, exist_ok=True)
        shim = os.path.join(target, "minime.cmd")
        with open(shim, "w", encoding="utf-8") as f:
            f.write(f'@echo off\r\n"{PY}" "{ROOT}\\run\\minime.py" %*\r\n')
        print(f"已注册命令：{shim}")
        if target.lower() not in os.environ.get("PATH", "").lower():
            print(f"  ! {target} 不在 PATH 中，请在「系统属性 → 环境变量」里加入")
        print("  下一步：新开终端执行 minime status")
        return 0
    target = os.path.join(os.path.expanduser("~"), ".local/bin")
    os.makedirs(target, exist_ok=True)
    shim = os.path.join(target, "minime")
    with open(shim, "w", encoding="utf-8") as f:
        f.write(f'#!/bin/sh\nexec "{PY}" "{ROOT}/run/minime.py" "$@"\n')
    os.chmod(shim, 0o755)
    print(f"已注册命令：{shim}")
    if target not in os.environ.get("PATH", "").split(":"):
        print(f"  ! {target} 不在 PATH 中，请把它加入 shell 配置：")
        print(f'    echo \'export PATH="$HOME/.local/bin:$PATH"\' >> ~/.zshrc')
    print("  下一步：新开终端执行 minime status")
    return 0


def cmd_install():
    import settings_schema as SS  # noqa: PLC0415
    sched = (SS.load().get("schedule") or {})
    if not sched.get("enabled", True):
        print("定时采集已在设置中关闭（schedule.enabled=false）；")
        print("  → 要启用：minime set schedule.enabled true && minime install")
        return 0
    minutes = int(sched.get("interval_minutes") or 30)
    if os.name == "nt":
        return _install_windows(minutes)
    if sys.platform != "darwin":
        print("定时采集目前支持 macOS（launchd）与 Windows（计划任务）；")
        print("  当前系统可用前台常驻替代：minime watch")
        return 1
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    # 注意：launchd 不继承登录 shell 的环境。必须显式给 HOME/PATH，
    # 否则 ~ 展开失败、git 找不到，LLM 步骤会卡住。
    # StandardOutPath 不能指向 /Volumes 下的卷，否则任务以 78 (EX_CONFIG) 退出。
    os.makedirs(paths.LOGS, exist_ok=True)
    out_log = os.path.join(paths.LOGS, "launchd.out")
    err_log = os.path.join(paths.LOGS, "launchd.err")
    # 不传 --fast / --no-llm：轨迹抽取必须跑，是否归纳交给运行时配置决定。
    # 否则安装时快照的参数会写死，用户之后在设置里开启 LLM 也不会生效。
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{TASK_NAME}</string>
  <key>ProgramArguments</key><array><string>{PY}</string>
    <string>{ROOT}/run/pipeline.py</string></array>
  <key>WorkingDirectory</key><string>{ROOT}</string>
  <key>EnvironmentVariables</key><dict>
    <key>HOME</key><string>{os.path.expanduser("~")}</string>
    <key>PATH</key><string>{os.path.dirname(PY)}:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>StartInterval</key><integer>{minutes * 60}</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>{out_log}</string>
  <key>StandardErrorPath</key><string>{err_log}</string>
</dict></plist>
"""
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(PLIST, "w") as f:
        f.write(plist)
    subprocess.run(["launchctl", "unload", PLIST], capture_output=True)
    r = subprocess.run(["launchctl", "load", PLIST], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"minime: 定时任务加载失败：{r.stderr.strip()}", file=sys.stderr)
        print("  下一步：检查 launchctl 权限；或用 minime watch 前台常驻替代", file=sys.stderr)
        return 1
    print(f"已安装定时任务：{PLIST}")
    print(f"  计划：每 {minutes} 分钟执行一次 minime update（采集按各源周期自动判断，未到点跳过）")
    print(f"  日志：{LOG_PATH} 与 {err_log}")
    print("  下一步：minime status 确认采集进度")
    try:  # 主动安装即视为重新开启，清掉「用户要求不自动安装」标记
        os.remove(SCHEDULE_OFF)
    except OSError:
        pass
    return 0


def _install_windows(minutes):
    """Windows 用计划任务（schtasks）实现定时采集。"""
    pipeline = os.path.join(ROOT, "run", "pipeline.py")
    os.makedirs(paths.LOGS, exist_ok=True)
    tr = f'"{PY}" "{pipeline}"'
    r = subprocess.run(["schtasks", "/Create", "/F", "/SC", "MINUTE",
                        "/MO", str(minutes), "/TN", TASK_NAME, "/TR", tr],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"minime: 计划任务创建失败：{r.stderr.strip() or r.stdout.strip()}",
              file=sys.stderr)
        print("  下一步：用管理员权限重试；或用 minime watch 前台常驻替代",
              file=sys.stderr)
        return 1
    print(f"已安装计划任务：{TASK_NAME}")
    print(f"  计划：每 {minutes} 分钟执行一次 minime update")
    print(f"  日志：{LOG_PATH}")
    print("  下一步：minime status 确认采集进度")
    return 0


SCHEDULE_OFF = os.path.join(paths.DATA_ROOT, ".schedule-off")


def _ensure_schedule():
    """首次运行自动注册定时任务，让采集与归纳开箱即用。

    用户显式 uninstall 时会写 .schedule-off 标记，之后不再自动装回。
    """
    if sys.platform != "darwin" and os.name != "nt":
        return
    if os.path.exists(SCHEDULE_OFF):
        return
    try:
        import settings_schema as SS  # noqa: PLC0415
        if not (SS.load().get("schedule") or {}).get("enabled", True):
            return
        if _sched_installed():
            return
        print("  首次运行：自动安装定时任务（采集 + 轨迹 + 归纳）")
        cmd_install()
    except Exception as e:  # noqa: BLE001
        print(f"  ! 定时任务自动安装失败：{type(e).__name__}: {e}"
              f"（可稍后执行 minime install）")


def cmd_uninstall():
    if sys.platform == "darwin":
        if os.path.exists(PLIST):
            subprocess.run(["launchctl", "unload", PLIST], capture_output=True)
            os.remove(PLIST)
            print(f"已移除定时任务：{PLIST}")
        else:
            print("没有安装定时任务")
    elif os.name == "nt":
        r = subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME],
                           capture_output=True, text=True)
        print(f"已移除计划任务：{TASK_NAME}" if r.returncode == 0 else "没有安装定时任务")
    else:
        print("没有安装定时任务")
    print(f"  注意：数据目录 {paths.DATA_ROOT} 已保留（含知识库与配置）；需要清空请手动删除")
    try:  # 标记用户明确不要定时任务，避免下次运行又自动装回
        with open(SCHEDULE_OFF, "w") as f:
            f.write(str(int(time.time())))
    except OSError:
        pass
    return 0


def main():
    paths.ensure()
    argv = sys.argv[1:]
    cmd = argv[0] if argv else "status"
    rest = argv[1:]
    if cmd in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    # 首次运行自动注册定时任务（显式 install/uninstall 自己管理；setup/update
    # 先跑完流水线再装，避免和刚拉起的定时任务抢 .pipeline.lock）
    if cmd not in ("setup", "update", "install", "uninstall"):
        _ensure_schedule()
    if cmd == "setup":
        rc = _run("run/pipeline.py")
        if rc == 0:
            import config  # noqa: PLC0415
            if not (config.load().get("llm") or {}).get("enabled"):
                print("  ! LLM 归纳未启用：先按结构化方式入库，不会提炼决策/偏好。")
                print("    → 面板「设置」填模型接口并开启，或 minime set llm.enabled true")
        _ensure_schedule()
        if rc == 0:
            print("  下一步：minime web 打开面板，或 minime mcp 接入 Trae")
        return rc
    if cmd == "update":
        rc = _run("run/pipeline.py")
        _ensure_schedule()
        return rc
    if cmd == "collect":
        return _run("collector/collect.py", *(["--force"] if "--force" in rest else []))
    if cmd == "status":
        for tip in _preflight():
            print(f"! {tip}")
        return cmd_status()
    if cmd == "doctor":
        for tip in _preflight():
            print(f"! {tip}")
        return _run("pipeline/selftest.py", *(["--json"] if "--json" in rest else []))
    if cmd == "web":
        _check_port()
        return _run("web/server.py")
    if cmd == "mcp":
        return _run("mcp_server/server.py", *rest)
    if cmd == "watch":
        return _run("run/autocollect.py", *rest)
    if cmd == "logs":
        return cmd_logs(rest)
    if cmd == "chat":
        return cmd_chat(rest)
    if cmd == "llm":
        return cmd_llm(rest)
    if cmd == "embed":
        return cmd_embed(rest)
    if cmd == "config":
        return cmd_config(rest)
    if cmd == "set":
        return cmd_set(rest)
    if cmd == "diag":
        return cmd_diag()
    if cmd == "restore":
        return cmd_restore()
    if cmd == "migrate":
        return cmd_migrate(rest)
    if cmd == "pack":
        return _run("pipeline/pack.py", *rest)
    if cmd == "install":
        return cmd_install()
    if cmd in ("link", "install-cli"):
        return cmd_link()
    if cmd == "uninstall":
        return cmd_uninstall()
    print(USAGE)
    print(f"未知命令：{cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
