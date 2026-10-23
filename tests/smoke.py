"""CI 冒烟：在干净的数据目录里验证「装完就能跑」，不依赖本机任何数据。

覆盖：
  - CLI 入口可用（--help / status / doctor --json）
  - 空库下不崩，给出可执行提示；Web 面板能起、首页与健康检查可用
  - 代码/数据目录分离：数据目录落在 MINIME_HOME
  - 旧目录迁移（store + config.json）与文件锁、向量编解码
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
PORT = 8799


def run(*args, env):
    env = {**env, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run([PY, "-m", "minime_launcher", *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, timeout=300)


def wait_http(url, timeout=60):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.5)
    raise SystemExit(f"服务未起来：{url}（{last}）")


def parse_json(text):
    """doctor 会在 JSON 前打印提示行，取第一个 { 到最后一个 }。"""
    start, end = text.find("{"), text.rfind("}")
    assert start >= 0 and end > start, f"没有 JSON 输出：\n{text}"
    return json.loads(text[start:end + 1])


def check_cli(home):
    env = {**os.environ, "MINIME_HOME": home}
    p = run("--help", env=env)
    assert p.returncode == 0, p.stderr
    assert "minime" in p.stdout

    p = run("status", env=env)
    assert p.returncode == 1, p.stdout + p.stderr
    assert "知识库为空或未初始化" in p.stdout, p.stdout

    p = run("doctor", "--json", env=env)
    assert p.returncode in (0, 1), p.stderr
    report = parse_json(p.stdout)
    assert report["ok"] is False and report["total"] >= 15, report
    assert os.path.isdir(os.path.join(home, "store")), "数据目录没有落在 MINIME_HOME"


def check_web(home):
    env = {**os.environ, "MINIME_HOME": home, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen([PY, "-m", "minime_launcher", "web"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        health = wait_http(f"http://127.0.0.1:{PORT}/api/health")
        assert health["ok"] is True and health["atoms"] == 0, health
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=5) as r:
            assert r.status == 200
            assert "<html" in r.read().decode("utf-8", "replace").lower()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def check_modules(tmp):
    sys.path.insert(0, os.path.join(REPO, "pipeline"))
    import embed  # noqa: PLC0415
    import normalize  # noqa: PLC0415
    import paths  # noqa: PLC0415
    import schema  # noqa: PLC0415

    vec = [0.1, -0.25, 0.5]
    back = embed.decode(embed.encode(vec))
    assert len(back) == 3 and all(abs(a - b) < 1e-6 for a, b in zip(vec, back))
    grams = normalize.bigrams("中文检索")
    assert all(g in grams for g in ("中文", "文检", "检索")), grams

    # 文件锁：同一进程第二次拿不到
    lock = paths.acquire_lock(os.path.join(tmp, "test.lock"))
    assert lock is not None
    assert paths.acquire_lock(os.path.join(tmp, "test.lock")) is None
    paths.release_lock(lock)

    # FTS：建库 -> 写一条 -> 按标题命中
    conn = schema.init(os.path.join(tmp, "kb.db"))
    conn.execute("INSERT INTO atom(atom_id, kind, title, body, summary, state,"
                 " created_at, updated_at) VALUES ('A1','fact','向量检索',"
                 " '如何开启向量检索','摘要','active',0,0)")
    rowid = conn.execute("SELECT rowid FROM atom WHERE atom_id='A1'").fetchone()[0]
    conn.execute("INSERT INTO atom_fts(rowid, title, summary, body, tags)"
                 " VALUES (?,?,?,?,?)", (rowid, "向量检索", "摘要", "如何开启向量检索", ""))
    hit = conn.execute("SELECT rowid FROM atom_fts WHERE atom_fts MATCH ?",
                       ("向量检索",)).fetchone()
    assert hit and hit[0] == rowid, "FTS 检索没命中"
    conn.close()


def check_migrate(tmp):
    sys.path.insert(0, os.path.join(REPO, "pipeline"))
    import paths  # noqa: PLC0415

    legacy = os.path.join(tmp, "legacy")
    os.makedirs(os.path.join(legacy, "store", "raw", "2026-01-01"))
    with open(os.path.join(legacy, "store", "raw", "2026-01-01", "a.json"), "wb") as f:
        f.write(b"{}")
    with open(os.path.join(legacy, "config.json"), "w", encoding="utf-8") as f:
        f.write("{}")

    dest = os.path.join(tmp, "dest")
    paths.STORE = os.path.join(dest, "store")
    paths.KB = os.path.join(paths.STORE, "kb.db")
    paths.CONFIG = os.path.join(dest, "config.json")

    moved = paths.migrate_from(legacy)
    assert "config.json" in moved and "raw" in moved, moved
    assert os.path.isfile(os.path.join(paths.STORE, "raw", "2026-01-01", "a.json"))
    assert os.path.isfile(paths.CONFIG)
    assert not os.path.isdir(os.path.join(legacy, "store"))


def main():
    # Windows 控制台默认 cp1252，中文输出会直接抛 UnicodeEncodeError
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    home = tempfile.mkdtemp(prefix="minime-home-")
    tmp = tempfile.mkdtemp(prefix="minime-tmp-")
    os.makedirs(os.path.join(home, "store"), exist_ok=True)
    with open(os.path.join(home, "config.json"), "w", encoding="utf-8") as f:
        json.dump({"web": {"port": PORT}}, f)

    print("1/4 CLI（--help / status / doctor）")
    check_cli(home)
    print("2/4 模块（paths / embed / normalize / schema+FTS）")
    check_modules(tmp)
    print("3/4 旧目录迁移")
    check_migrate(tmp)
    print("4/4 Web 面板")
    check_web(home)
    print("冒烟通过")


if __name__ == "__main__":
    main()
