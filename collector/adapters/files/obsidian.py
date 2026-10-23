"""适配器：Obsidian 笔记库（vault）。

识别与采集：
  - roots         显式配置的 vault 根目录（优先）
  - auto_discover 自动扫描常见位置，凡含 .obsidian/ 目录即视为 vault
    位置：~/Documents、~/Obsidian、~/Notes、~/Library/Mobile Documents/iCloud~md~obsidian
  - 只采 .md；跳过 .obsidian/ .trash/ .git/ node_modules 等
  - 忽略 frontmatter 里 `minime: ignore` 或 `private: true` 的笔记（隐私开关）

与通用 local_docs 的区别：会解析 Obsidian 语义，产出更强的库内关系
  - frontmatter（tags/aliases/source）→ facet 与别名
  - [[wiki link]] → 知识之间的 link 关系
  - #tag → topic facet（含嵌套 #a/b）
这些信息落 raw 归档，供 normalize 使用。
"""
import fnmatch
import json
import os
import re
import sys

# collector/ 是 base 框架所在层；上溯三级，直接运行本文件也能调试
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
from base import ROOT, SourceAdapter, put_raw, register  # noqa: E402

DEFAULT_ROOTS = [
    "~/Documents",
    "~/Obsidian",
    "~/Notes",
    "~/Library/Mobile Documents/iCloud~md~obsidian/Documents",
]
SKIP_DIRS = {"node_modules", ".git", ".trash", ".obsidian", ".venv", "__pycache__",
             ".cache", ".DS_Store", "venv", "dist", "build"}
FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)
WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
TAG = re.compile(r"(?<![\w/])#([A-Za-z0-9_\u4e00-\u9fff][\w/\-\u4e00-\u9fff]*)")
INLINE_TAGS_SKIP = {"#", "##", "###"}      # Markdown 标题


def _cfg():
    try:
        sys.path.insert(0, os.path.join(ROOT, "pipeline"))
        import config  # noqa: PLC0415
        base = {"roots": [], "auto_discover": True, "include": ["*.md"],
                "max_file_bytes": 524288, "max_files": 50000}
        return {**base, **(config.load().get("collect", {}).get("obsidian") or {})}
    except Exception:  # noqa: BLE001
        return {"roots": [], "auto_discover": True, "include": ["*.md"],
                "max_file_bytes": 524288, "max_files": 50000}


def parse_frontmatter(text):
    """极简 YAML frontmatter 解析（只支持 key: value 与 [a, b] / - 列表）。"""
    m = FRONTMATTER.match(text)
    if not m:
        return {}, text
    meta, cur_list = {}, None
    for raw in m.group(1).split("\n"):
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.lstrip().startswith("- ") and cur_list is not None:
            meta[cur_list].append(line.lstrip()[2:].strip())
            continue
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        if not v:
            meta[k] = []
            cur_list = k
        elif v.startswith("[") and v.endswith("]"):
            meta[k] = [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
            cur_list = None
        else:
            meta[k] = v.strip("'\"")
            cur_list = None
    return meta, text[m.end():]


def as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [x.strip() for x in str(v).split(",") if x.strip()]


def is_private(meta):
    if str(meta.get("minime", "")).lower() == "ignore":
        return True
    if str(meta.get("private", "")).lower() in ("true", "yes", "1"):
        return True
    return "private" in [str(x).lower() for x in as_list(meta.get("tags"))]


def discover_vaults():
    out = []
    for root in DEFAULT_ROOTS:
        root = os.path.expanduser(root)
        if not os.path.isdir(root):
            continue
        if os.path.isdir(os.path.join(root, ".obsidian")):
            out.append(root)
            continue
        # 向下一层找 .obsidian（iCloud Documents 常见结构是 <vault>/.obsidian）
        try:
            for d in os.listdir(root):
                sub = os.path.join(root, d)
                if os.path.isdir(os.path.join(sub, ".obsidian")):
                    out.append(sub)
        except OSError:
            continue
    return out


@register
class ObsidianAdapter(SourceAdapter):
    name = "obsidian"
    kind = "obsidian_note"
    DEFAULT_INTERVAL_S = 1800
    priority = 55
    description = "Obsidian 笔记库（自动发现 vault，解析 frontmatter / [[双链]] / #标签）"
    label = "Obsidian 笔记"
    fix_hint = "在「设置 → Obsidian」填写 vault 目录，或先打开 Obsidian"

    def __init__(self, conn):
        super().__init__(conn)
        self.cfg = _cfg()

    def vaults(self):
        roots = [os.path.expanduser(r) for r in self.cfg.get("roots") or []]
        # 只认真正的 vault；显式配置的目录即使没 .obsidian 也接受（便于纯 Markdown 用户）
        explicit = [r for r in roots if os.path.isdir(r)]
        found = discover_vaults() if self.cfg.get("auto_discover", True) else []
        return sorted({*explicit, *found})

    def probe(self):
        v = self.vaults()
        if not v:
            return False, ("未发现 vault（含 .obsidian 的目录）："
                           "在「设置 → Obsidian → vault 目录」指定")
        return True, f"{len(v)} 个 vault：{', '.join(os.path.basename(x) for x in v[:3])}"

    def _notes(self, vault):
        inc = self.cfg.get("include") or ["*.md"]
        maxb = int(self.cfg.get("max_file_bytes") or 524288)
        budget = int(self.cfg.get("max_files") or 50000)
        n = 0
        for dirpath, dirnames, files in os.walk(vault):
            dirnames[:] = [d for d in dirnames
                           if d not in SKIP_DIRS and not d.startswith(".")]
            for fn in files:
                if not any(fnmatch.fnmatch(fn.lower(), p.lower()) for p in inc):
                    continue
                full = os.path.join(dirpath, fn)
                try:
                    if os.path.islink(full) or os.path.getsize(full) > maxb:
                        continue
                except OSError:
                    continue
                yield full
                n += 1
                if n >= budget:
                    return

    def collect(self):
        notes = private = 0
        links = tags = 0
        for vault in self.vaults():
            vname = os.path.basename(vault) or "vault"
            for full in self._notes(vault):
                try:
                    text = open(full, encoding="utf-8", errors="replace").read()
                except OSError:
                    continue
                meta, body = parse_frontmatter(text)
                if is_private(meta):
                    private += 1
                    continue
                rel = os.path.relpath(full, vault)
                wiki = sorted({m.strip() for m in WIKILINK.findall(body) if m.strip()})
                inline = sorted({t for t in TAG.findall(body)
                                 if t not in INLINE_TAGS_SKIP and not t.isdigit()})
                fm_tags = as_list(meta.get("tags"))
                payload = {
                    "vault": vname,
                    "path": rel,
                    "frontmatter": meta,
                    "wiki_links": wiki,
                    "tags": sorted({*fm_tags, *inline}),
                    "aliases": as_list(meta.get("aliases")),
                    "body": body[:20000],
                }
                put_raw(self.conn, self.kind, f"{vname}/{rel}",
                        json.dumps(payload, ensure_ascii=False).encode())
                notes += 1
                links += len(wiki)
                tags += len(payload["tags"])
        return {"vaults": len(self.vaults()), "notes": notes,
                "wiki_links": links, "tags": tags, "private_skipped": private}

    def verify(self):
        n = self.conn.execute("SELECT COUNT(*) FROM raw_object WHERE kind=?",
                              (self.kind,)).fetchone()[0]
        return n > 0, f"{len(self.vaults())} vault / {n} 篇笔记"
