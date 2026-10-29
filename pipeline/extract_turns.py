"""从快照 git 抽取「每轮对话的文件变更轨迹」。

Trae 的 snapshot git 只有两类有意义的提交：
  before-chat-turn-<turn_id>   每轮开始前的基线
  after-chat-turn-<turn_id>    该轮所有工具调用完成后的结果（tag）
  toolcall-<turn_id>-<tc_id>   中间态（可回溯单次工具改动）

这里把 before^..after 的差异抽成 turn / change 两张表，并保留 after 态的文件全文。
"""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "collector"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "pipeline"))
import paths  # noqa: E402
from collect import DB, SNAPSHOT  # noqa: E402

# 路径判定：首段是 disk / base / without 之一，第三段起才是真实文件路径
PREFIXES = ("disk/content/", "base/content/", "base/without/", "without/", "content/")


def git(repo, *args, binary=False):
    p = subprocess.run(["git", "-C", repo, *args], capture_output=True,
                       text=not binary)
    return p.stdout if p.returncode == 0 else (b"" if binary else "")


def real_path(raw):
    for p in PREFIXES:
        if raw.startswith(p):
            return raw[len(p):], p.split("/")[0]
    return None, None


def ensure_schema(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS turn (
      turn_id     TEXT PRIMARY KEY,
      session_id  TEXT NOT NULL,
      ts          INTEGER,
      commit_before TEXT,
      commit_after  TEXT,
      n_files     INTEGER,
      n_added     INTEGER,
      n_modified  INTEGER,
      n_deleted   INTEGER,
      additions   INTEGER,
      deletions   INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_turn_session ON turn(session_id, ts);
    CREATE TABLE IF NOT EXISTS turn_file (
      turn_id   TEXT NOT NULL,
      session_id TEXT NOT NULL,
      path      TEXT NOT NULL,
      action    TEXT,
      additions INTEGER,
      deletions INTEGER,
      blob_after TEXT,
      blob_before TEXT,
      UNIQUE(turn_id, path)
    );
    CREATE INDEX IF NOT EXISTS idx_turnfile_path ON turn_file(path);
    CREATE TABLE IF NOT EXISTS file_state (
      session_id TEXT NOT NULL,
      path       TEXT NOT NULL,
      blob       TEXT NOT NULL,
      ts         INTEGER,
      UNIQUE(session_id, path)
    );
    CREATE INDEX IF NOT EXISTS idx_filestate_blob ON file_state(blob);
    """)
    conn.commit()


SNIPPETS = paths.SNIPPETS


def _write_snippet(repo, blob, path):
    """把改动后文件的前 4KB 落到 store/snippets，供 LLM 二次归纳读取真实内容。"""
    safe = path.replace("/", "_")[:120]
    full = os.path.join(SNIPPETS, safe)
    if os.path.exists(full) and os.path.getsize(full) > 0:
        return
    data = git(repo, "cat-file", "-p", blob, binary=True)[:4096]
    os.makedirs(SNIPPETS, exist_ok=True)
    with open(full, "wb") as f:
        f.write(data)


def parse_numstat(text):
    out = {}
    for line in text.split("\n"):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        a, d, path = parts
        out[path] = (int(a) if a.isdigit() else 0, int(d) if d.isdigit() else 0)
    return out


def run(limit_sessions=None):
    conn = paths.db_connect(DB)
    ensure_schema(conn)
    t0 = time.time()
    stats = {"sessions": 0, "turns": 0, "files": 0, "skipped": 0}
    for sid in sorted(os.listdir(SNAPSHOT)):
        repo = os.path.join(SNAPSHOT, sid, "v2")
        if not os.path.isdir(os.path.join(repo, ".git")):
            continue
        # 已处理过且 tag 数没变化的会话跳过
        n_tags = len([l for l in git(repo, "show-ref", "--tags").split("\n")
                      if l.strip()])
        done = conn.execute("SELECT COUNT(*) FROM turn WHERE session_id=?", (sid,)).fetchone()[0]
        if done and done >= n_tags:
            stats["skipped"] += 1
            continue
        log = git(repo, "log", "--all", "--format=%H%x01%at%x01%s")
        commits = {}
        for line in log.strip().split("\n"):
            if not line:
                continue
            h, ts, subj = line.split("\x01", 2)
            commits[subj] = (h, int(ts))
        for line in git(repo, "show-ref", "--tags").split("\n"):
            if not line.strip():
                continue
            h, ref = line.split(" ", 1)
            subj = ref.replace("refs/tags/", "")
            if subj.startswith("after-chat-turn-"):
                ts = git(repo, "log", "-1", "--format=%at", h).strip() or "0"
                commits[subj] = (h, int(ts))
        turns = []
        for subj, (h, ts) in commits.items():
            if subj.startswith("after-chat-turn-"):
                turns.append((subj[len("after-chat-turn-"):], h, ts))
        stats["sessions"] += 1
        for turn_id, after, ts in turns:
            before = commits.get(f"before-chat-turn-{turn_id}")
            before_hash = before[0] if before else git(
                repo, "rev-parse", f"{after}^").strip()
            numstat = parse_numstat(git(repo, "diff", "--numstat", "-M",
                                        before_hash, after))
            name_status = git(repo, "diff", "--name-status", "-M", before_hash, after)
            rows = []
            for line in name_status.split("\n"):
                if not line.strip():
                    continue
                parts = line.split("\t")
                action = parts[0][:1]
                raw = parts[-1]
                rp, layer = real_path(raw)
                if not rp or layer not in ("disk", "base"):
                    continue  # 只认 disk/base 层真实文件
                a, d = numstat.get(raw, (0, 0))
                blob_after = git(repo, "rev-parse", f"{after}:{raw}").strip()
                blob_before = ""
                if action != "A":
                    blob_before = git(repo, "rev-parse",
                                      f"{before_hash}:{raw}").strip()
                rows.append((turn_id, sid, rp, action, a, d, blob_after, blob_before))
            if not rows:
                continue
            n_add = sum(1 for r in rows if r[3] == "A")
            n_mod = sum(1 for r in rows if r[3] == "M")
            n_del = sum(1 for r in rows if r[3] == "D")
            conn.execute("INSERT OR REPLACE INTO turn VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (turn_id, sid, ts, before_hash, after, len(rows),
                          n_add, n_mod, n_del,
                          sum(r[4] for r in rows), sum(r[5] for r in rows)))
            conn.executemany(
                "INSERT OR REPLACE INTO turn_file VALUES (?,?,?,?,?,?,?,?)", rows)
            for r in rows:
                if r[6]:
                    conn.execute(
                        "INSERT OR REPLACE INTO file_state VALUES (?,?,?,?)",
                        (sid, r[2], r[6], ts))
                    _write_snippet(repo, r[6], r[2])
            stats["turns"] += 1
            stats["files"] += len(rows)
        conn.commit()
    stats["elapsed_s"] = round(time.time() - t0, 1)
    return stats


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
