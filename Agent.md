# Chord Code — Agent 快速上手（面向 Coding Agent）

> 目的：让新来的 Agent **在 5 分钟内**理解本项目要做什么、现在做到哪、从哪里开始改、以及该看哪些参考资料。

## 0. 一句话概览

`Chord Code` 是一个 **local-first 的 Agent Core（MVP）**：提供 Agent loop + Tool registry + Permission gate + SSE 事件流 + SQLite 持久化，并附带一个简单的 Web UI 用于可视化 session/message/parts/permissions/events。

**如果本次任务涉及 `task` 工具、subagent、child session、并行 task batch、或 multi-agent 相关扩展，先阅读 `docs/subagents.md`。**

## 1. 项目目标 / 非目标

**目标**
- 复刻/对齐 OpenCode 风格的内核：`loop` 编排、`tools` 执行、`permission` 拦截、`bus` 事件驱动、`store` 落库。
- 多客户端接入：任何前端/CLI 通过 `SSE (/events)` 订阅事件即可实时展示。
- 安全默认值：所有外部副作用（尤其 `bash`、文件写入、跨目录访问）必须走 permission gate。
- 可观测性：可选 Langfuse 追踪 LLM 调用/工具执行/异常。

**暂不作为 v0.1.x 强目标（但在 Roadmap 中）**
- Context compaction / memory / summary
- 插件自动加载（Plugin loader）与更完整的插件生态
- 更复杂的 Multi-Agent 机制（Agent Teams / Agent-to-Agent）
- sandbox runtime / 远程执行环境

## 2. 当前开发进度（以仓库为准）

参考：`docs/project.md`（标注为 v0.1.1 MVP+）、`CHANGES.md`（变更记录）。

**已具备的主链路**
- FastAPI API + 静态 Web：`src/chordcode/api/app.py`
- Session loop（流式 + tool calling + 中断）：`src/chordcode/loop/session_loop.py`
- 权限系统（ask/allow/deny + approvals 持久化）：`src/chordcode/permission/service.py`
- 事件总线（进程内 pub/sub）+ SSE：`src/chordcode/bus/bus.py`、`/events`
- SQLite 持久化：`src/chordcode/store/sqlite.py`
- 工具（当前：bash/read/write/skill/todowrite/websearch/webfetch）与 registry：`src/chordcode/tools/*`
- Subagent 基础设施（`AgentRegistry` / `RunRequest` / child sessions / `task` tool / parallel task batches）：`src/chordcode/agents/*`、`src/chordcode/tools/task.py`
- MCP 客户端（发现配置、管理连接、暴露 MCP 工具给 LLM）：`src/chordcode/mcp/*`
- Hooks（用于在关键生命周期点做可插拔改写/观测）：`src/chordcode/hooks.py`、`src/chordcode/hookdefs.py`
- YAML 配置系统（全局 + 项目级深度合并、字段元数据注册、Settings UI）：`src/chordcode/config.py`、`src/chordcode/config_schema.py`
- Langfuse（可选）：`docs/langfuse.md`、`src/chordcode/observability/*`

**注意**
- `docs/agent-core-roadmap.md` 里用 “Plugin/Hooks 系统” 对齐 OpenCode 的插件化能力；本仓库当前已有 Hooker/Hook 定义，但**尚未做“插件自动发现/加载”**（若要做 v0.2，按 roadmap 设计补齐）。
- `FastAPI(title="Chord Code", version="0.1.0")` 与文档 v0.1.1 标记可能不一致：以 `docs/project.md` + `CHANGES.md` 记录为主，必要时同步版本号。

**近期方向（Owner 已确认）**
- 已落地 V1 subagent delegation：primary agent 通过 `task` 工具唤起 `explore` child session。
- 下一阶段 Multi-Agent 重点是更复杂的编排机制：`Agent Teams`、`Agent-to-Agent`、更多内置 subagent 类型。
- 现有策略保持默认 `ask`，但增加“免 ask”的快速测试开关（仅限本地开发）。
- 工程化建设：日志、运行脚本、CLI 调试工具，让 Claude Code 等 Coding Agent 更容易验证/迭代本项目。

## 3. 快速运行（本地）

**依赖**
- Python 3.11+
- `uv`

**启动**
```bash
# 首次：复制示例配置并填入必填项
cp config.yaml.example ~/.chordcode/config.yaml
# 编辑 ~/.chordcode/config.yaml，填入 openai.base_url / api_key / model

uv sync

# 方式 1: CLI（推荐）
chordcode serve --reload --port 4096

# 方式 2: 直接用 uvicorn
uv run uvicorn chordcode.api.app:app --reload --port 4096
```

**CLI 快速验证**
```bash
chordcode doctor                          # 检查环境健康
chordcode config show                     # 查看当前配置
chordcode run "Reply PONG" --permission allow  # 端到端测试
```

**访问**
- Web UI：`http://127.0.0.1:4096/`
- CLI 帮助：`chordcode --help`（完整命令树见 `docs/cli.md`）

## 4. 配置（YAML）

配置采用 YAML 文件（也支持 JSON），按优先级从低到高依次加载并深度合并：
1. 内置默认值
2. 全局：`~/.chordcode/config.yaml`（或 `.json`）
3. 项目级：`{worktree}/.chordcode/config.yaml`（或 `.json`）——覆盖同名字段

模板：`config.yaml.example`

**必需（OpenAI-compatible Chat Completions）**
- `openai.base_url`
- `openai.api_key`
- `openai.model`

**可选**
- `system_prompt`：全局 system prompt（空则加载 `prompts/default.txt`）
- `db_path`：SQLite 路径（默认 `./data/chordcode.sqlite3`）
- `default_worktree`：默认 worktree（空则自动探测 git root）
- `default_permission_action`：`ask` | `allow` | `deny`（推荐 `ask`）
- `hooks.debug`：输出 hooks 调试日志
- `logging.*`：日志级别/输出/目录/轮转/保留
- `web_search.tavily_api_key`：Tavily 搜索 API key
- `prompt_templates`：自定义模板变量（`{{key}}` 形式注入 system prompt）

**Langfuse（可选）**
- 见 `docs/langfuse.md`；`langfuse.*` 字段控制追踪行为。

**Settings UI**
- Web UI Activity Bar 中点击齿轮图标进入 Settings 面板
- Visual 标签页：只读查看所有配置字段及说明
- Raw YAML 标签页：直接编辑项目/全局配置文件并保存（保存后需重启生效）

**MCP 服务器配置（可选）**
- 在以下路径放置 `mcp.json`（后者覆盖前者同名 server）：
  - 全局：`~/.cursor/mcp.json`、`~/.chordcode/mcp.json`
  - 项目级：`{worktree}/.cursor/mcp.json`、`{worktree}/.chordcode/mcp.json`
- 格式：`{ "mcpServers": { "<name>": { "command": "...", "args": [...] } } }`（local）或 `{ "mcpServers": { "<name>": { "url": "..." } } }`（remote）
- 启动时自动发现并连接；MCP 工具以 `{server}_{tool}` 命名注入 ToolRegistry，权限类别为 `mcp`。

## 5. 关键代码入口（建议从这里读）

- `src/chordcode/api/app.py`：HTTP API、SSE、web 静态文件挂载、run/interrupt 端点、Config API（7 个端点）、组装工具上下文
- `src/chordcode/loop/session_loop.py`：核心编排（LLM 流式、tool_calls、permission gate、落库、发事件、中断）
- `src/chordcode/config.py`：YAML/JSON 配置加载、深度合并、验证、序列化（`Config` 及子 dataclass）
- `src/chordcode/config_schema.py`：配置字段元数据注册（key/description/default/sensitive/choices），供 Settings UI 与默认值生成使用
- `src/chordcode/model.py`：Message/Part/Permission 等 Pydantic 模型（前后端对齐的"协议层"）
- `src/chordcode/tools/`：工具实现（bash/read/write/skill/todowrite/websearch/webfetch）+ path 限制/截断 + registry
- `src/chordcode/agents/`：agent 定义、registry、task delegation service、run request/result
- `src/chordcode/tools/task.py`：主 agent 委派 subagent 的入口工具
- `src/chordcode/mcp/`：MCP 客户端支持（config 加载、server 连接管理、tool adapter）
- `src/chordcode/permission/service.py`：权限询问与规则匹配、pending approvals、reply 流程
- `src/chordcode/store/sqlite.py`：表结构与 CRUD
- `src/chordcode/web/`：前端渲染（按 Message Header + Part 展示、权限面板、事件面板、Settings 面板）
- `docs/project.md`：本项目当前架构/数据流/API/事件结构（强烈建议先看）
- `docs/subagents.md`：subagent 的设计、task 工具契约、parallel batch、child session 与可观测性实现
- `CHANGES.md`：版本间改动点与决策背景

## 6. 开发约定（写代码前先对齐）

- **不要绕过 permission gate**：任何会执行命令/写文件/跨目录访问的能力，都应显式建模为 tool + permission。
- **事件驱动优先**：UI/CLI 的状态靠 SSE 订阅事件，而不是轮询/塞私货状态。
- **变更要"协议先行"**：若改 Part/Message/事件结构，先改 `model.py`，再同步 `session_loop.py` + `api/app.py` + `src/chordcode/web/app.js`。
- `src/chord_code.egg-info/` 属于构建产物；一般不需要手改（如需清理，用构建/打包流程处理）。
- 不要提交包含密钥的配置文件（`~/.chordcode/config.yaml` 中的 `api_key` 等 sensitive 字段）。`.env` 已弃用，项目不再读取环境变量作为配置。

## 6.1 Agent 开发指南（务必遵守）

**文档规范**
- 文档必须简洁、专业、可维护；避免“长篇大论”和重复叙述。
- 若无明确要求，**不要随意新增文档/新增大段说明**；优先在既有文档里补充最小必要信息。
- 任何对行为/协议/接口有影响的改动完成后，必须自查是否需要同步更新：
  - `Agent.md`（确保新来的 Agent 依然能 5 分钟上手）

**代码风格**
- 追求：简单、可扩展、功能解耦（编排/执行/存储/展示清晰分层）。
- 尽可能少用继承；优先使用组合、显式依赖注入、`Protocol`/接口抽象来构建层级。
- 保持模块边界：`loop` 只负责编排；`tools/permission/store/llm` 各自负责具体执行与副作用。
- 在关键逻辑处添加**英文注释**（解释 “why / contract / edge cases”），避免大段注释，保持可读性。

## 7. 测试与验证

测试覆盖：hooks/loop、logging、permission、prompt template、web tools、skills、API 端点等。

```bash
uv run python -m pytest tests/ -v
```

## 8. 常见改动路径（给 Agent 的“导航”）

**新增一个工具**
1. 在 `src/chordcode/tools/` 实现一个 `Tool`
2. 在 `src/chordcode/api/app.py` 的 `/sessions/{session_id}/run` 里注册到 `ToolRegistry(...)`
3. 在 `permission` 里定义/使用对应 permission（通常是 tool 名或更细粒度的类别）
4. 更新 `docs/project.md`（工具列表/权限类别）与 Web UI（如需要新展示）

**内置工具（当前与近期）**
- `todowrite`：已实现。输入结构化 todo 列表，输出任务状态与统计，用于过程跟踪。
- `skill`：已实现。按名称加载技能正文（`skill(name=...)`）；可用技能列表通过 `skill` 工具 description 内嵌的 `<available_skills>` 暴露。
- `task`：已实现。primary agent 可把子任务委派给 `explore` subagent；child run 落在独立 session，结果以标准化 tool output 回收到 parent。

**Skills 功能说明（已实现 v1）**
- `Skills 是什么`：Skill 是一个可版本化的能力包目录，核心文件是 `SKILL.md`（YAML frontmatter + Markdown 正文）；模型平时只看到 metadata，需要时再按名加载正文。
- `Skills 在哪些目录`（仅扫描当前 session worktree 范围）：`skills/*/SKILL.md`、`.claude/skills/*/SKILL.md`、`.agents/skills/*/SKILL.md`、`.opencode/skill/*/SKILL.md`、`.opencode/skills/*/SKILL.md`。
- `项目里怎么实现`：
  1. 发现与校验：`src/chordcode/skills/loader.py`（从 `cwd` 向上到 `worktree` 扫描；校验 `name/description`、目录名一致性、name regex）。
  2. 工具暴露与按需加载：`src/chordcode/tools/skill.py`（description 生成 `<available_skills>`；执行时加载正文并返回 `<skill_content>` + `<skill_files>`）。
  3. 运行时接线：`src/chordcode/api/app.py` 在 `ToolRegistry` 注册 `SkillTool`。
  4. 权限控制：`permission="skill"`；规则评估复用 `src/chordcode/permission/rules.py`，`deny` 的 skill 不会出现在可用列表中。

**新增/调整一个 Hook 点**
1. 在 `src/chordcode/hookdefs.py` 添加 hook 名与 input/output schema
2. 在调用点（通常 `session_loop.py` / `api/app.py` / `permission/service.py`）触发 `hooks.trigger(...)`
3. 补充 `tests/test_hooks.py` 覆盖行为

**新增/修改一个配置字段**
1. 在 `src/chordcode/config_schema.py` 中注册字段元数据（`_r(...)` 调用）
2. 在 `src/chordcode/config.py` 的对应 dataclass 中添加字段，并在 `_build_config()` 中解析
3. 在消费处（`api/app.py`、`session_loop.py` 等）读取 `cfg.xxx`
4. 更新 `config.yaml.example`；如为新增 section，更新 `CLAUDE.md` 第 4 节
5. Settings UI 自动从 `/config/schema` 获取字段元数据，无需额外改前端

**新增/调整 CLI 命令**
1. 在 `src/chordcode/cli/commands/` 下添加或修改命令模块
2. 在 `src/chordcode/cli/app.py` 中注册到 typer root app
3. 使用 `client.py` 调用 API；使用 `output.py` 处理 JSON / rich 输出
4. 更新 `docs/cli.md` 命令树与示例

**改事件结构 / 前端展示**
1. 先改 `src/chordcode/model.py` / 事件发布处
2. 再改 `src/chordcode/web/app.js` 的 state 管理与渲染函数
3. 用浏览器 + SSE Events 面板核对是否符合预期

**MCP 功能说明（已实现）**
- `MCP 是什么`：Model Context Protocol 客户端支持，允许 Agent 连接外部 MCP 服务器并使用其工具（文件系统、数据库、搜索 API 等）。
- `配置发现`：启动时扫描全局 + 项目级 `mcp.json`，解析 `mcpServers` 字段。`command` → local/stdio，`url` → remote/streamable-http（可显式指定 `"transport": "sse"`）。
- `项目里怎么实现`：
  1. 配置加载：`src/chordcode/mcp/config.py`（`MCPServerConfig` + `load_mcp_configs()`）。
  2. 连接管理：`src/chordcode/mcp/client.py`（`MCPManager`：并发连接、工具缓存、call_tool、生命周期管理）。
  3. 工具适配：`src/chordcode/mcp/tool_adapter.py`（`MCPToolAdapter` 实现 `Tool` Protocol，session loop 无需改动）。
  4. 运行时接线：`src/chordcode/api/app.py` 在 startup 初始化 `MCPManager`，在 `run_session` 注入 MCP 工具到 `ToolRegistry`。
  5. API 端点：`GET /mcp/status`、`GET /mcp/tools`、`POST /mcp/{name}/connect`、`POST /mcp/{name}/disconnect`、`POST /mcp/servers`。
  6. 权限控制：`permission="mcp"`，patterns 为 `{server}_{tool}` 命名空间。
  7. Hook 点：`mcp.server.connect`、`mcp.tool.call`（定义在 `hookdefs.py`）。

## 9. 推荐参考信息（本仓库内）

**强烈推荐优先阅读**
- `CHANGES.md`
- `docs/subagents.md`（当任务涉及 `task` / subagent / child session / multi-agent 扩展时）

**可选参考**
- `docs/agent-core-roadmap.md`：对标 OpenCode 的能力缺口与 v0.2+ 规划
- `docs/langfuse.md`：可观测性配置与排障
- `refs/opencode/`：OpenCode（开源 AI Coding Agent）。用于对齐“目标体验/能力边界”，重点关注：多 Agent（build/plan）权限差异、终端/TUI 交互与 client/server 架构思路。
- `refs/nanobot/`：nanobot（超轻量个人 AI Assistant）。用于参考“最小但可用”的工程组织方式，重点关注：agent loop ↔ tools、配置/CLI、bus/路由、skills loader、subagent/后台任务与 cron 思路。
- `refs/learn-claude-code/`：learn-claude-code（从零构建 Agent 的教学项目）。用于补齐“Agent 模式方法论”，重点关注：核心循环、显式规划（Todo）、子任务（Subagent/Task）、按需知识注入（Skills）——与本仓库计划补齐的 `todo/task/skill` 工具强相关。

## 10. 工程化建设清单（优先做"让 Agent 更好用"）

- ~~日志：统一 logging~~ ✓ 已完成。通过 `logging.*` YAML 配置控制级别/输出/目录/轮转/保留。
- ~~配置系统~~ ✓ 已完成。YAML + JSON 文件配置、全局/项目级合并、Config API、Settings UI。
- ~~CLI 工具~~ ✓ 已完成。`chordcode` CLI（typer + rich），完整命令树见 `docs/cli.md`。入口 `src/chordcode/cli/app.py`。
- 运行脚本：提供 `scripts/`（dev/run/test/format/lint/doctor）统一入口，减少每次手动拼命令。
- CLI 调试工具：可以一键 `create session -> send message -> run -> stream events -> reply permissions`，用于快速回归与对比不同 LLM/provider 的行为。
- 固化"验收动作"：为每个新增工具/协议变更提供可重复的 CLI/脚本验证路径（比只靠浏览器点点点更可靠）。
