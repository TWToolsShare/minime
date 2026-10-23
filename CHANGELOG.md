# 更新记录

## v0.1.0-pre.8（2026-10-23）

### 新增

- **Web 面板中英双语切换**：新增翻译层与语言开关，覆盖导航、统计栏、知识浏览、进度、画像、工作区、采集源、日志、归档、图谱、设置、数据包、对话等视图，以及侧栏筛选与空态。语言选择写入 `localStorage`，切换时重渲染统计栏等只渲染一次的组件；知识正文与品牌名不做机器翻译。

### 修复

- **定时任务不再跳过轨迹抽取与 LLM 归纳**：`minime install` 生成的 launchd / schtasks 命令此前固定带 `--fast --no-llm`。`--fast` 跳过 `extract_turns`，新会话进不了 `turn` 表；`--no-llm` 跳过 `llm_enrich`，导致采集一直在跑但知识库长期没有新知识产出。现按 `llm.enabled` 决定是否加 `--no-llm`，并移除 `--fast`（轨迹是 LLM 归纳的唯一输入）；Windows 计划任务同步修正。
- 版本号 `0.1.0rc8` 对应 `v0.1.0-pre.8`。

## v0.1.0-pre.7（2026-10-23）

### 开源准备

- README 改为英文默认（`README.md`），中文移至 `README.zh-CN.md`，顶部双语互链；图片与文档链接改用绝对地址，兼容 GitHub 与 PyPI 渲染。
- 新增 `SECURITY.md` 与 `CONTRIBUTING.md`：私密漏洞报告、开发环境、测试与提交规范。
- CI 增加 `ubuntu-latest`，与 README 声明的 Linux 支持一致。
- `pyproject.toml` 补充作者、`project.urls` 与 MIT / Beta / Python 版本 classifier。
- README 增加面板截图。
- 版本号 `0.1.0rc7` 对应 `v0.1.0-pre.7`。
- 已发布到 Repsy `https://repo.repsy.io/tengwang/pypi/`：`minime 0.1.0rc7` 的 wheel 与 sdist。

## v0.1.0-pre.6（2026-10-22）

### 新增

- **WorkBuddy / WorkBuddy AI 采集**（`collector/adapters/agents/workbuddy.py`）：自动扫描 `~/.workbuddy-ai` 与 `~/.workbuddy`，采集会话 JSONL（剥离 `<system-reminder>`，只留用户 `<user_query>`）、`memory/*_memory.md` 长期记忆、USER / IDENTITY / SOUL 身份文件，并用 `workbuddy.db` 的 `sessions` 补齐标题、工作区、模型；已删除会话跳过。
- **Qoder 采集**（`collector/adapters/agents/qoder.py`）：只读读取 `com.qoder.app.stable/main.sqlite` 的 `chat_sessions` 与 `chat_session_messages`，从 `payload_json` 提取纯文本、跳过工具调用，并带上每轮改动文件；Qoder 运行中可读（WAL 一并读取），已删除会话跳过。
- **豆包（Doubao）采集**（`collector/adapters/agents/doubao.py`）：支持豆包导出的 messages JSON / JSONL、本地 SQLite 消息表、导出目录里的 Markdown / TXT；自动识别 macOS 应用容器、Windows `%APPDATA%\Doubao` 与 Linux 常见目录，无权限时给出「完全磁盘访问权限」或「先导出」的明确动作。
- **知识清洗**（`pipeline/normalize.py`）新增 `workbuddy_session` / `workbuddy_memory` / `qoder_session` / `doubao_conversation` 四类来源：会话提问与摘要、记忆条目、导出文档分段落库，并带 project / topic / tech / scene 标签。
- 新增 `agents/` 适配器通道；设置页增加「AI 助手数据源」（WorkBuddy 目录、身份文件开关、Qoder 目录、豆包导出目录）与各源采集周期；`embedding.sources`、语义闸门与 Web 来源中文名同步。README 与使用手册补充三类新源的采集方式，以及 macOS 完全磁盘访问权限的授权对象、launchd 限制和验证步骤（授权给运行 minime 的终端/Python，不是豆包）。
- 打包同步：`pyproject.toml` 加入 `minime.collector.adapters.agents`，CI wheel 校验覆盖 WorkBuddy / Qoder / 豆包三个新适配器。
- 版本号 `0.1.0rc6` 对应 `v0.1.0-pre.6`。
- **已发布到 Repsy** `https://repo.repsy.io/tengwang/pypi/`：`minime 0.1.0rc6` 的 wheel 与 sdist。公开库安装无需 token；若返回 401，说明仓库仍为 Private，需改 Visibility 或使用 token。

## v0.1.0-pre.5（2026-10-22）

### 调整（代码 / 数据目录分离，跨平台）

- **数据目录与代码目录分离**（新增 `pipeline/paths.py`）：知识库、配置、原始归档、日志、资源包统一放到用户数据目录，代码目录只读、可整体替换升级。
  - 默认位置：macOS `~/Library/Application Support/minime`、Windows `%LOCALAPPDATA%\minime`、Linux `~/.local/share/minime`；可用 `MINIME_HOME` 指定到任意路径。
  - 老版本把数据放在代码目录（`store/`、`config.json`），首次运行自动搬迁，搬完打印提示；迁移失败不阻塞启动。
  - 全部模块（pipeline / collector / web / mcp / run）改为引用 `paths` 常量，不再各自拼 `ROOT/store`。
- **Windows 可运行**：文件锁改为 POSIX `flock` / Windows `msvcrt.locking` 双实现；`minime web|mcp|watch` 不再用 `os.execv`（Windows 没有），改为子进程启动；Trae 数据目录按 `%APPDATA%\Trae` / `~/.config/Trae` 自动识别（`MINIME_TRAE_DIR` 可覆盖）。
- **定时采集跨平台**：macOS 仍用 launchd，Windows 改用计划任务（`schtasks`，任务名 `ai.minime.collect`），`minime install / uninstall / status / doctor` 自动按平台走；launchd 的 stdout/stderr 从 `/tmp` 改到数据目录 `store/logs/`。
- `minime link` 在 Windows 上写 `minime.cmd`（`%LOCALAPPDATA%\minime\bin`），不再依赖 shell shim。
- 面板「就地重启」在 Windows 上改为延迟启动新进程并退出旧进程，避免 `execv` 不可用。

### 分发（pipx）

- 新增 `pyproject.toml`：`pipx install "/path/to/minime[full]"` 一条命令装好，全局可用 `minime`；`[full]` 带上 `mcp` / `cryptography` / `numpy`，只要核心功能可去掉这个 extra。
  - 代码按原目录布局装进 `site-packages/minime/`（`pipeline/ collector/ run/ web/ mcp_server/ docs/`），不打散模块、不改任何现有 import。
  - 新增 `minime_launcher.py` 作为入口，定位安装目录后执行 `run/minime.py`；源码目录直接运行同样可用。
  - 面板 logo / favicon 随包分发（`docs/brand`），`web/index.html` 一并打包。
  - 版本号 `0.1.0rc5` 对应 `v0.1.0-pre.5`。
- 新增 `minime migrate --from <旧目录>`：源码版 → pipx 版升级时，把旧目录里的 `store/`、`config.json` 搬到数据目录；幂等合并，目标已有知识库时默认拒绝、`--merge` 只补搬缺失的归档/日志。
- **已发布到私有 PyPI（Repsy）** `https://repo.repsy.io/tengwang/pypi/`：`minime 0.1.0rc5` 的 wheel 与 sdist。
  - 安装：`pipx install --index-url "https://<用户名>:<token>@repo.repsy.io/tengwang/pypi/simple/" --pip-args="--prerelease=allow" minime`。
  - `mcp` / `cryptography` / `numpy` 只在上游 PyPI，`"minime[full]"` 会触发 uv 的依赖混淆保护，改为 `pipx inject minime mcp cryptography numpy`（或锁定 `minime==0.1.0rc5` 后从上游装 extras）。
- 新增 GitHub Actions（`.github/workflows/ci.yml` + `tests/smoke.py`）：macOS / Windows 双平台跑安装、空库 CLI、Web 健康检查、迁移、文件锁、FTS 与 wheel 内容校验。
- 全新安装（还没 `setup`）不再报错：`minime status` 提示「知识库为空或未初始化」而不是 traceback；`doctor` 把「没初始化」与「库损坏」分开提示；面板 `/api/health` 在空库下返回 `atoms: 0`。

### 修复

- **`web.port` 配置不生效**：`config.DEFAULTS` 缺少 `web` 段，`config.json` 与 `MINIME_WEB_PORT` 里的端口被合并逻辑丢弃，面板固定跑 8765。补上默认段与环境变量映射。
- `collector/base.py` 顶部漏 `import sys`，导致采集日志写入 `oplog` 的异常被静默吞掉（日志一直没有采集记录）。
- 全新安装时 `minime doctor` 的 `index` 检查仍提示 `./run/minime.sh collect`，改为 `minime collect`。

## v0.1.0-pre.4（2026-10-22）

### 新增

- **回答方式开关**（`answer.mode`，设置页「回答方式」，默认 `llm`）：一个开关切换整条问答链路。
  - `llm` 大模型作答：模型理解问题并归纳证据作答，检索同时启用向量。
  - `rules` 关键词规则作答：直接把命中的知识摘录成答案，不调用模型、检索只用关键词，模型服务不可达时也能用。新增 `pipeline/answer.py` 负责规则作答（保留 `[n]` 引用编号与引用计数）。
  - Web 对话、`minime chat`、MCP `minime_decide` 三处共用，`confidence_breakdown.answer_source` 标注 `llm / rules / rules_fallback`。
  - 环境变量 `MINIME_ANSWER_MODE`；设置页新增下拉控件（schema `options`），命令行 `minime set answer.mode llm|rules`。
- **向量检索**（`pipeline/retrieval.py`）：`embedding.enabled` 改为默认开启。查询向量与清洗阶段已缓存的 `atom_embedding` 算余弦，与关键词相关度同尺度相加（语义分 `(1+cos)/2` 映射到 0~1），仅语义一路命中的条目折价计入，避免模糊相似挤掉精确命中。余弦低于 `embedding.min_score × 0.8` 的命中丢弃；embedding 接口不可达或超过 `embedding.query_timeout_s`（默认 4 秒）时静默降级为纯关键词检索。Web、MCP、`minime chat` 共用同一套，评分拆解新增「语义相似度」。
- 设置页新增「查询超时」（`embedding.query_timeout_s`）；`minime doctor` 新增 embedding 健康检查（模型、维度、缓存条数、矩阵规模、text_mode）。
- 新增 `requirements.txt`：`mcp` / `cryptography` / `numpy` 三个可选依赖，`pip install -r requirements.txt` 一次装齐（核心功能仍零第三方依赖）。

### 调整（全链路优化）

- **`minime_decide` 从「阈值 + 摘录」变成真判断**（`mcp_server/server.py`）：`llm` 模式下把证据交给模型输出严格 JSON（`sufficient / answer / used / verdict`），由模型判断证据是否足以回答，并返回实际引用的条目；模型不可达或返回不合格式时退回规则路径。规则路径的门槛抽成 `answer.decide_min_score`（默认 0.55），不再硬编码。
- **反馈闭环自动闭合**：`minime_decide` 把回答实际引用的知识自动记 `cited`；`minime_recall` 新增 `cited_ids` 参数，调用方回传用到的 `atom_id` 即计入反馈；新增 `retrieval.record_feedback_many` 批量版（会过滤不存在的 id）。不再依赖 AI 主动调用 `minime_feedback`。
- **`minime_decide` 结论缓存**（`answer.decide_cache_ttl_s`，默认 600 秒）：同一问题 + 同一批证据命中缓存时不再调用模型（4.8s → 0.01s），也不重复写 `cited`，避免短时间内反复提问把反馈权重刷高；设置 0 关闭。
- **向量检索走矩阵**（`pipeline/embed.py`）：抽出统一的 embedding 客户端 + 向量缓存模块，全量向量落 `store/vec_cache/<model>.npy`，检索时矩阵乘（进程内 + 磁盘两级缓存，按条数/更新时间/模型/text_mode 失效）。1942 条约 29ms → 0.2ms 热查询；没有 numpy 时自动退回逐条余弦。
- **判重与分类用上向量**：`consolidate.py` 增加语义判重路径（`consolidate.vector_similarity`，与标题规则并联，正文仍复核避免合并同一文档的不同小节）；`semantic_gate.py` 增加分类校准（`embedding.reclassify`），用原型向量把泛化的 `fact` 改判为 `troubleshooting / decision / preference`，原型向量缓存在 `embedding_cache`，只算一次。
- **facet 过滤改 `IN` 子查询**：SQLite 会为其建 bloom 过滤并从 facet 侧倒查，实测同一筛选 33ms → 16ms，且随库增长不再线性恶化。
- **配置读取加进程内缓存**（`pipeline/config.py`）：按 `config.json` 的 `(mtime, size)` 缓存合并结果，环境变量仍实时生效；单次 `load()` 从约 0.25ms 降到 0.03ms，检索一轮原本要读 3~4 次。
- **schema 加迁移机制**（`pipeline/schema.py`）：新增 `_migrations` 表与 `MIGRATIONS` 列表，老库增量补齐表/索引；`retrieval._ensure_gate`、`semantic_gate._ensure`、`consolidate._ensure` 统一收口到 `schema.migrate()`。新增 `embedding_cache`、`supersede_log` 与若干索引。
- 设置页新增：`answer.decide_min_score`、`embedding.text_mode`（标题+摘要 / 标题+正文）、`embedding.reclassify`、`embedding.kind_min_score`、`consolidate.vector_similarity`。

### 修复

- embedding 开关打开但未填地址/模型时，检索流程不再尝试发起请求。
- `minime_decide` 的 `confidence` 超过 1 时截断到 1.0（混合打分下置信度可能溢出）。
- `minime_decide` 在 LLM 模式下强化证据对齐：证据必须与问题指向同一对象/场景，且回答至少引用一条证据，否则判 `insufficient_evidence`，避免拿相同关键词的无关材料硬答。
- `pipeline/schema.py` 补建 `atom_embedding` 表，此前只在语义闸门运行时按需创建。
- `normalize.py` 删除从未被使用的 `RULE_MARKERS`，分类细化统一交给语义校准。
- **本地文档 / Obsidian 适配器读不到配置**：`_cfg()` 拼 pipeline 路径时少上溯一级（拼成 `collector/pipeline`），`import config` 必然失败并静默回落默认值——本地文档长期显示「未配置采集目录」，Obsidian 忽略用户指定 vault 只走自动发现。改用 `base.ROOT` 拼接。
- **同标题知识永远不刷新正文**：`fingerprint` 只由 kind + 标题决定，重复清洗时只提升 `veracity`、跳过正文；文档改版后知识库一直停在首次清洗的快照。现在同一来源（`source_kind + source_ref`）内容变化会替换正文、摘要与来源摘要，并按 `raw_object` 写入顺序处理，最新版本胜出。
- **高质量来源没有向量**：向量化范围沿用 `embedding.sources` 的 4 类，`trae_memory` 与 `llm_enrich` 共 321 条只靠关键词命中。现在所有知识都向量化，`semantic_gate` 的归档判定仍只作用于配置的来源（统计新增 `scoped`）。
- **本地文档把代码注释当标题**：切段正则 `#{1,4}\s*` 允许零空格，`#!/usr/bin/env python3` 被当成章节标题，产生 39 条「`!/usr/bin/env python3`」知识。现在只有 `.md` / `.markdown` 按标题切段，其余文件整文件一条并限制 12000 字符。
- 本地文档达到 `max_files` 上限时静默丢弃剩余文件，现在采集统计会返回 `truncated: true`。
- **本地源码与文档同权**：代码文件也按 `fact` / `trust_tier 2` / `importance 0.5` 入库，检索时常压过使用手册。现在按扩展名分级：文档（md/markdown/txt/rst/org）保持 tier 2，其余源码与配置降为 `trust_tier 3`、`importance 0.28`、`confidence 0.65`，仍可检索但排序让位于文档。采集上限 `collect.local.max_files` 默认 20000 并开放到设置页「本地数据源 → 单轮最多采集文件数」（此前写死 2000 且已贴上限，超出部分静默丢弃）。
- 重复清洗时只更新 `veracity`，`importance / trust_tier / confidence` 不会跟着新规则变；现在这三项随每次清洗刷新，调权重后重跑即可生效。
- **清洗与闸门的无用功**：语义闸门逐条算 Python 余弦（2416 条 × 69 个种子，一次 9.6s），改为一次矩阵乘（4.5s）；重建模式此前每写一条知识就建一次 FTS，末尾又整体重建一遍，现在只在末尾建；删掉 `Writer` 里从未使用的全表备份。稳态一次清洗 61s → 35s。
- **存储瘦身**：`atom_fts` 改 contentless（列值不再存副本，检索按 `atom.rowid` 关联，迁移 0006 自动重灌 2433 条）；向量由 base64 文本改 float32 BLOB（迁移 0007，省 25%、解码更快）；`usage_event` 增加 180 天保留期（`feedback.usage_retention_days`，设置页可改，0 表示永久）。配合一次 `VACUUM`：`kb.db` 131.3MB → 35.6MB，闸门 4.5s → 2.0s，稳态清洗 35s → 5s。
- **原始归档保留策略**：内容寻址的 `store/raw` 此前每个版本都永久保留，新增 `consolidate.raw_keep_versions`（默认 3，设置页可改，0 表示永久），每次清洗只保留每个来源最近几版；知识正文已在 `kb.db`，清理不影响检索。本次清理 166 个旧版本，`store/raw` 67MB → 15MB。
- `minime status/doctor` 会提示缺失的可选依赖（`mcp`、`cryptography`）与对应的安装命令（含当前解释器绝对路径）。

## v0.1.0-pre.3（2026-10-21）

品牌标识：新增 minime logo，接入面板顶栏、favicon 与 README。

### 新增

- **Logo**（`docs/brand/`）：收窄的取景框 + 琥珀方块，表示「从工作轨迹里收敛出结论」。提供深/浅两套横版 SVG 与 PNG、独立 mark、方形图标（`icon-1024.png` 可直接作 GitHub 头像）与 `favicon.svg`。
- 面板顶栏 `minime` 前加入内联 SVG 小标，跟随主题变色；页面补 `favicon` / `apple-touch-icon`。
- 新增白名单静态资源路由：只直出 `docs/brand` 下的 svg/png/ico，文件名取 basename 防路径穿越。

## v0.1.0-pre.2（2026-10-21）

面板易用性修复：设置页与顶部导航重排，新增「重启面板」，并修掉「归纳全部」被静默缩成一批的问题。

### 新增

- **重启面板**：设置页顶栏与 `POST /api/restart`，就地重启面板进程，端口等需重启的配置立即生效；重启后自动重连，端口变更时自动跳转。保存后若端口或服务端代码与运行中的进程不一致，会提示「重启后生效」。
- **顶部导航分层**：业务主线（对话、知识浏览、知识图谱、能力画像、资源包）常驻；处理进度、工作区、采集源、运行日志、设置、归档收进「更多 ▾」，点击展开/收起。进入次要页自动展开，只记忆用户主动的展开偏好；窄屏下导航内部横向滚动并自动把当前项滚进可见区。

### 调整

- **设置页重排**：按分组拆成独立卡片，卡片内固定两列、统一标签列宽，说明文字统一放在控件下方，多行列表字段占满整行；操作栏吸顶，不再有滚动时露字、便签与输入框错位的问题。
- 设置项提示文案区分「环境变量覆盖」与「保存后未生效」。

### 修复

- **「归纳全部」静默按一批执行**：`limit=0` 被当成假值回落到 `max_atoms_per_run`（12 轮），任务正常结束但只处理一批。现在 `limit=0` 表示不限量（`pipeline/progress.py`、`pipeline/llm_enrich.py`），面板与 `minime llm --all` 均一次处理全部待处理轮次。
- 面板少量无匹配控制台请求的 404 噪声。

## v0.1.0-pre.1（2026-10-20）

第一个预发布版本。核心链路已跑通：多源采集 → 清洗成知识 → 多维标签 → 加权检索 → MCP 分身 → Web 面板。

### 新增

- **采集适配器按通道分目录**：`collector/adapters/trae/`（云端会话、会话快照、工作区索引、Trae 记忆）与 `collector/adapters/files/`（本地文档、Obsidian）。新增数据源只需在对应通道目录写一个类并在该通道 `__init__.py` import 一行，不改调度与流水线。
- **定期知识整理**（`pipeline/consolidate.py`）：清洗后自动执行——到期条目归档、低价值活动事件按保留期归档、同 kind+scope 且标题与正文都高度相近的条目标记「已取代」。只改状态不删数据，幂等可重跑。
- **长文全文入库 + 摘要**（`pipeline/summarize.py`）：不再按 160 字硬截断。接入 LLM 时对超长正文生成摘要（按内容签名缓存，避免重复调用），未接入则全文入库。
- **反馈降权可恢复**：连续 N 次「不相关」（默认 2）进入 `dormant`（降权）而非归档；正常命中不足时按惩罚系数补召回；任一「采用/引用/验证」自动恢复 `active`，面板左侧「状态 → 已降权」可手动恢复。冷门知识不会因为两次误标就再也召不回。
- **归纳全部**：处理进度页「归纳全部」按钮（带待处理轮次确认）与 `minime llm --all`，一次性处理所有待处理轮次；`--run N` 仍按数量跑一批。
- **知识浏览左侧主题手风琴**：知识类型 / 项目 / 技术 / 场景 / 标签分组可折叠，默认展开「知识类型」，展开其他分组时自动收起已展开的。

### 调整

- 卡片去掉鸡肋的「展开」，改为「详情」抽屉；反馈按钮点击后就地更新并提示降权进度（`reject_streak/threshold`）。
- 检索与对话召回已降权知识，结果标注「曾降权」。
- 设置页新增「检索与反馈」三项与「知识整理」配置；MCP 工具描述同步为「只降权不删除」。
- 全局文案精准化：避免口语化与 AI 化的提示，失败时直接给下一步命令。

### 修复

- 定时清洗（`--no-llm`）不再覆盖已有 LLM 摘要。
- `database is locked`：摘要调用模型前先提交，放开写锁。
- 摘要聚类过宽：去掉尾部纯路径括注、跳过纯英文/路径标题与 event、加正文 Jaccard 门槛。

### 已知限制

- Trae 云端接口只返回当前端会话，历史会话 404；主数据源是本地快照。
- 语义相关性闸门（embedding）默认关闭，需要自备 OpenAI 兼容接口。
- 记忆同步到 mnemosyne 默认关闭。
