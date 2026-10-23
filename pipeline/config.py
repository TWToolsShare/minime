"""配置读取（config.json + MINIME_* 环境变量覆盖）。

进程内按 config.json 的 (mtime, size) 缓存解析结果：检索一次会读好几次配置，
每次都解析 JSON + 深拷贝 DEFAULTS 是白扔的延迟。环境变量每次实时读取，
因此 `MINIME_*` 改了立刻生效，改文件也立刻生效。
"""
import copy
import json
import os

import paths

PATH = paths.CONFIG

DEFAULTS = {
    "llm": {
        "enabled": False, "base_url": "http://127.0.0.1:8000/v1",
        "model": "Qwen3.8-27B", "api_key": "none", "timeout_s": 180,
        "max_tokens": 2048, "disable_thinking": True,
        "max_atoms_per_run": 12, "batch_size": 4, "temperature": 0.2,
        "prompt_lang": "zh", "summary_threshold": 1200,
        "summary_max_per_run": 100,
    },
    "answer": {
        # llm=大模型理解并作答（默认，检索也走向量） / rules=关键词规则问答（纯本地、检索只用关键词）
        "mode": "llm",
        # rules 模式下判定「证据是否充分」的分数门槛；混合打分后 0.55 相当于只靠关键词时的 0.4 左右
        "decide_min_score": 0.55,
        # 同一问题 + 同一批证据的模型结论缓存秒数（0=不缓存）；只影响 llm 模式
        "decide_cache_ttl_s": 600,
    },
    "memory_sync": {
        "enabled": False, "direction": "minime_to_bank", "bank": "case-analysis",
        "min_trust_tier": 1,
        "kinds": ["decision", "preference", "troubleshooting", "pattern", "insight"],
        "min_veracity": 0.75, "max_per_run": 20,
    },
    "embedding": {
        # 向量检索：开启后关键词没命中也能按语义召回；接口不可达时自动降级为纯 FTS
        "enabled": True, "base_url": "", "model": "", "api_key": "",
        "min_score": 0.28, "batch_size": 32, "seeds": [],
        "query_timeout_s": 4,
        # 向量化用哪段文本：title_summary（标题+摘要）/ full（标题+正文）
        "text_mode": "title_summary",
        # 语义分类校准：用原型向量把泛化的 fact 归到 troubleshooting/decision/preference
        "reclassify": True, "kind_min_score": 0.55,
        "sources": ["local_doc", "obsidian", "cloud_conversation", "snapshot_turn",
                    "workbuddy_session", "workbuddy_memory", "qoder_session",
                    "doubao_conversation"],
    },
    "consolidate": {
        "enabled": True, "similarity": 0.88, "body_similarity": 0.85,
        # 向量判重：余弦达到该值即视为同一知识（标题+正文表征相同）
        "vector_similarity": 0.86,
        # 原始归档每个来源保留的版本数（0=不清理）；正文已在 kb.db，旧版本只影响回溯深度
        "raw_keep_versions": 3,
        "event_retention_days": 365,
    },
    "feedback": {
        # 连续多少次「不相关」进入降权（0=不降权）；降权不是删除，可被召回与恢复
        "dormant_after_rejects": 2,
        "dormant_penalty": 0.6,
        "dormant_fallback_min": 3,
        # 反馈日志（usage_event）保留天数；检索/引用每次都会写，0 表示永久保留
        "usage_retention_days": 180,
    },
    "collect": {
        "cloud_sessions": True, "snapshot_turns": True,
        "workspace_index": True, "trae_memory": True, "file_history": True,
        "local": {
            "roots": [],
            "include": ["*.md", "*.txt", "*.markdown", "*.org", "*.rst",
                        "*.py", "*.ts", "*.js", "*.java", "*.go", "*.rs",
                        "*.json", "*.yml", "*.yaml", "*.toml", "*.sql"],
            "exclude_dirs": ["node_modules", ".git", ".venv", "venv", "dist", "build",
                             "__pycache__", ".next", "target", "vendor", ".cache",
                             "Library", "Pods", ".idea", ".vscode"],
            "path_allow": [],
            "keyword_allow": [],
            "keyword_deny": [],
            "max_file_bytes": 262144,
            "max_files": 20000,
        },
        "obsidian": {
            "roots": [],
            "auto_discover": True,
            "include": ["*.md"],
            "max_file_bytes": 524288,
            "max_files": 50000,
        },
        "workbuddy": {
            # 留空自动扫描 ~/.workbuddy-ai 与 ~/.workbuddy
            "roots": [],
            "include_identity": True,
        },
        "doubao": {
            # 留空自动扫描豆包应用数据目录；导出目录可直接填这里
            "roots": [],
        },
        "qoder": {
            # 留空自动扫描 com.qoder.app.stable（含 main.sqlite）
            "roots": [],
        },
        "intervals": {},
    },
    "schedule": {"enabled": True, "interval_minutes": 30},
    "web": {"port": 8765},
}

_ENV = {
    "MINIME_LLM_ENABLED": ("llm", "enabled", bool),
    "MINIME_LLM_BASE_URL": ("llm", "base_url", str),
    "MINIME_LLM_MODEL": ("llm", "model", str),
    "MINIME_LLM_API_KEY": ("llm", "api_key", str),
    "MINIME_MEMORY_SYNC": ("memory_sync", "enabled", bool),
    "MINIME_ANSWER_MODE": ("answer", "mode", str),
    "MINIME_MEMORY_BANK": ("memory_sync", "bank", str),
    "MINIME_EMBEDDING_ENABLED": ("embedding", "enabled", bool),
    "MINIME_EMBEDDING_BASE_URL": ("embedding", "base_url", str),
    "MINIME_EMBEDDING_MODEL": ("embedding", "model", str),
    "MINIME_EMBEDDING_API_KEY": ("embedding", "api_key", str),
    "MINIME_WEB_PORT": ("web", "port", int),
}


def _coerce(v, typ):
    if typ is bool:
        return str(v).strip().lower() in ("1", "true", "yes", "on")
    return typ(v)


_FILE_CACHE = {"stamp": object(), "data": None}


def _stamp():
    try:
        st = os.stat(PATH)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _merged():
    """DEFAULTS + config.json 的合并结果，按文件戳缓存。返回的对象不要直接改。"""
    stamp = _stamp()
    if _FILE_CACHE["data"] is not None and _FILE_CACHE["stamp"] == stamp:
        return _FILE_CACHE["data"]
    data = copy.deepcopy(DEFAULTS)
    if stamp is not None:
        try:
            user = json.load(open(PATH, encoding="utf-8"))
        except (OSError, ValueError):
            user = {}
        if isinstance(user, dict):
            for section, values in user.items():
                if isinstance(values, dict) and section in data:
                    for k, v in values.items():
                        if k.startswith("_"):
                            continue
                        # 嵌套 dict（如 collect.local）按层合并，不整体覆盖
                        if isinstance(v, dict) and isinstance(data[section].get(k), dict):
                            data[section][k].update(v)
                        else:
                            data[section][k] = v
    _FILE_CACHE["stamp"] = stamp
    _FILE_CACHE["data"] = data
    return data


def load():
    paths.ensure()
    cfg = copy.deepcopy(_merged())
    for env, (sec, key, typ) in _ENV.items():
        if env in os.environ:
            cfg[sec][key] = _coerce(os.environ[env], typ)
    return cfg


def answer_mode(cfg=None):
    """返回 "llm" 或 "rules"：大模型作答 / 关键词规则作答。非法值按默认 llm 处理。"""
    mode = ((cfg or load()).get("answer") or {}).get("mode")
    return "rules" if str(mode).strip().lower() == "rules" else "llm"


def save(patch):
    """按 section 合并写入。嵌套 dict 按层合并，原子写且权限 0600。"""
    paths.ensure()
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    cur = {}
    if os.path.exists(PATH):
        try:
            cur = json.load(open(PATH, encoding="utf-8"))
        except ValueError:
            cur = {}
    for sec, values in patch.items():
        if isinstance(values, dict):
            for k, v in values.items():
                if isinstance(v, dict) and isinstance(cur.get(sec, {}).get(k), dict):
                    cur[sec][k].update(v)
                else:
                    cur.setdefault(sec, {})[k] = v
        else:
            cur[sec] = values
    tmp = PATH + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PATH)
    return cur
