# Chord Code 🤖

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white)
![Runtime](https://img.shields.io/badge/Runtime-Local--First-1F6FEB?style=flat-square)
![Agent](https://img.shields.io/badge/Agent-Personal%20Assistant-111827?style=flat-square)

> 一个偏工程化、可扩展、可持续运行的个人通用 Agent 助手。  
> 内置 Web UI、CLI 和 HTTP API，支持权限控制、会话持久化、定时任务、子代理、MCP、技能、知识库、渠道接入与可观测性。

[![示意图](https://picui.ogmua.cn/s1/2026/03/18/69b97ede1d703.webp)](https://picui.ogmua.cn/s1/2026/03/18/69b97ede1d703.webp)
Chord Code 既可以直接作为你的个人 AI 助手，也适合作为二次开发底座，用来构建面向代码、知识、自动化工作流的智能系统。

## ✨ 为什么是 Chord Code

- 🏠 Local-first，默认围绕本地工作区运行，同时支持 Daytona 远程 runtime
- 🧰 同一套后端同时提供 Web UI、CLI 和 REST API
- 🔒 内置 permission gate，方便把 Agent 自动化和人工确认结合起来
- 🧠 支持 workspace 级 memory、知识库检索和技能扩展
- ⏰ 支持 cronjobs，让 Agent 按计划自动唤醒并持续工作
- 🛰 支持多渠道接入，当前已实现 Feishu 适配器
- 📈 提供 Langfuse tracing、结构化日志和 SQLite 持久化

## 🧩 核心能力

| 能力 | 说明 |
| --- | --- |
| 🤖 Agent Loop | 支持 session-based 对话、工具调用、事件流和中断控制 |
| 🖥️ Multi Interface | 内置 Web UI、CLI、REST API，适合手动使用和程序集成 |
| 🔐 Permission System | 对 bash、文件、外部目录等能力做细粒度授权 |
| 🗃️ Persistence | 使用 SQLite 持久化 session、message、todo、cron job 和运行历史 |
| ⏰ Cron Jobs | 定时唤醒已有 session，执行周期性任务 |
| 🧠 Local Memory | 支持 `memory.md` 与 `memory/YYYY-MM-DD.md` 归档和检索 |
| 🕵️ Subagents | 支持 read-only `explore` 子代理，适合聚焦调查类任务 |
| 🔌 Extensibility | 支持 MCP server、skills、知识库检索与文档解析 |
| 📡 Channel Integration | 支持可扩展渠道机制，当前内置 Feishu |
| 📊 Observability | 支持 Langfuse tracing 与 JSONL 结构化日志 |

## 🚀 快速开始

### 1. 环境要求

- Python 3.11+
- `uv`

### 2. 安装依赖

```bash
uv sync
```

### 3. 初始化配置

Chord Code 当前使用 **YAML 配置文件**，不再依赖 `.env`。

```bash
mkdir -p ~/.chordcode
cp config.yaml.example ~/.chordcode/config.yaml
```

最少需要配置一个 OpenAI-compatible LLM endpoint：

```yaml
openai:
  base_url: "https://api.deepseek.com/v1"
  api_key: "REPLACE_ME"
  model: "deepseek-chat"
```

配置优先级说明：

- 全局配置：`~/.chordcode/config.yaml`
- 项目级配置：`./.chordcode/config.yaml`
- 项目级配置会覆盖全局配置

### 4. 启动服务

```bash
uv run chordcode serve --reload --port 4096
```

启动后访问：

- Web UI: [http://127.0.0.1:4096](http://127.0.0.1:4096)
- API Base URL: [http://127.0.0.1:4096](http://127.0.0.1:4096)

### 5. 做一次健康检查

```bash
uv run chordcode doctor
```

## ⚡ 常见用法

### 一次性运行一个任务

```bash
uv run chordcode run "总结当前仓库结构，并给出下一步建议" --permission allow
```

### 手动管理 Session

```bash
uv run chordcode sessions create --worktree /path/to/project --title "My Session"
uv run chordcode sessions list --limit 10
uv run chordcode agent send <session-id> "继续刚才的任务"
uv run chordcode agent run <session-id>
```

### 创建一个定时任务

```bash
uv run chordcode cronjobs create \
  --session-id <session-id> \
  --name hourly-summary \
  --message "请总结最近进展并给出下一步计划" \
  --kind every \
  --every-ms 3600000
```

### 查看日志

```bash
uv run chordcode logs files
uv run chordcode logs view --level ERROR --limit 20
```

## 🧠 可扩展能力

- `runtime.backend = local | daytona`：在本地或 Daytona sandbox 中运行 Agent
- `channels.feishu`：把 Agent 接入飞书
- `memory`：为本地 workspace 提供长期记忆与归档
- `web_search.tavily_api_key`：启用 Web Search
- `kb` / `vlm`：接入知识库与文档解析能力
- `mcp`：连接外部 MCP server，扩展工具面
- `skills`：加载项目级或用户级技能，复用固定工作流

## 📚 文档

- [CLI 使用说明](docs/cli.md)
- [Cron Jobs 设计与用法](docs/cronjobs.md)
- [Local Memory 设计](docs/memory.md)
- [Subagents 机制](docs/subagents.md)
- [变更记录](CHANGES.md)

## 🧪 开发与测试

```bash
uv run pytest
```

如果你更偏好 `unittest`：

```bash
uv run python -m unittest discover -s tests
```

## ⚠️ 使用提示

- 不要提交包含密钥的配置文件
- `default_permission_action: allow` 只建议用于本地开发或测试
- Tavily、Memory Embeddings、Langfuse、Daytona、Feishu、KB/VLM 都是可选增强能力
- 如果你在寻找一个可长期迭代的个人 Agent 平台，而不是一次性 demo，这个仓库会比“agent shell”更合适
