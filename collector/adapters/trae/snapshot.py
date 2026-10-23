"""适配器：Trae 快照 git。

数据形态：snapshot/<sid>/v2/.git
  before-chat-turn-<turn_id> / after-chat-turn-<turn_id>（在 refs/tags）
采集粒度：会话级（归档 git log + tags + refs 摘要）
"""
import json
import os
import subprocess
import sys

# collector/ 是 base 框架所在层；上溯三级，直接运行本文件也能调试
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
from base import SNAPSHOT, SourceAdapter, put_raw, register  # noqa: E402


def git(repo, *args, text=True):
    p = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=text)
    return p.stdout if p.returncode == 0 else ("" if text else b"")


@register
class SnapshotAdapter(SourceAdapter):
    name = "snapshot"
    kind = "snapshot_log"
    DEFAULT_INTERVAL_S = 900
    priority = 10
    description = "Trae 快照 git：每轮 AI 改动的全量文件快照与提交轨迹"
    label = "Trae 会话快照"
    fix_hint = "确认 Trae 已安装并至少进行过一次 AI 对话"

    def probe(self):
        if not os.path.isdir(SNAPSHOT):
            return False, f"快照目录不存在：{SNAPSHOT}"
        n = sum(1 for d in os.listdir(SNAPSHOT)
                if os.path.isdir(os.path.join(SNAPSHOT, d, "v2", ".git")))
        return (True, f"{n} 个会话仓库") if n else (False, "没有可读的会话仓库")

    def repos(self):
        if not os.path.isdir(SNAPSHOT):
            return []
        return [d for d in sorted(os.listdir(SNAPSHOT))
                if os.path.isdir(os.path.join(SNAPSHOT, d, "v2", ".git"))]

    def collect(self):
        sessions = turns = changed = 0
        for sid in self.repos():
            repo = os.path.join(SNAPSHOT, sid, "v2")
            log = git(repo, "log", "--all", "--format=%H%x01%at%x01%s")
            if not log.strip():
                continue
            commits = []
            for line in log.strip().split("\n"):
                h, ts, subject = line.split("\x01", 2)
                commits.append({"hash": h, "ts": int(ts), "subject": subject})
            sessions += 1
            turns += sum(1 for c in commits if c["subject"].startswith("before-chat-turn-"))
            info = {
                "session_id": sid,
                "commits": commits,
                "tags": git(repo, "tag").split(),
                "refs": [ln for ln in git(repo, "show-ref").split("\n") if ln],
            }
            payload = json.dumps(info, ensure_ascii=False).encode()
            # 内容寻址：无变化时 sha 相同，自动跳过写入
            before = self.conn.execute(
                "SELECT path FROM raw_object WHERE kind=? AND source=?",
                (self.kind, sid)).fetchone()
            sha = put_raw(self.conn, self.kind, sid, payload)
            if not before or before[0].split("/")[-1][:64] != sha:
                changed += 1
        return {"sessions": sessions, "turns": turns, "changed": changed}

    def verify(self):
        n = self.conn.execute("SELECT COUNT(*) FROM raw_object WHERE kind=?",
                              (self.kind,)).fetchone()[0]
        return n > 0, f"{len(self.repos())} 仓库 / {n} 会话归档"
