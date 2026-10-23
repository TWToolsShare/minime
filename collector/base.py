"""采集适配器框架。

每个信息源实现一个 SourceAdapter，统一四件套：
  probe()    能否采集（登录态/路径/依赖）
  collect()  采集并落 raw 归档（内容寻址，可重复执行）
  stats()    规模（用于增量判断与报表）
  verify()   数据是否真的可用（自检）

自动采集（AutoCollector）：
  - 每个适配器声明 interval_s（默认 900s）与 enabled
  - 用 store/collect_state 记录上次运行时间与结果，到点才跑
  - 失败自动退避（interval * 2^n，上限 1h），成功后清零
  - 单源异常不影响其他源（隔离）

新增信息源只需写一个类并 register()，无需改调度与流水线。
"""
import hashlib
import json
import os
import sqlite3
import sys
import time
import traceback

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pipeline"))
import paths  # noqa: E402

ROOT = paths.CODE_ROOT
RAW = paths.RAW
MANIFEST = paths.MANIFEST

HOME = os.path.expanduser("~")


def _trae_root():
    """Trae 用户数据目录（可用 MINIME_TRAE_DIR 覆盖）。"""
    env = os.environ.get("MINIME_TRAE_DIR")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    if os.name == "nt":
        return os.path.join(os.environ.get("APPDATA") or HOME, "Trae")
    if sys.platform == "darwin":
        return os.path.join(HOME, "Library/Application Support/Trae")
    return os.path.join(os.environ.get("XDG_CONFIG_HOME")
                        or os.path.join(HOME, ".config"), "Trae")


TRAE = _trae_root()
SNAPSHOT = os.path.join(TRAE, "ModularData/ai-agent/snapshot")
WS_STORAGE = os.path.join(TRAE, "User/workspaceStorage")
HISTORY = os.path.join(TRAE, "User/History")
MEMORY = os.path.join(HOME, ".trae/memory")

_REGISTRY = []


def _log(status, name, **fields):
    """采集日志（失败不影响采集本身）。"""
    try:
        import oplog  # noqa: PLC0415
        level = {"ok": "info", "not_due": "info", "unavailable": "warn"}.get(status, "error")
        oplog.write(level, f"collect.{name}", status=status, **fields)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- 归档
def init_db():
    paths.ensure()
    os.makedirs(RAW, exist_ok=True)
    c = sqlite3.connect(MANIFEST)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS raw_object (
      sha256 TEXT PRIMARY KEY,
      kind   TEXT NOT NULL,
      source TEXT NOT NULL,
      path   TEXT NOT NULL,
      size   INTEGER NOT NULL,
      created_at INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_raw_kind ON raw_object(kind, source);
    CREATE TABLE IF NOT EXISTS collect_state (
      adapter      TEXT PRIMARY KEY,
      last_run     INTEGER,
      last_ok      INTEGER,
      last_status  TEXT,
      last_error   TEXT,
      last_elapsed REAL,
      last_payload TEXT,
      runs         INTEGER DEFAULT 0,
      fails        INTEGER DEFAULT 0
    );
    """)
    c.commit()
    return c


def put_raw(conn, kind, source, payload: bytes) -> str:
    """内容寻址落盘；同内容重复写自动去重。"""
    h = hashlib.sha256(payload).hexdigest()
    day = time.strftime("%Y-%m-%d")
    rel = os.path.join(day, f"{h}.json")
    full = os.path.join(RAW, rel)
    row = conn.execute("SELECT 1 FROM raw_object WHERE sha256=?", (h,)).fetchone()
    if not row:
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(payload)
        conn.execute("INSERT INTO raw_object VALUES (?,?,?,?,?,?)",
                     (h, kind, source, rel, len(payload), int(time.time())))
        conn.commit()
    return h


def raw_known(conn, kind, source) -> bool:
    return conn.execute("SELECT 1 FROM raw_object WHERE kind=? AND source=? LIMIT 1",
                        (kind, source)).fetchone() is not None


# ---------------------------------------------------------------- 适配器基类
class SourceAdapter:
    """信息源适配器基类。子类只需实现 probe/collect。"""

    name = "base"
    # 落 raw 的 kind 前缀，用于统计与下游识别
    kind = "base"
    DEFAULT_INTERVAL_S = 900  # 自动采集周期默认值（可被 config.json 覆盖，见 interval_s 属性）
    priority = 50             # 越小越先跑
    description = ""
    label = ""                # 界面用的中文名
    fix_hint = ""             # 不可用时给用户的一步动作

    def __init__(self, conn):
        self.conn = conn
        self.errors = []

    @property
    def interval_s(self):
        """采集周期：设置页配置 > 类默认值（子类可不改代码调整节奏）。"""
        default = type(self).DEFAULT_INTERVAL_S
        try:
            import sys as _sys  # noqa: PLC0415
            sys_path = os.path.join(ROOT, "pipeline")
            if sys_path not in _sys.path:
                _sys.path.insert(0, sys_path)
            import config  # noqa: PLC0415
            v = ((config.load().get("collect") or {}).get("intervals") or {}).get(self.name)
            return int(v) if v else default
        except Exception:  # noqa: BLE001
            return default

    # ---- 必须实现
    def probe(self):
        """返回 (ok: bool, reason: str)。"""
        raise NotImplementedError

    def collect(self):
        """采集并写 raw；返回统计 dict（会写进 collect_state.last_payload）。"""
        raise NotImplementedError

    # ---- 可选覆盖
    def verify(self):
        """自检：返回 (ok, detail)。默认看本 kind 是否已有归档。"""
        n = self.conn.execute("SELECT COUNT(*) FROM raw_object WHERE kind=?",
                              (self.kind,)).fetchone()[0]
        return n > 0, f"{n} objects"

    def stats(self):
        n, size = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size),0) FROM raw_object WHERE kind=?",
            (self.kind,)).fetchone()
        return {"objects": n, "bytes": size}

    # ---- 框架能力
    def state(self):
        cur = self.conn.execute("SELECT * FROM collect_state WHERE adapter=?",
                                (self.name,))
        names = [d[0] for d in cur.description]
        r = cur.fetchone()
        return dict(zip(names, r)) if r else None

    def due(self, now=None, force=False):
        """是否到点该跑（自动采集的核心判据）。"""
        if force:
            return True
        st = self.state()
        if not st or not st["last_run"]:
            return True
        fails = st.get("fails") or 0
        backoff = min(self.interval_s * (2 ** min(fails, 4)), 3600)
        return (now or time.time()) - st["last_run"] >= backoff

    def run(self):
        """执行一次采集：probe -> collect，记录状态，异常隔离。"""
        t0 = time.time()
        status, payload, error = "ok", {}, ""
        try:
            ok, reason = self.probe()
            if not ok:
                status, error = "unavailable", reason
            else:
                payload = self.collect() or {}
        except Exception as e:  # noqa: BLE001 — 单源失败不能拖垮整体
            status = "error"
            error = f"{type(e).__name__}: {e}"
            self.errors.append(traceback.format_exc()[-800:])
        elapsed = round(time.time() - t0, 2)
        prev = self.state() or {}
        fails = 0 if status == "ok" else (prev.get("fails") or 0) + 1
        self.conn.execute(
            """INSERT INTO collect_state
               (adapter,last_run,last_ok,last_status,last_error,last_elapsed,
                last_payload,runs,fails)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(adapter) DO UPDATE SET
                 last_run=excluded.last_run, last_ok=excluded.last_ok,
                 last_status=excluded.last_status, last_error=excluded.last_error,
                 last_elapsed=excluded.last_elapsed, last_payload=excluded.last_payload,
                 runs=collect_state.runs+1, fails=excluded.fails""",
            (self.name, int(time.time()), 1 if status == "ok" else 0, status,
             error, elapsed, json.dumps(payload, ensure_ascii=False), 1, fails))
        self.conn.commit()
        _log(status, self.name, elapsed=elapsed, fails=fails,
             error=error, **{k: v for k, v in payload.items()
                             if isinstance(v, (int, float, str))})
        return {"adapter": self.name, "status": status, "error": error,
                "elapsed_s": elapsed, **payload}


# ---------------------------------------------------------------- 注册
def register(cls):
    _REGISTRY.append(cls)
    return cls


def registry(conn, names=None):
    """按 priority 排序实例化适配器；names 可过滤。"""
    out = []
    for cls in sorted(_REGISTRY, key=lambda c: c.priority):
        if names and cls.name not in names:
            continue
        out.append(cls(conn))
    return out


def all_adapters():
    return sorted(_REGISTRY, key=lambda c: c.priority)


# ---------------------------------------------------------------- 自动采集
class AutoCollector:
    """按各适配器声明的 interval_s 调度；支持单次 tick 与常驻循环。"""

    def __init__(self, conn=None, names=None):
        self.conn = conn or init_db()
        self.names = names

    def tick(self, force=False, now=None):
        results = []
        for a in registry(self.conn, self.names):
            if not a.due(now=now, force=force):
                results.append({"adapter": a.name, "status": "not_due",
                                "next_in_s": self._next_in(a, now)})
                continue
            results.append(a.run())
        return results

    def _next_in(self, a, now=None):
        st = a.state() or {}
        if not st.get("last_run"):
            return 0
        fails = st.get("fails") or 0
        wait = min(a.interval_s * (2 ** min(fails, 4)), 3600)
        return max(0, int(wait - ((now or time.time()) - st["last_run"])))

    def loop(self, once=False, sleep_s=30, on_tick=None):
        """常驻：每 sleep_s 检查一次，到点的源才真正跑。"""
        while True:
            res = self.tick()
            if on_tick:
                on_tick(res)
            else:
                print(json.dumps({"tick": res}, ensure_ascii=False), flush=True)
            if once:
                return res
            time.sleep(sleep_s)

    def status(self):
        out = []
        for a in registry(self.conn, self.names):
            st = a.state() or {}
            ok, detail = True, ""
            try:
                ok, detail = (a.probe() if not st else a.verify())
            except Exception as e:  # noqa: BLE001
                ok, detail = False, f"{type(e).__name__}: {e}"
            out.append({
                "adapter": a.name, "description": a.description,
                "label": a.label or a.name, "fix_hint": a.fix_hint,
                "interval_s": a.interval_s, "health": ok, "detail": detail,
                "last_run": st.get("last_run"), "last_status": st.get("last_status"),
                "last_error": st.get("last_error"), "last_elapsed": st.get("last_elapsed"),
                "fails": st.get("fails") or 0, "runs": st.get("runs") or 0,
                "next_in_s": self._next_in(a), **a.stats(),
            })
        return {"adapters": out,
                "archive": dict(zip(("objects", "bytes"), self.conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(size),0) FROM raw_object").fetchone()))}
