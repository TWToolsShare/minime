"""自检：逐项验证「现在能不能用」，并把失败翻译成下一步动作。

检查项：
  config      配置文件可读、校验通过
  kb          知识库可打开、有原子、FTS 可检索
  index       工作区索引可用
  adapters    各采集源健康度（probe）
  llm         本地大模型连通与可用模型
  mnemosyne   记忆库 CLI 与目标 bank
  mcp         MCP 工具可枚举（直接 import，不启子进程）

退出码：全部通过 0，有失败 1 —— 可直接接进 CI / 升级脚本。
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))
sys.path.insert(0, os.path.join(ROOT, "collector"))

import paths  # noqa: E402

# 把上游错误翻译成「该去做什么」
HINTS = {
    401: "登录态失效：重新登录 Trae 后重跑采集",
    402: "额度用尽：换账号或改用本地数据源",
    403: "无权限：检查账号 scope",
    404: "接口不存在：可能是 Trae 升级后接口变更",
    429: "被限流：稍后重试",
}


class Result:
    def __init__(self):
        self.items = []

    def add(self, name, ok, detail="", hint=""):
        self.items.append({"check": name, "ok": bool(ok),
                           "detail": str(detail)[:300], "hint": hint})
        return ok

    @property
    def ok(self):
        return all(i["ok"] for i in self.items)

    def report(self):
        return {"ok": self.ok, "passed": sum(1 for i in self.items if i["ok"]),
                "total": len(self.items), "items": self.items}


def check_config(r):
    try:
        import settings_schema as S  # noqa: PLC0415
        cfg = S.load()
        r.add("config", True, f"{S.PATH} · {len(S.FIELDS)} 个可配置项"
              + ("" if os.path.exists(S.PATH) else "（未创建，使用默认值）"))
        if os.path.exists(S.PATH):
            mode = oct(os.stat(S.PATH).st_mode & 0o777)
            r.add("config.perms", mode in ("0o600", "0o400"),
                  f"权限 {mode}", "配置文件含密钥，建议 chmod 600 config.json")
        return cfg
    except Exception as e:  # noqa: BLE001
        r.add("config", False, f"{type(e).__name__}: {e}", "检查 config.json 是否为合法 JSON")
        return {}


def _kb_ready():
    """知识库文件存在且已建表（避免把「没 setup」误报成「库损坏」）。"""
    if not os.path.exists(paths.KB):
        return False
    import sqlite3  # noqa: PLC0415
    try:
        c = sqlite3.connect(paths.KB)
        ok = c.execute("SELECT 1 FROM sqlite_master"
                       " WHERE type='table' AND name='atom'").fetchone()
        c.close()
        return bool(ok)
    except sqlite3.Error:
        return False


def check_kb(r):
    if not _kb_ready():
        return r.add("kb", False, "知识库未初始化", "先跑：minime setup")
    try:
        import retrieval as R  # noqa: PLC0415
        conn = R.connect()
        n = conn.execute("SELECT COUNT(*) FROM atom WHERE state='active'").fetchone()[0]
        f = conn.execute("SELECT COUNT(*) FROM facet").fetchone()[0]
        r.add("kb", n > 0, f"{n} 条知识 · {f} 个标签",
              "知识库为空：先跑 minime setup")
        hits = R.search(conn, "配置", limit=3)
        r.add("kb.fts", len(hits) > 0, f"检索命中 {len(hits)} 条",
              "FTS 为空：跑 minime setup 重建索引")
        return conn
    except Exception as e:  # noqa: BLE001
        r.add("kb", False, f"{type(e).__name__}: {e}", "检查 store/kb.db 是否损坏")
        return None


def check_index(r):
    p = paths.INDEX
    if not os.path.exists(p):
        return r.add("index", False, "store/index.db 不存在",
                     "跑 minime collect 后重试")
    import sqlite3  # noqa: PLC0415
    c = sqlite3.connect(p)
    ws = c.execute("SELECT COUNT(*) FROM workspace").fetchone()[0]
    ss = c.execute("SELECT COUNT(*) FROM session").fetchone()[0]
    c.close()
    r.add("index", ss > 0, f"{ws} 工作区 · {ss} 会话")


def check_adapters(r):
    try:
        import collect as C  # noqa: PLC0415
        conn = C.init_db()
        for a in C.registry(conn):
            try:
                ok, reason = a.probe()
            except Exception as e:  # noqa: BLE001
                ok, reason = False, f"{type(e).__name__}: {e}"
            r.add(f"adapter.{a.name}", ok, reason,
                  "" if ok else (a.fix_hint or
                                 "该源不可用不会影响其他源；按提示补充配置或登录"))
    except Exception as e:  # noqa: BLE001
        r.add("adapters", False, f"{type(e).__name__}: {e}")


def check_llm(r, cfg):
    if not (cfg.get("llm") or {}).get("enabled"):
        return r.add("llm", True, "未启用（可选）", "")
    try:
        import llm_enrich as L  # noqa: PLC0415
        h = L.health(cfg)
        r.add("llm", h.get("ok"), h.get("error") or
              f"{h.get('using')} · 可用 {len(h.get('models') or [])} 个模型",
              "" if h.get("ok") else
              "检查本地模型服务是否启动；地址与 API Key 在设置页修改")
    except Exception as e:  # noqa: BLE001
        r.add("llm", False, f"{type(e).__name__}: {e}")


def check_embedding(r, cfg):
    e = cfg.get("embedding") or {}
    import embed  # noqa: PLC0415
    if not e.get("enabled"):
        return r.add("embedding", True, "已关闭，检索只用关键词", "")
    if not embed.healthy(e):
        return r.add("embedding", False, "已开启但未填接口地址 / 模型，检索自动降级为关键词",
                     "设置页「向量检索」填 base_url 与 model，或关闭该开关")
    try:
        import retrieval as R  # noqa: PLC0415
        conn = R.connect()
        try:
            R._ensure_gate(conn)
            vec = embed.embed_texts(e, ["minime 自检"],
                                    timeout=float(e.get("query_timeout_s") or 4))[0]
            cached = conn.execute("SELECT COUNT(*) FROM atom_embedding WHERE model=?",
                                  (e.get("model"),)).fetchone()[0]
            ids, mat = embed.load_matrix(conn, e)
        finally:
            conn.close()
        extra = (f" · 矩阵 {len(ids)}×{mat.shape[1]}（numpy）" if mat is not None
                 else " · 矩阵未启用（装 numpy 可加速）")
        r.add("embedding", True,
              f"{e.get('model')} · 维度 {len(vec)} · 已缓存 {cached} 条{extra}"
              f" · text_mode={embed.text_mode(e)}",
              "" if cached else "尚无缓存向量：运行一次 minime update 生成")
    except Exception as ex:  # noqa: BLE001
        r.add("embedding", False, f"{type(ex).__name__}: {ex}",
              "embedding 服务不可达时检索会自动降级为关键词；检查接口地址")


def check_mnemosyne(r, cfg):
    try:
        import memory_sync as M  # noqa: PLC0415
        exists = os.path.exists(M.MNEMOSYNE)
        r.add("mnemosyne.cli", exists, M.MNEMOSYNE,
              "" if exists else "未安装 mnemosyne，记忆同步不可用（不影响其他功能）")
        if exists:
            bank = (cfg.get("memory_sync") or {}).get("bank")
            out = M._cli("bank", "list", timeout=20)
            ok = bank in out
            r.add("mnemosyne.bank", ok, f"目标 bank: {bank}",
                  "" if ok else f"先创建：mnemosyne bank create {bank}")
    except Exception as e:  # noqa: BLE001
        r.add("mnemosyne", False, f"{type(e).__name__}: {e}")


def check_mcp(r):
    try:
        sys.path.insert(0, os.path.join(ROOT, "mcp_server"))
        import inspect  # noqa: PLC0415
        import server as S  # noqa: PLC0415
        names = sorted(n for n, f in inspect.getmembers(S, inspect.isfunction)
                       if n.startswith("minime_") and f.__module__ == S.__name__)
        r.add("mcp", len(names) > 0, f"{len(names)} 个工具 · "
              + ", ".join(names[:4]) + ("…" if len(names) > 4 else ""))
    except Exception as e:  # noqa: BLE001
        r.add("mcp", False, f"{type(e).__name__}: {e}",
              "MCP 服务无法加载：检查 mcp 包是否安装")


def check_schedule(r, cfg):
    sched = cfg.get("schedule") or {}
    if not sched.get("enabled", True):
        return r.add("schedule", True, "已在设置中关闭（schedule.enabled=false）")
    minutes = sched.get("interval_minutes", 30)
    task = "ai.minime.collect"
    if os.name == "nt":
        import subprocess  # noqa: PLC0415
        ok = subprocess.run(["schtasks", "/Query", "/TN", task],
                            capture_output=True).returncode == 0
        return r.add("schedule", ok, f"每 {minutes} 分钟 · 计划任务 {task}",
                     "安装：minime install（或 minime set schedule.enabled false 关闭此检查）")
    if sys.platform != "darwin":
        return r.add("schedule", True, "当前系统未提供定时任务，可用 minime watch 常驻")
    plist = os.path.expanduser(f"~/Library/LaunchAgents/{task}.plist")
    if not os.path.exists(plist):
        return r.add("schedule", False, "定时任务未安装",
                     "安装：minime install（或 minime set schedule.enabled false 关闭此检查）")
    import re  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    out = subprocess.run(["launchctl", "list", task],
                         capture_output=True, text=True).stdout
    m = re.search(r'"LastExitStatus"\s*=\s*(-?\d+)', out)
    code = m.group(1) if m else "未知"
    r.add("schedule", code in ("0", "未知"),
          f"每 {minutes} 分钟 · 上次退出码 {code}",
          f"看 {paths.LOGS} 下的 launchd.err，或临时改用手动：minime update")


def check_llm_progress(r):
    if not _kb_ready():
        return r.add("llm.progress", False, "知识库未初始化", "先跑：minime setup")
    try:
        import progress as P  # noqa: PLC0415
        pr = P.llm_progress()
        r.add("llm.progress", True,
              f"{pr['percent']}% · {pr['processed']}/{pr['candidates']} 轮 · 待处理 {pr['pending']}")
    except Exception as e:  # noqa: BLE001
        r.add("llm.progress", False, f"{type(e).__name__}: {e}")


def check_writer_mode(r):
    """回归：LLM 归纳必须用增量 Writer，否则会把全库替换成只剩归纳产物。"""
    try:
        import inspect  # noqa: PLC0415
        import llm_enrich as L  # noqa: PLC0415
        src = inspect.getsource(L.run) + inspect.getsource(
            __import__("progress").run_llm_job)
        bad = "full=False" not in src
        r.add("writer.llm_incremental", not bad,
              "归纳走增量写入" if not bad else "归纳使用了重建模式 Writer",
              "" if not bad else "llm_enrich.run 与 progress.run_llm_job 必须用 Writer(full=False)")
    except Exception as e:  # noqa: BLE001
        r.add("writer.llm_incremental", False, f"{type(e).__name__}: {e}")


def check_packs(r):
    """资源包：默认包必须存在且知识都有归属；开关过滤不能把库搜空。"""
    if not _kb_ready():
        return r.add("packs", False, "知识库未初始化", "先跑：minime setup")
    try:
        import packs as PK  # noqa: PLC0415
        import retrieval as R  # noqa: PLC0415
        conn = R.connect()
        PK.ensure(conn)
        orphan = conn.execute(
            "SELECT COUNT(*) FROM atom a WHERE state='active' AND NOT EXISTS"
            " (SELECT 1 FROM pack_atom pa WHERE pa.atom_id=a.atom_id)").fetchone()[0]
        n_packs = conn.execute("SELECT COUNT(*) FROM pack").fetchone()[0]
        n_coll = conn.execute("SELECT COUNT(*) FROM pack_collection").fetchone()[0]
        hits = len(R.search(conn, "架构", limit=3))
        ok = orphan == 0 and n_packs >= 1
        r.add("packs", ok,
              f"{n_packs} 个包 · {n_coll} 套知识集 · 无归属 {orphan} 条 · 检索命中 {hits}",
              "" if ok else "有知识未归属任何知识集：跑 minime update 或点资源包页「补判」")
    except Exception as e:  # noqa: BLE001
        r.add("packs", False, f"{type(e).__name__}: {e}")


def check_web(r, cfg):
    import urllib.request  # noqa: PLC0415
    port = (cfg.get("web") or {}).get("port") or 8765
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health", timeout=3) as resp:
            data = json.loads(resp.read().decode())
        r.add("web", True, f"端口 {port} · {data.get('ok')}")
    except Exception:  # noqa: BLE001
        r.add("web", True, f"端口 {port} 未运行（需要时 ./run/minime.sh web）")


def run():
    r = Result()
    cfg = check_config(r)
    check_kb(r)
    check_index(r)
    check_adapters(r)
    check_llm(r, cfg)
    check_llm_progress(r)
    check_embedding(r, cfg)
    check_mnemosyne(r, cfg)
    check_mcp(r)
    check_writer_mode(r)
    check_packs(r)
    check_schedule(r, cfg)
    check_web(r, cfg)
    return r.report()


def main():
    rep = run()
    if "--json" in sys.argv:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(f"minime 自检：{rep['passed']}/{rep['total']} 通过")
        for i in rep["items"]:
            mark = "✓" if i["ok"] else "✗"
            print(f"  {mark} {i['check']:24s} {i['detail']}")
            if not i["ok"] and i["hint"]:
                print(f"      → {i['hint']}")
    sys.exit(0 if rep["ok"] else 1)


if __name__ == "__main__":
    main()
