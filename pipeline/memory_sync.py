"""minime -> mnemosyne 单向同步（可选，默认关闭）。

打开方式（二选一）：
  config.json:  "memory_sync": { "enabled": true, "bank": "case-analysis" }
  环境变量:     MINIME_MEMORY_SYNC=1

筛选条件（都满足才同步）：
  kind ∈ memory_sync.kinds
  trust_tier <= memory_sync.min_trust_tier
  veracity >= memory_sync.min_veracity
  未同步过（sync_cursor 记录）

同步内容为「结论 + 来源」，不含全量原文，避免记忆库被轨迹污染。
"""
import json
import os
import re
import sqlite3
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
from retrieval import connect  # noqa: E402

MNEMOSYNE = os.path.expanduser("~/.local/bin/mnemosyne")
# 可选：同步到哪个记忆后端。mnemosyne=本地 CLI；其余为预留
BACKENDS = ("mnemosyne",)
BANKS = ("case-analysis", "personal", "project", "user_profile")


def ensure_cursor(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS sync_cursor (
        atom_id TEXT PRIMARY KEY,
        memory_id TEXT,
        synced_at INTEGER NOT NULL)""")
    conn.commit()


def stats(conn):
    """同步概览：哪些候选、已同步多少、按 kind 分布。"""
    n_synced = conn.execute(
        "SELECT COUNT(*) FROM sync_cursor").fetchone()[0] if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='sync_cursor'").fetchone() else 0
    by_kind = {r[0]: r[1] for r in conn.execute(
        "SELECT kind, COUNT(*) FROM atom WHERE state='active' GROUP BY kind")}
    return {"synced": n_synced, "kb_atoms": by_kind, "banks": BANKS,
            "cli": os.path.exists(MNEMOSYNE)}


def candidates(conn, cfg):
    c = cfg["memory_sync"]
    kinds = c["kinds"]
    ph = ",".join("?" * len(kinds))
    rows = conn.execute(f"""
        SELECT a.* FROM atom a
        LEFT JOIN sync_cursor s ON s.atom_id=a.atom_id
        WHERE s.atom_id IS NULL AND a.state='active'
          AND a.kind IN ({ph})
          AND a.trust_tier <= ?
          AND a.veracity >= ?
        ORDER BY a.static_importance DESC, a.updated_at DESC
        LIMIT ?""", [*kinds, c["min_trust_tier"], c["min_veracity"],
                     c["max_per_run"]]).fetchall()
    return rows


def _cli(*args, timeout=120):
    p = subprocess.run([MNEMOSYNE, *args], capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout)[-500:])
    return p.stdout


def use_bank(bank):
    """mnemosyne CLI 无 --bank 参数，靠 config 切换当前 bank。"""
    if not bank:
        return None
    return _cli("config", "set", "bank", bank, timeout=30)


def remember(atom, bank=None):
    """调用 mnemosyne CLI 写入一条记忆。

    CLI 签名：mnemosyne store <content> [source] [importance]
    目标 bank 由 use_bank() 先行切换。
    """
    content = f"{atom['title']}\n\n{atom['body']}"
    source = f"minime:{atom['kind']}:{atom['scope'] or 'global'}"
    imp = round(min(0.95, max(0.1, float(atom["static_importance"] or 0.5))), 2)
    out = _cli("store", content, source, str(imp))
    # 输出形如 "Stored: f025739742ab3e1a"
    m = re.search(r"Stored:\s*(\S+)", out)
    return m.group(1) if m else None


def run(dry_run=False):
    cfg = config.load()
    mc = cfg["memory_sync"]
    if not mc["enabled"] and not dry_run:
        return {"skipped": "memory_sync.enabled=false（在 config.json 或 MINIME_MEMORY_SYNC=1 打开）"}
    conn = connect()
    ensure_cursor(conn)
    rows = candidates(conn, cfg)
    if dry_run:
        return {"dry_run": True, "candidates": [
            {"atom_id": r["atom_id"], "kind": r["kind"], "title": r["title"],
             "veracity": r["veracity"], "trust_tier": r["trust_tier"]} for r in rows]}
    ok = fail = 0
    try:
        use_bank(mc["bank"])
    except (RuntimeError, subprocess.SubprocessError) as e:
        return {"error": f"切换 bank 失败（bank 不存在？先 mnemosyne bank create {mc['bank']}）: {e}"}
    for r in rows:
        try:
            mid = remember(r)
            conn.execute("INSERT OR REPLACE INTO sync_cursor VALUES (?,?,?)",
                         (r["atom_id"], mid or "", int(__import__("time").time())))
            conn.commit()
            ok += 1
        except (RuntimeError, subprocess.SubprocessError) as e:
            print(f"[sync] {r['atom_id']} failed: {e}", file=sys.stderr)
            fail += 1
    return {"synced": ok, "failed": fail, "bank": mc["bank"]}


if __name__ == "__main__":
    print(json.dumps(run(dry_run="--dry-run" in sys.argv), ensure_ascii=False, indent=1))
