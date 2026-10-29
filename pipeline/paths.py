"""代码目录与数据目录分离（跨平台）。

代码目录：本仓库/安装目录，只读，随版本升级整体替换。
数据目录：知识库、配置、原始归档、日志、资源包，跨升级保留。

默认数据目录（可用 MINIME_HOME 覆盖）：
  macOS   ~/Library/Application Support/minime
  Windows %LOCALAPPDATA%\\minime
  Linux   ~/.local/share/minime

老版本把数据放在代码目录下（<code>/store、<code>/config.json）。
首次运行时会自动搬到数据目录，搬完代码目录即可安全替换/升级。
"""
import os
import shutil
import sqlite3
import sys

CODE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def db_connect(path, row_factory=None, timeout=30, check_same_thread=True):
    """统一数据库连接工厂：持久化 WAL + 大 busy_timeout，缓解并发锁。

    任何到 minime 数据库的读写都应走这里，避免裸 sqlite3.connect 各设各的、
    默认值（busy_timeout=5000ms）偏小导致 database is locked。
    check_same_thread=False 用于需跨线程访问的库（如 jobs.db、Web 面板共享）。
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=timeout, check_same_thread=check_same_thread)
    if row_factory is not None:
        conn.row_factory = row_factory
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
    except sqlite3.Error:
        pass
    return conn


def _default_home():
    env = os.environ.get("MINIME_HOME")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/minime")
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "minime")
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "minime")


DATA_ROOT = _default_home()
STORE = os.path.join(DATA_ROOT, "store")
RAW = os.path.join(STORE, "raw")
SNIPPETS = os.path.join(STORE, "snippets")
KB = os.path.join(STORE, "kb.db")
MANIFEST = os.path.join(STORE, "manifest.db")
INDEX = os.path.join(STORE, "index.db")
JOBS = os.path.join(STORE, "jobs.db")
CHAT = os.path.join(STORE, "chat.db")
LOGS = os.path.join(STORE, "logs")
PACKS = os.path.join(STORE, "packs")
DIAG = os.path.join(STORE, "diag")
VEC_CACHE = os.path.join(STORE, "vec_cache")
CONFIG = os.path.join(DATA_ROOT, "config.json")
LOCK = os.path.join(STORE, ".pipeline.lock")

_ENSURED = False


def _legacy_paths():
    """旧版布局：数据就在代码目录里。"""
    return (os.path.join(CODE_ROOT, "store"),
            os.path.join(CODE_ROOT, "config.json"))


def has_kb(path=None):
    """数据目录里是否已经有可用的知识库（有 atom 表才算）。"""
    path = path or KB
    if not os.path.exists(path):
        return False
    import sqlite3  # noqa: PLC0415
    try:
        c = db_connect(path)
        ok = c.execute("SELECT 1 FROM sqlite_master"
                       " WHERE type='table' AND name='atom'").fetchone()
        c.close()
        return bool(ok)
    except sqlite3.Error:
        return False


def _purge_empty_store():
    """删掉没有知识库的空壳数据库（首次 doctor 会顺手建出空表）。"""
    if has_kb():
        return
    for name in ("kb.db", "manifest.db", "index.db", "jobs.db", "chat.db"):
        for suffix in ("", "-wal", "-shm"):
            p = os.path.join(STORE, name + suffix)
            if os.path.exists(p):
                os.remove(p)


def _move_merge(src, dst):
    """移动文件/目录到 dst；目录已存在时逐项合并，返回实际移动的条目数。"""
    if os.path.isdir(src) and os.path.isdir(dst):
        n = 0
        for name in os.listdir(src):
            n += _move_merge(os.path.join(src, name), os.path.join(dst, name))
        try:
            os.rmdir(src)
        except OSError:
            pass
        return n
    if os.path.exists(dst):
        return 0
    shutil.move(src, dst)
    return 1


def migrate_from(root):
    """把旧代码目录里的 store/ 与 config.json 搬到数据目录。

    幂等合并：目标已有的条目跳过，目录逐项合并；返回实际移动的顶层条目名。
    """
    src_store = os.path.join(root, "store")
    src_cfg = os.path.join(root, "config.json")
    os.makedirs(STORE, exist_ok=True)
    if not has_kb():
        _purge_empty_store()
    moved = []
    if os.path.isdir(src_store):
        for name in os.listdir(src_store):
            if _move_merge(os.path.join(src_store, name), os.path.join(STORE, name)):
                moved.append(name)
        try:
            if os.listdir(src_store) == [".pipeline.lock"]:
                os.remove(os.path.join(src_store, ".pipeline.lock"))
        except OSError:
            pass
        try:
            os.rmdir(src_store)
        except OSError:
            pass
    if os.path.exists(src_cfg) and not os.path.exists(CONFIG):
        shutil.move(src_cfg, CONFIG)
        moved.append("config.json")
    return moved


def ensure():
    """确保数据目录存在；有旧版数据时搬一次。幂等，进程内只做一次。"""
    global _ENSURED
    if _ENSURED:
        return DATA_ROOT
    _ENSURED = True
    if has_kb():
        return DATA_ROOT
    old_store, old_config = _legacy_paths()
    os.makedirs(STORE, exist_ok=True)
    if not (os.path.isdir(old_store) or os.path.exists(old_config)):
        return DATA_ROOT
    try:
        moved = migrate_from(CODE_ROOT)
    except OSError as e:  # 迁移失败不阻塞启动，但要说清楚数据还在旧位置
        print(f"minime: 数据目录迁移失败（{e}）；数据仍在 {old_store}，"
              f"请手动移动到 {STORE} 后重试", file=sys.stderr)
        return DATA_ROOT
    if moved:
        print(f"minime: 数据已迁移到 {DATA_ROOT}（{len(moved)} 项），"
              f"代码目录现在可以安全升级", file=sys.stderr)
    return DATA_ROOT


# ---------------------------------------------------------------- 跨平台文件锁
def acquire_lock(path=LOCK, blocking=False):
    """非阻塞拿排他锁。成功返回文件对象（调用方需保持引用），失败返回 None。

    POSIX 用 flock，Windows 用 msvcrt.locking；进程退出锁自动释放。
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    f = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt  # noqa: PLC0415
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl  # noqa: PLC0415
            flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            fcntl.flock(f, flags)
        return f
    except OSError:
        f.close()
        return None


def release_lock(f):
    if not f:
        return
    try:
        if os.name == "nt":
            import msvcrt  # noqa: PLC0415
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl  # noqa: PLC0415
            fcntl.flock(f, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        try:
            f.close()
        except OSError:
            pass
