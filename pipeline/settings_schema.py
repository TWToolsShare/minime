"""声明式配置 schema：一份字段表同时驱动「设置页表单」和「命令行校验」。

优先级：面板保存值 > 环境变量 > 内置默认（DEFAULTS）
  - 保存值落 config.json（原子写，权限 0600）
  - 空值 / null 表示删除该项，回退到下一层

新增配置项只需在 FIELDS 里加一行，前后端都不用改。
"""
import json
import os

import paths

PATH = paths.CONFIG

# group 用于设置页分组；sensitive 字段在接口回传时脱敏
FIELDS = [
    # ---- 回答方式
    {"key": "answer.mode", "env": "MINIME_ANSWER_MODE", "type": "string",
     "group": "回答方式", "label": "作答方式", "options": ["llm", "rules"],
     "option_labels": {"llm": "大模型作答（推荐）", "rules": "关键词规则作答"},
     "hint": "大模型作答：模型理解问题并给出结论，检索同时启用向量；"
             "关键词规则作答：直接摘录命中的知识，不调用模型、检索只用关键词，断网可用"},
    {"key": "answer.decide_min_score", "env": "", "type": "float", "min": 0.2, "max": 1,
     "group": "回答方式", "label": "规则模式的证据门槛",
     "hint": "0.2~1，默认 0.55；大模型作答时由模型判断证据是否充分，该值只在关键词规则作答下生效"},
    {"key": "answer.decide_cache_ttl_s", "env": "", "type": "int", "min": 0, "max": 86400,
     "group": "回答方式", "label": "结论缓存（秒）",
     "hint": "默认 600；同一问题且证据相同才复用模型结论，命中后不再调用模型、也不重复记引用；0 表示不缓存"},
    # ---- 本地大模型归纳
    {"key": "llm.enabled", "env": "MINIME_LLM_ENABLED", "type": "bool",
     "group": "本地大模型归纳", "label": "启用 LLM 归纳",
     "hint": "把「改了哪些文件」提炼成「为什么这么改、约束是什么」；关闭时只用规则抽取"},
    {"key": "llm.base_url", "env": "MINIME_LLM_BASE_URL", "type": "string",
     "group": "本地大模型归纳", "label": "接口地址",
     "hint": "OpenAI 兼容接口，例如 http://127.0.0.1:8000/v1"},
    {"key": "llm.model", "env": "MINIME_LLM_MODEL", "type": "string",
     "group": "本地大模型归纳", "label": "模型"},
    {"key": "llm.api_key", "env": "MINIME_LLM_API_KEY", "type": "string",
     "group": "本地大模型归纳", "label": "API Key", "sensitive": True,
     "hint": "留空将清除已保存的 Key，回退到环境变量或默认值；本地服务可填任意值"},
    {"key": "llm.max_atoms_per_run", "env": "", "type": "int", "min": 1, "max": 500,
     "group": "本地大模型归纳", "label": "每次最多归纳",
     "hint": "每条约 40 秒，按机器性能调整"},
    {"key": "llm.max_tokens", "env": "", "type": "int", "min": 256, "max": 8192,
     "group": "本地大模型归纳", "label": "单次最大 token"},
    {"key": "llm.disable_thinking", "env": "", "type": "bool",
     "group": "本地大模型归纳", "label": "关闭思考链",
     "hint": "推理型模型（Qwen3 等）关闭后归纳更快更稳"},
    {"key": "llm.summary_threshold", "env": "", "type": "int", "min": 200, "max": 20000,
     "group": "本地大模型归纳", "label": "长文摘要阈值（字符）",
     "hint": "超过该长度的知识会调用模型生成摘要；未启用 LLM 时不做摘要，全文入库"},
    {"key": "llm.summary_max_per_run", "env": "", "type": "int", "min": 0, "max": 2000,
     "group": "本地大模型归纳", "label": "每次最多摘要",
     "hint": "0 表示不限制；摘要结果按内容缓存，后续清洗不会重复调用"},
    {"key": "llm.summary_time_budget_s", "env": "", "type": "int", "min": 0, "max": 7200,
     "group": "本地大模型归纳", "label": "摘要总时长上限（秒）",
     "hint": "清洗期间给模型调用的总预算，用完后面的长文直接截断，避免模型卡住把整条流水线拖死；0 表示不限制"},
    # ---- 知识整理
    {"key": "consolidate.enabled", "env": "", "type": "bool",
     "group": "知识整理（定期）", "label": "启用定期压缩与取代",
     "hint": "每次清洗后执行：到期条目归档、近似重复条目保留一条、其余标记为已取代"},
    {"key": "consolidate.similarity", "env": "", "type": "float", "min": 0.6, "max": 1,
     "group": "知识整理（定期）", "label": "标题相似度阈值",
     "hint": "0.6~1，默认 0.88；越高越保守，只合并几乎相同的标题"},
    {"key": "consolidate.body_similarity", "env": "", "type": "float", "min": 0.5, "max": 1,
     "group": "知识整理（定期）", "label": "正文相似度阈值",
     "hint": "0.5~1，默认 0.85；标题相近且正文也相近才合并，避免同名不同内容被压缩"},
    {"key": "consolidate.vector_similarity", "env": "", "type": "float", "min": 0.5, "max": 1,
     "group": "知识整理（定期）", "label": "语义判重阈值",
     "hint": "0.5~1，默认 0.86；换词/换语序写出的同一结论按向量余弦判重，与标题规则并联。"
             "需要向量检索可用，否则自动跳过"},
    {"key": "consolidate.event_retention_days", "env": "", "type": "int", "min": 0, "max": 3650,
     "group": "知识整理（定期）", "label": "活动事件保留天数",
     "hint": "超过天数且无采用/引用反馈的低价值事件会归档；0 表示不按时间清理"},
    {"key": "consolidate.raw_keep_versions", "env": "", "type": "int", "min": 0, "max": 100,
     "group": "知识整理（定期）", "label": "原始归档保留版本数",
     "hint": "默认 3；每个来源（文件/会话）只留最近几个原始版本，超出的删除。"
             "知识正文已入库，清理不影响检索；0 表示永久保留"},
    # ---- 检索与反馈
    {"key": "feedback.dormant_after_rejects", "env": "", "type": "int", "min": 0, "max": 10,
     "group": "检索与反馈", "label": "连续「不相关」降权阈值",
     "hint": "默认 2；达到后进入「已降权」，不删除、不归档，仍可在命中不足时召回；0 表示不降权"},
    {"key": "feedback.dormant_penalty", "env": "", "type": "float", "min": 0.1, "max": 1,
     "group": "检索与反馈", "label": "降权后的评分系数",
     "hint": "0.1~1，默认 0.6；越低越少被召回"},
    {"key": "feedback.dormant_fallback_min", "env": "", "type": "int", "min": 0, "max": 20,
     "group": "检索与反馈", "label": "降权召回触发条数",
     "hint": "正常知识命中少于该条数时，才补充召回降权知识；0 表示不召回"},
    {"key": "feedback.usage_retention_days", "env": "", "type": "int", "min": 0, "max": 3650,
     "group": "检索与反馈", "label": "反馈日志保留（天）",
     "hint": "默认 180；检索与引用每次调用都会记一条，超期在清洗时清理；0 表示永久保留"},
    # ---- 向量检索（Embedding）
    {"key": "embedding.enabled", "env": "MINIME_EMBEDDING_ENABLED", "type": "bool",
     "group": "向量检索（Embedding）", "label": "启用向量检索",
     "hint": "默认开：检索时叠加语义相似度召回，关键词没命中也能找到相近知识；同时按相似度归档无关知识。"
             "接口不可达时检索自动降级为关键词检索（不报错）；0.1 秒级开销增加一次 embedding 请求"},
    {"key": "embedding.base_url", "env": "MINIME_EMBEDDING_BASE_URL", "type": "string",
     "group": "向量检索（Embedding）", "label": "接口地址",
     "hint": "OpenAI 兼容 /v1，例如 http://127.0.0.1:11434/v1；留空则检索只用关键词"},
    {"key": "embedding.model", "env": "MINIME_EMBEDDING_MODEL", "type": "string",
     "group": "向量检索（Embedding）", "label": "模型",
     "hint": "例如 bge-m3、text-embedding-3-small；换模型后旧向量不再参与检索，需重新清洗一次"},
    {"key": "embedding.query_timeout_s", "env": "", "type": "float", "min": 0.5, "max": 60,
     "group": "向量检索（Embedding）", "label": "查询超时（秒）",
     "hint": "默认 4；超时即本次检索降级为关键词，保证面板/MCP 不被拖慢"},
    {"key": "embedding.api_key", "env": "MINIME_EMBEDDING_API_KEY", "type": "string",
     "group": "向量检索（Embedding）", "label": "API Key", "sensitive": True,
     "hint": "留空将清除已保存的 Key，回退到环境变量或默认值；本地服务可填任意值"},
    {"key": "embedding.min_score", "env": "", "type": "float", "min": 0, "max": 1,
     "group": "向量检索（Embedding）", "label": "相似度阈值",
     "hint": "0~1，默认 0.28；越高越严格。检索按 0.8 倍作为召回下限，同一阈值也用于归档无关知识。"
             "先用 python pipeline/semantic_gate.py --dry-run 预览命中数"},
    {"key": "embedding.batch_size", "env": "", "type": "int", "min": 1, "max": 256,
     "group": "向量检索（Embedding）", "label": "每批条数"},
    {"key": "embedding.text_mode", "env": "", "type": "string",
     "group": "向量检索（Embedding）", "label": "向量化文本",
     "options": ["title_summary", "full"],
     "option_labels": {"title_summary": "标题 + 摘要", "full": "标题 + 正文（更长更准，也更慢）"},
     "hint": "检索与语义闸门共用同一份表征；改完需重新清洗一次才会重算已缓存的向量"},
    {"key": "embedding.reclassify", "env": "", "type": "bool",
     "group": "向量检索（Embedding）", "label": "语义分类校准",
     "hint": "用原型向量把泛化的「事实」改判成踩坑/决定/偏好，让按类型过滤的工具能捞到；"
             "只改模型抽取的条目，用户明确表达的不动"},
    {"key": "embedding.kind_min_score", "env": "", "type": "float", "min": 0.3, "max": 1,
     "group": "向量检索（Embedding）", "label": "分类校准阈值",
     "hint": "0.3~1，默认 0.55；越高越保守"},
    {"key": "embedding.seeds", "env": "", "type": "list",
     "group": "向量检索（Embedding）", "label": "主题种子（可选）",
     "hint": "每行一个项目名或关键词；留空则自动取知识库里的项目/技术标签"},
    {"key": "embedding.sources", "env": "", "type": "list",
     "group": "向量检索（Embedding）", "label": "只过滤这些来源",
     "hint": "每行一个来源；留空则用默认：local_doc、obsidian、cloud_conversation、"
             "snapshot_turn、workbuddy_session、workbuddy_memory、qoder_session、"
             "doubao_conversation"},
    # ---- 记忆同步
    {"key": "memory_sync.enabled", "env": "MINIME_MEMORY_SYNC", "type": "bool",
     "group": "记忆同步", "label": "启用 minime → mnemosyne 同步"},
    {"key": "memory_sync.bank", "env": "MINIME_MEMORY_BANK", "type": "string",
     "group": "记忆同步", "label": "目标记忆库",
     "hint": "mnemosyne 中需先存在该 bank（mnemosyne bank create <name>）"},
    {"key": "memory_sync.min_veracity", "env": "", "type": "float", "min": 0, "max": 1,
     "group": "记忆同步", "label": "最低可信度"},
    {"key": "memory_sync.max_per_run", "env": "", "type": "int", "min": 1, "max": 500,
     "group": "记忆同步", "label": "每次最多同步"},
    # ---- 本地数据源
    {"key": "collect.local.roots", "env": "", "type": "list",
     "group": "本地数据源", "label": "采集目录",
     "hint": "每行一个目录；用于把本机文档/笔记纳入知识库"},
    {"key": "collect.local.include", "env": "", "type": "list",
     "group": "本地数据源", "label": "包含的文件类型",
     "hint": "每行一个通配符，例如 *.md"},
    {"key": "collect.local.exclude_dirs", "env": "", "type": "list",
     "group": "本地数据源", "label": "排除的目录名"},
    {"key": "collect.local.path_allow", "env": "", "type": "list",
     "group": "本地数据源", "label": "只收这些路径（可选）",
     "hint": "每行一个通配符，匹配相对或绝对路径，例如 docs/* 或 *architecture*；留空表示不限"},
    {"key": "collect.local.keyword_allow", "env": "", "type": "list",
     "group": "本地数据源", "label": "关键词白名单（可选）",
     "hint": "每行一个词；文件名或正文前 8KB 命中任意一个才收；留空表示不限"},
    {"key": "collect.local.keyword_deny", "env": "", "type": "list",
     "group": "本地数据源", "label": "关键词黑名单（可选）",
     "hint": "每行一个词；文件名或正文前 8KB 命中任意一个就跳过"},
    {"key": "collect.local.max_file_bytes", "env": "", "type": "int",
     "min": 1024, "max": 10485760,
     "group": "本地数据源", "label": "单文件大小上限（字节）"},
    {"key": "collect.local.max_files", "env": "", "type": "int",
     "min": 100, "max": 200000,
     "group": "本地数据源", "label": "单轮最多采集文件数",
     "hint": "默认 20000；单个采集目录单轮遍历的文件上限，达到后本轮停止，"
             "采集统计会标记 truncated。目录很大时调高，或收窄「包含的文件类型」"},
    # ---- Obsidian
    {"key": "collect.obsidian.roots", "env": "", "type": "list",
     "group": "Obsidian", "label": "vault 目录",
     "hint": "留空则自动扫描 ~/Documents、~/Obsidian、~/Notes 与 iCloud；每行一个目录"},
    {"key": "collect.obsidian.auto_discover", "env": "", "type": "bool",
     "group": "Obsidian", "label": "自动发现 vault",
     "hint": "凡含 .obsidian 目录的文件夹即识别为 vault"},
    {"key": "collect.obsidian.max_file_bytes", "env": "", "type": "int",
     "min": 1024, "max": 10485760,
     "group": "Obsidian", "label": "单篇笔记大小上限（字节）"},
    # ---- WorkBuddy / 豆包
    {"key": "collect.workbuddy.roots", "env": "", "type": "list",
     "group": "AI 助手数据源", "label": "WorkBuddy 目录",
     "hint": "留空自动扫描 ~/.workbuddy-ai 与 ~/.workbuddy；每行一个目录"},
    {"key": "collect.workbuddy.include_identity", "env": "", "type": "bool",
     "group": "AI 助手数据源", "label": "采集 WorkBuddy 身份文件",
     "hint": "包含 USER.md / IDENTITY.md / SOUL.md；关闭后只采会话与长期记忆"},
    {"key": "collect.doubao.roots", "env": "", "type": "list",
     "group": "AI 助手数据源", "label": "豆包导出目录",
     "hint": "留空自动扫描豆包应用数据目录；建议把豆包导出的 JSON / Markdown / SQLite "
             "放到一个固定目录后填这里（无需系统权限）。macOS 直接读应用容器需给"
             "运行 minime 的终端/Python 授予完全磁盘访问权限——不是给豆包授权"},
    {"key": "collect.qoder.roots", "env": "", "type": "list",
     "group": "AI 助手数据源", "label": "Qoder 目录",
     "hint": "留空自动扫描 ~/Library/Application Support/com.qoder.app.stable（含 main.sqlite）；"
             "每行一个目录"},
    # ---- 定时采集
    {"key": "schedule.enabled", "env": "", "type": "bool",
     "group": "定时采集", "label": "启用后台定时采集",
     "hint": "关闭后仅在手动执行 minime update 或面板点击采集时采集"},
    {"key": "schedule.interval_minutes", "env": "", "type": "int", "min": 5, "max": 1440,
     "group": "定时采集", "label": "定时触发间隔（分钟）",
     "hint": "系统闹钟粒度；实际是否采集仍由各源自己的周期决定。改后需 minime install 重新注册"},
    # ---- 服务
    {"key": "web.port", "env": "MINIME_WEB_PORT", "type": "int", "min": 1024, "max": 65535,
     "group": "服务", "label": "Web 面板端口",
     "hint": "修改后需重启面板（minime web）"},
    {"key": "web.retention_days_error", "env": "", "type": "int", "min": 1, "max": 365,
     "group": "服务", "label": "错误日志保留（天）"},
    {"key": "web.retention_hours_info", "env": "", "type": "int", "min": 1, "max": 720,
     "group": "服务", "label": "普通日志保留（小时）"},
]

# 每个采集源的采集周期（秒）：设置页可视化，落 config.json -> collect.intervals.<name>
ADAPTER_INTERVALS = [
    ("cloud_sessions", "Trae 云端会话", 300),
    ("snapshot", "Trae 会话快照", 600),
    ("workspace_index", "工作区索引", 1800),
    ("file_history", "文件编辑历史", 1800),
    ("trae_memory", "Trae 记忆", 3600),
    ("local_docs", "本地文档", 3600),
    ("obsidian", "Obsidian 笔记", 1800),
    ("workbuddy", "WorkBuddy 会话", 1800),
    ("qoder", "Qoder 会话", 1800),
    ("doubao", "豆包会话", 3600),
]
def _dur(sec):
    """秒数说成人话：3600 -> 1 小时。"""
    if sec % 3600 == 0:
        return f"{sec // 3600} 小时"
    if sec % 60 == 0:
        return f"{sec // 60} 分钟"
    return f"{sec} 秒"


for _name, _label, _default in ADAPTER_INTERVALS:
    FIELDS.append({
        "key": f"collect.enabled.{_name}", "env": "", "type": "bool",
        "group": "定时采集 · 各源开关", "label": _label, "default": True,
        "hint": "取消勾选即停止采集/重试（已入库知识与归档保留，可随时再打开）",
    })
    FIELDS.append({
        "key": f"collect.intervals.{_name}", "env": "", "type": "int",
        "min": 60, "max": 86400, "group": "定时采集 · 各源周期",
        "label": _label, "default": _default,
        "hint": f"单位：秒。默认 {_dur(_default)}；失败后自动退避（×2ⁿ，最长 1 小时）",
    })

BY_KEY = {f["key"]: f for f in FIELDS}


def get_path(d, key):
    cur = d
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def set_path(d, key, value):
    parts = key.split(".")
    cur = d
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    if value is None:
        cur.pop(parts[-1], None)
    else:
        cur[parts[-1]] = value


def _read():
    paths.ensure()
    if not os.path.exists(PATH):
        return {}
    try:
        d = json.load(open(PATH, encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data):
    paths.ensure()
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    tmp = PATH + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, PATH)


def normalize(field, raw):
    """按 schema 校验并转换单个值；返回 (ok, value_or_error)。"""
    t = field["type"]
    label = field["label"]
    if t == "bool":
        if isinstance(raw, bool):
            return True, raw
        s = str(raw).strip().lower()
        if s in ("1", "true", "on", "yes"):
            return True, True
        if s in ("0", "false", "off", "no"):
            return True, False
        return False, f"「{label}」需要是开 / 关"
    if t == "int":
        try:
            v = int(str(raw).strip())
        except ValueError:
            return False, f"「{label}」需要是整数"
        lo, hi = field.get("min"), field.get("max")
        if lo is not None and v < lo:
            return False, f"「{label}」不能小于 {lo}"
        if hi is not None and v > hi:
            return False, f"「{label}」不能大于 {hi}"
        return True, v
    if t == "float":
        try:
            v = float(str(raw).strip())
        except ValueError:
            return False, f"「{label}」需要是数字"
        lo, hi = field.get("min"), field.get("max")
        if lo is not None and v < lo:
            return False, f"「{label}」不能小于 {lo}"
        if hi is not None and v > hi:
            return False, f"「{label}」不能大于 {hi}"
        return True, v
    if t == "list":
        if isinstance(raw, list):
            items = raw
        else:
            items = [x.strip() for x in str(raw).split("\n")]
        return True, [x for x in items if str(x).strip()]
    v = str(raw).strip()
    if not v:
        return False, f"「{label}」不能为空"
    if field.get("options") and v not in field["options"]:
        return False, f"「{label}」只能是：{' / '.join(field['options'])}"
    return True, v


def load():
    """返回合并后的完整配置（保存值 > 环境变量 > 内置默认）。"""
    import config as C  # noqa: PLC0415
    return C.load()


def current_view():
    """当前生效值（敏感字段脱敏），供界面展示。"""
    cfg = load()
    out = {}
    for f in FIELDS:
        v = get_path(cfg, f["key"])
        # bool 字段未保存时按 default 展示，避免「默认开启」被画成未勾选
        if v is None and f["type"] == "bool" and "default" in f:
            v = f["default"]
        if f.get("sensitive") and v:
            v = "***"
        out[f["key"]] = v
    return out


def saved_view():
    """面板里保存过的原始值（不含默认与环境变量），用于标记「未重启」。"""
    saved = _read()
    out = {}
    for f in FIELDS:
        v = get_path(saved, f["key"])
        if f.get("sensitive") and v:
            v = "***"
        out[f["key"]] = v
    return out


def schema():
    """给前端的字段表（不含内部细节）。"""
    return [{"key": f["key"], "type": f["type"], "group": f["group"],
             "label": f["label"], "hint": f.get("hint", ""),
             "env": f.get("env", ""), "sensitive": bool(f.get("sensitive")),
             "options": f.get("options"), "option_labels": f.get("option_labels"),
             "min": f.get("min"), "max": f.get("max")} for f in FIELDS]


def save(values: dict):
    """保存面板配置。values 的 key 用点号路径；空字符串/null 表示删除。"""
    data = _read()
    applied, errors = {}, {}
    for key, raw in values.items():
        f = BY_KEY.get(key)
        if not f:
            errors[key] = f"未知配置项: {key}"
            continue
        if f.get("sensitive") and raw == "***":
            continue                      # 界面回传的脱敏占位，忽略
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            set_path(data, key, None)
            applied[key] = None
            continue
        ok, res = normalize(f, raw)
        if not ok:
            errors[key] = res
            continue
        set_path(data, key, res)
        applied[key] = res
    _write(data)
    return {"ok": not errors, "applied": applied, "errors": errors, "path": PATH}
