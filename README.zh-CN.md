# minime

<p align="center">
  <a href="https://github.com/TWToolsShare/minime/blob/main/README.md">English</a> · <b>简体中文</b>
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/TWToolsShare/minime/main/docs/brand/logo-dark.svg" alt="minime" width="340">
</p>

<p align="center">把你和 AI 一起干的活，变成随时能问的「第二个你」。</p>

你在 Trae、Qoder、WorkBuddy 里做过的判断、踩过的坑、定下的规范，大多散落在聊天记录里，过几天就找不回来。minime 在本机把这些轨迹收起来，整理成一条条可检索的知识，再用对话或 MCP 交还给 AI——让它按你以前的做法回答，而不是泛泛而谈。

- **只读采集**：不改动任何被采集应用的数据
- **本地优先**：知识库、配置、日志都留在你自己的电脑上
- **有据可查**：每条回答都标注来源，能回到具体会话与文件
- **越用越准**：你标记「有用 / 不相关」，检索排序会跟着调整

当前版本 `v0.1.0-pre.7`，支持 macOS / Windows / Linux。

<p align="center">
  <img src="https://raw.githubusercontent.com/TWToolsShare/minime/main/docs/panel.png" alt="minime 面板：知识浏览" width="900">
</p>

---

## 三步开始

### 1. 安装

需要 Python 3.10+。推荐用 pipx 安装，独立环境，不污染系统 Python：

```bash
pipx install \
  --index-url "https://repo.repsy.io/tengwang/pypi/simple/" \
  --pip-args="--prerelease=allow" minime

# 三个可选增强：MCP 接入、读取 Trae 云端会话、向量检索加速
pipx inject minime mcp cryptography numpy
```

> 三个可选依赖只在上游 PyPI，所以拆成 `install` + `inject` 两步。不装也能用，只是少了 MCP、云端会话和向量加速。

### 2. 首次构建

```bash
minime setup
```

这一步会把你本机已有的数据采集进来、清洗成知识条目，并建立知识图谱和能力画像。第一次需要几分钟。

### 3. 打开面板

```bash
minime web
```

浏览器访问 **http://127.0.0.1:8765/**。

第一次进来，建议按这个顺序走一遍：

1. 顶栏 **处理进度** → 点「完整更新」→ 等它跑完
2. 顶栏 **对话** → 问一句你最近想过的问题
3. 觉得答得不准 → 回 **处理进度** 补归纳

---

## 面板里有什么

11 个页签，日常只用三个：

| 页签 | 用途 | 什么时候用 |
|---|---|---|
| **对话** | 基于你的知识库提问、追问，回答带来源 | 最常用 |
| **处理进度** | 看采集、清洗、归纳各做到哪一步 | 想知道「还剩多少活」 |
| **知识浏览** | 按项目、技术、类型筛选知识条目 | 想翻具体内容 |

其余的按需使用：

| 页签 | 用途 |
|---|---|
| 知识图谱 | 项目、技术、概念之间的关系图 |
| 能力画像 | 从你的工作轨迹里归纳出的 8 个维度评分 |
| 资源包 | 导出 / 导入可分享的知识包 |
| 工作区 | 哪些工作区被采集了、各有多少会话 |
| 采集源 | 每个数据源的状态、采集周期、手动触发 |
| 运行日志 | 所有操作记录，可按级别和关键词过滤 |
| 设置 | 填目录、配模型、改采集周期 |
| 归档 | 原始数据的存放位置与占用空间 |

---

## 日常怎么用

### 问它问题

进入 **对话**，底部输入问题，Enter 发送，Shift+Enter 换行。

回答会遵守两条规则：

- 知识库里有依据 → 给结论，并用 `[1] [2]` 标出来源，点编号可看原文
- 知识库里没有 → 先明确说「知识库里没有这条」，再给通用建议并标注「（通用经验）」

这样你能一眼分清哪些是你的经验，哪些是模型的推测。

同一会话可以连续追问，它记得最近 6 轮。左侧是历史会话，可切换、重命名、删除，刷新页面不会丢。

习惯命令行的话：

```bash
minime chat "我有哪些架构决策？"
minime chat --session S18e10c207b6f "那依据是什么？"   # 接着上次聊
minime chat --list                                     # 历史会话
```

### 把还没归纳的活干完

打开 **处理进度**，看「待处理」数量：

- 点 **「立即归纳一批」**：处理 12 轮，适合顺手补一点
- 点 **「归纳全部」**：一次处理完，耗时较长，可随时在顶栏停止
- 命令行：`minime llm --run 50` 或 `minime llm --all`，后台执行，关掉终端也会继续

进度按「已处理轮次 / 有实质改动的轮次」计算。一轮可能提炼出 0 条，也可能 3 条，所以按轮次看比按条数看更准确。

### 告诉它什么有用

知识浏览页每张卡片底部有 **采用 / 引用 / 不相关**：

- 采用、引用 → 这条知识以后更容易被检索到
- 不相关 → 降权并轻微降低可信度；连续多次会进入「已降权」，但不删除，需要时可在左侧「状态 → 已降权」恢复

对话中被引用的知识也会自动加分。这就是「越用越好用」。

---

## 它能收集哪些东西

| 数据来源 | 需要配置吗 | 说明 |
|---|---|---|
| **Trae** | 不需要 | 会话快照、云端会话、工作区文件历史、`~/.trae/memory` 记忆 |
| **本地文档 / 笔记 / 代码** | 需要 | 设置页「本地数据源 → 采集目录」，每行一个路径 |
| **Obsidian** | 一般不需要 | 自动发现 vault；也可手动指定。支持 frontmatter、`[[双链]]`、`#标签` |
| **WorkBuddy / WorkBuddy AI** | 不需要 | 会话、长期记忆、已填写的身份文件 |
| **Qoder** | 不需要 | 本地会话、消息正文、每轮改动的文件清单 |
| **豆包** | 需要 | 推荐把导出的 JSON / Markdown 放到固定目录，再在设置页填「豆包导出目录」 |

几点说明：

- **本地目录**：加完目录后下次采集自动生效；想立刻采就 `minime collect --force`。
- **Obsidian 隐私**：在某篇笔记的 frontmatter 里写 `private: true` 或 `minime: ignore`，整篇不会被采集。
- **豆包**：导出目录方案不需要任何系统权限。只有想直接读豆包应用容器时才需要给「运行 minime 的终端」授予 macOS 完全磁盘访问权限（不是给豆包授权），且正文若存在 IndexedDB 中仍需走导出方案。
- **采集频率**：设置页「定时采集 · 各源周期」可分别调整。改周期不需要重装定时任务；只有改「定时触发间隔」才需要 `minime install`。
- **定时采集**：macOS 用 launchd，Windows 用计划任务，Linux 用 `minime watch` 常驻。定时任务只做采集和清洗，归纳仍由你手动触发。

---

## 接进 Trae / Claude Code / Cursor

让 AI 客户端直接调用你的知识库：

```json
{
  "mcpServers": {
    "minime": {
      "command": "minime",
      "args": ["mcp"]
    }
  }
}
```

> `minime` 不在 PATH 里时，`command` 填 Python 解释器绝对路径，`args` 填 `["/绝对路径/minime/mcp_server/server.py"]`。

跨 AI 或跨机器共享同一份记忆时，用 HTTP 模式：

```bash
minime mcp --http --port 8791
# 客户端填 http://<主机>:8791/mcp
```

接上之后可以直接说「用 minime 查一下我以前怎么处理这个问题的」。共 18 个工具，最常用的四个：

| 工具 | 用途 |
|---|---|
| `minime_decide` | 依据你的历史经验给决策建议 |
| `minime_recall` | 检索知识 |
| `minime_chat` | 多轮问答 |
| `minime_troubleshoot` | 查排障经验 |

---

## 分享你的经验

把知识打包成资源包，别人导入后就能复用你的判断方式：

```bash
minime pack export --name my-worker --author alice
minime pack inspect my-worker.minime.zip      # 先看里面有什么
minime pack import my-worker.minime.zip --dry-run
minime pack import my-worker.minime.zip
```

- 导出的包默认不包含原始对话，绝对路径会归一为 `~`，密钥和 token 替换为 `***`
- 包内按「知识集」分组，导入后默认全部关闭，用哪套开哪套
- 只能导出自己创建的包；别人导入的包只能使用，不能再导出或删除

---

## 数据与隐私

| 事项 | 说明 |
|---|---|
| 采集方式 | 只读，不修改任何原始应用数据 |
| 数据位置 | macOS `~/Library/Application Support/minime`；Windows `%LOCALAPPDATA%\minime`；Linux `~/.local/share/minime` |
| 自定义位置 | 设置环境变量 `MINIME_HOME` |
| 网络 | 不主动上传任何内容；面板只监听 `127.0.0.1`，拒绝局域网访问 |
| 配置文件 | `config.json` 权限 600，内含密钥时注意保管 |
| 导出 | 默认脱敏，敏感属性默认不带 |

升级、换版本、换安装方式都不会动数据目录。备份只需拷走整个数据目录；换电脑时把目录拷过去，或在新机器上设置同一个 `MINIME_HOME`。

从源码版迁到 pipx 版时：

```bash
minime migrate --from /path/to/old/minime
```

---

## 常用命令

```bash
# 日常
minime web                     打开面板
minime status                  一眼看全：采集 / 归纳 / 定时 / 服务
minime chat "问题"             命令行问一句

# 更新与归纳
minime update                  增量更新（采集 + 清洗）
minime collect --force         立刻全部重采
minime llm --run 20            后台归纳一批
minime llm --all               归纳全部待处理轮次

# 配置
minime config                  查看当前生效配置
minime set llm.enabled true    修改一项配置
minime set collect.local.roots ~/notes

# 排查与数据
minime doctor                  自检，全部通过退出码为 0
minime logs -f                 跟随运行日志
minime restore                 从原始归档重建知识库（不重新采集）
minime diag                    导出脱敏诊断包，方便反馈问题

# 定时采集
minime install                 安装定时采集（macOS launchd / Windows 计划任务）
minime uninstall               移除定时任务，数据保留
```

---

## 常见问题

**安装后提示 `minime: command not found`**
pipx 的 bin 目录不在 PATH 里。执行 `pipx ensurepath`，或重开终端。也可以用 `minime link` 把命令注册到 `~/.local/bin`。

**处理进度里的「待处理」一直不动**
这是正常的。定时任务只采集和清洗，归纳需要你手动触发：点「立即归纳一批 / 归纳全部」，或运行 `minime llm --run 20`。

**问了问题，它说「知识库里没有」**
先看「处理进度」是否还有待归纳的轮次；再看「采集源」和「工作区」，确认相关内容确实被采到了。

**某个采集源显示红色**
点进「采集源」，红行下面会写下一步该做什么；也可以运行 `minime doctor` 看详情。单个源异常不影响其他源。

**豆包提示 Operation not permitted**
macOS 隐私保护导致的，不是豆包坏了。推荐把豆包导出文件放到固定目录，在设置页填「豆包导出目录」，无需任何权限。

**改了配置不生效**
`web.*` 需要重启面板，`schedule.*` 需要重新 `minime install`，`collect.*` 下次采集生效。`minime set` 保存时会直接提示该做哪一步。

**知识库被清空了**
数据不会真丢，原始归档还在。运行 `minime restore` 可完整重建，不需要重新采集。

**日志在哪**
数据目录下 `store/logs/minime.jsonl`，或面板「运行日志」页。

**想反馈问题**
运行 `minime diag`，会生成一个已脱敏的诊断包（自检 + 进度 + 配置 + 最近日志），直接发出去即可。

---

## 更多文档

- [使用手册](https://github.com/TWToolsShare/minime/blob/main/docs/使用手册.md)：面向使用者的完整操作说明
- [更新记录](https://github.com/TWToolsShare/minime/blob/main/CHANGELOG.md)：每个版本的新增、调整与已知限制
- [参与贡献](https://github.com/TWToolsShare/minime/blob/main/CONTRIBUTING.md)：开发环境、测试、提交规范
- [安全策略](https://github.com/TWToolsShare/minime/blob/main/SECURITY.md)：如何私下报告安全问题

## 许可

MIT
