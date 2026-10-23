"""适配器：本地数据源（用户自选目录中的文档/笔记/代码）。

与 Trae 专用适配器的区别：
  - 路径由配置决定（config.json -> collect.local_roots），不写死
  - 支持 include/exclude 通配、单文件大小上限、按内容哈希去重
  - 采集粒度：文件级；落 raw 时 kind=local_doc，source=相对路径

这样「本地任意资料」都能作为独立数据源接入知识库，
而不只是 Trae 的对话与快照。
"""
import fnmatch
import os
import sys

# collector/ 是 base 框架所在层；上溯三级，直接运行本文件也能调试
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
from base import ROOT, SourceAdapter, put_raw, register  # noqa: E402

DEFAULTS = {
    "roots": [],
    "include": ["*.md", "*.txt", "*.markdown", "*.org", "*.rst",
                "*.py", "*.ts", "*.js", "*.java", "*.go", "*.rs",
                "*.json", "*.yml", "*.yaml", "*.toml", "*.sql"],
    "exclude_dirs": ["node_modules", ".git", ".venv", "venv", "dist", "build",
                     "__pycache__", ".next", "target", "vendor", ".cache",
                     "Library", "Pods", ".idea", ".vscode"],
    "max_file_bytes": 262144,
    "max_files": 20000,
}


def path_allowed(cfg, full, rel):
    """可选：只收匹配这些通配符的文件（相对路径或绝对路径）。留空=不限。"""
    allow = [os.path.expanduser(str(p).strip())
             for p in (cfg.get("path_allow") or []) if str(p).strip()]
    if not allow:
        return True
    relp = rel.replace(os.sep, "/")
    fullp = full.replace(os.sep, "/")
    return any(fnmatch.fnmatch(relp, p) or fnmatch.fnmatch(fullp, p) for p in allow)


def keyword_allowed(cfg, rel, data):
    """可选：文件名或正文前 8KB 的关键词白/黑名单。两者都留空=不限。"""
    deny = [str(k).strip().lower() for k in (cfg.get("keyword_deny") or []) if str(k).strip()]
    allow = [str(k).strip().lower() for k in (cfg.get("keyword_allow") or []) if str(k).strip()]
    if not deny and not allow:
        return True
    name = rel.lower()
    head = data[:8192].decode("utf-8", "replace").lower()
    if deny and any(k in name or k in head for k in deny):
        return False
    if allow and not any(k in name or k in head for k in allow):
        return False
    return True


def _cfg():
    try:
        sys.path.insert(0, os.path.join(ROOT, "pipeline"))
        import config  # noqa: PLC0415
        return {**DEFAULTS, **(config.load().get("collect", {}).get("local") or {})}
    except Exception:  # noqa: BLE001 — 配置异常时退回默认，不影响其他源
        return dict(DEFAULTS)


@register
class LocalDocsAdapter(SourceAdapter):
    name = "local_docs"
    kind = "local_doc"
    DEFAULT_INTERVAL_S = 3600
    priority = 60
    description = "本地文档/笔记（路径在配置 collect.local.roots 中指定）"
    label = "本地文档"
    fix_hint = "在「设置 → 本地数据源」填写存在的目录"

    def __init__(self, conn):
        super().__init__(conn)
        self.cfg = _cfg()

    def probe(self):
        roots = [os.path.expanduser(r) for r in self.cfg.get("roots") or []]
        exists = [r for r in roots if os.path.isdir(r)]
        if not roots:
            return False, "未配置采集目录：在「设置 → 本地数据源 → 采集目录」填写"
        if not exists:
            return False, f"以下目录不存在：{', '.join(roots[:3])}；请在「设置 → 本地数据源」更新"
        return True, f"{len(exists)} 个目录"

    def _walk(self, root):
        inc = self.cfg.get("include") or []
        exc = set(self.cfg.get("exclude_dirs") or [])
        maxb = int(self.cfg.get("max_file_bytes") or 262144)
        budget = int(self.cfg.get("max_files") or 20000)
        count = 0
        for dirpath, dirnames, files in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in exc and not d.startswith(".")]
            for fn in files:
                if not any(fnmatch.fnmatch(fn.lower(), p.lower()) for p in inc):
                    continue
                full = os.path.join(dirpath, fn)
                try:
                    if os.path.getsize(full) > maxb or os.path.islink(full):
                        continue
                except OSError:
                    continue
                if not path_allowed(self.cfg, full, os.path.relpath(full, root)):
                    continue
                yield full
                count += 1
                if count >= budget:
                    self._truncated = True   # 达到单轮文件上限，剩下的不再采（会在 stats 里报出来）
                    return

    def collect(self):
        files = same = 0
        self._truncated = False
        for raw_root in self.cfg.get("roots") or []:
            root = os.path.expanduser(raw_root)
            if not os.path.isdir(root):
                continue
            for full in self._walk(root):
                try:
                    with open(full, "rb") as f:
                        data = f.read()
                except OSError:
                    continue
                rel = os.path.relpath(full, root)
                if not keyword_allowed(self.cfg, rel, data):
                    continue
                put_raw(self.conn, self.kind, rel, data)
                files += 1
        n = self.conn.execute("SELECT COUNT(*) FROM raw_object WHERE kind=?",
                              (self.kind,)).fetchone()[0]
        return {"files_scanned": files, "archived": n,
                "truncated": bool(getattr(self, "_truncated", False))}
