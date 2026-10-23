# 参与贡献

感谢愿意一起完善 minime。这个项目的核心约束是**本地优先、只读采集、默认脱敏**，提交前请先确认没有把个人数据带进仓库。

## 开发环境

需要 Python 3.10+。核心功能零第三方依赖，下面三个是可选增强：

```bash
git clone https://github.com/TWToolsShare/minime.git
cd minime
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python run/minime.py doctor
```

Windows 下把 `.venv/bin/python` 换成 `.venv\Scripts\python`。

不想让开发过程读到本机数据时，用一个临时数据目录：

```bash
export MINIME_HOME="$(mktemp -d)"
.venv/bin/python run/minime.py web
```

## 跑测试

```bash
.venv/bin/python tests/smoke.py    # 使用临时数据目录，不碰本机知识库
.venv/bin/python run/minime.py doctor
```

CI 会在 macOS、Windows、Linux 上跑同一套冒烟。提交前至少本地跑一遍 `tests/smoke.py`。

## 代码结构

```
collector/      采集层：适配器 + 调度 + raw 归档
pipeline/       知识层：清洗、检索、对话、图谱、画像、资源包
web/            Web 面板（server.py + index.html）
mcp_server/     MCP 服务
run/            命令行入口
tests/smoke.py  CI 冒烟
```

## 新增数据源

1. 在 `collector/adapters/` 下选择通道目录（`trae/`、`files/`、`agents/`），新建适配器模块。
2. 继承 `SourceAdapter`，实现 `probe()` 与 `collect()`，声明 `name`、`kind`、`DEFAULT_INTERVAL_S`、`priority`、`label`、`fix_hint`。
3. 在该通道的 `__init__.py` 里 import 一行完成注册。
4. 在 `pipeline/normalize.py` 增加对应的清洗分支，给知识打上 project / tech / scene / topic 标签。
5. 在 `pipeline/settings_schema.py` 增加配置项（如果这个源需要路径或开关）。
6. 用构造数据补测试，并同步 `README.md`（英文）、`README.zh-CN.md`、使用手册和 CHANGELOG。

采集必须只读，不能修改被采集应用的数据；遇到权限问题时给出可执行的下一步，而不是静默失败。

## 提交前检查

- 没有提交 `store/`、`config.json`、`*.minime.zip`、数据库、日志或真实对话
- 没有硬编码个人路径、邮箱、内网地址、token 或密钥
- 新增用户可见功能时同步更新 `README.md` / `README.zh-CN.md` / 使用手册 / CHANGELOG
- 跨平台：路径用 `pathlib` / `os.path`，不写只在 POSIX 生效的逻辑
- 不新增必需依赖；确需新增时放进可选依赖并说明原因
- CI 通过

## 提交与 PR

- 一个提交只做一件事，说明「为什么改」，而不只是「改了什么」
- PR 描述里写清影响范围、验证方式和兼容性
- 涉及导出、脱敏、采集范围的改动，请特别说明隐私影响
- 提交即表示同意以 MIT 许可证发布
