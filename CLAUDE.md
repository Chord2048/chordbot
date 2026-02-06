# Chord Code — Agent 快速上手（面向 Coding Agent）

> 目的：让新来的 Agent **在 5 分钟内**理解本项目要做什么、现在做到哪、从哪里开始改、以及该看哪些参考资料。

## 0. 一句话概览

`Chord Code` 是一个 **local-first 的 Agent Core（MVP）**：提供 Agent loop + Tool registry + Permission gate + SSE 事件流 + SQLite 持久化，并附带一个简单的 Web UI 用于可视化 session/message/parts/permissions/events。

## 1. 项目目标 / 非目标

**目标**
- 复刻/对齐 OpenCode 风格的内核：`loop` 编排、`tools` 执行、`permission` 拦截、`bus` 事件驱动、`store` 落库。
- 多客户端接入：任何前端/CLI 通过 `SSE (/events)` 订阅事件即可实时展示。
- 安全默认值：所有外部副作用（尤其 `bash`、文件写入、跨目录访问）必须走 permission gate。
- 可观测性：可选 Langfuse 追踪 LLM 调用/工具执行/异常。

**暂不作为 v0.1.x 强目标（但在 Roadmap 中）**
- Context compaction / memory / summary
- 插件自动加载（Plugin loader）与更完整的插件生态
- 多 Agent（plan/build/explore/compaction 等）与子任务编排
- MCP / sandbox runtime / 远程执行环境

## 2. 当前开发进度（以仓库为准）

参考：`docs/project.md`（标注为 v0.1.1 MVP+）、`CHANGES.md`（变更记录）。

**已具备的主链路**
- FastAPI API + 静态 Web：`src/chordcode/api/app.py`
- Session loop（流式 + tool calling + 中断）：`src/chordcode/loop/session_loop.py`
- 权限系统（ask/allow/deny + approvals 持久化）：`src/chordcode/permission/service.py`
- 事件总线（进程内 pub/sub）+ SSE：`src/chordcode/bus/bus.py`、`/events`
- SQLite 持久化：`src/chordcode/store/sqlite.py`
- 工具（当前：bash/read/write）与 registry：`src/chordcode/tools/*`
- Hooks（用于在关键生命周期点做可插拔改写/观测）：`src/chordcode/hooks.py`、`src/chordcode/hookdefs.py`
- Langfuse（可选）：`docs/langfuse.md`、`src/chordcode/observability/*`

**注意**
- `docs/agent-core-roadmap.md` 里用 “Plugin/Hooks 系统” 对齐 OpenCode 的插件化能力；本仓库当前已有 Hooker/Hook 定义，但**尚未做“插件自动发现/加载”**（若要做 v0.2，按 roadmap 设计补齐）。
- `FastAPI(title="Chord Code", version="0.1.0")` 与文档 v0.1.1 标记可能不一致：以 `docs/project.md` + `CHANGES.md` 记录为主，必要时同步版本号。

**近期方向（Owner 已确认）**
- 优先扩展内置工具：`Todo`（生成计划）、`Task`（子 Agent 派发任务）、`skill`（按需加载 skills）。
- 现有策略保持默认 `ask`，但增加“免 ask”的快速测试开关（仅限本地开发）。
- 工程化建设：日志、运行脚本、CLI 调试工具，让 Claude Code 等 Coding Agent 更容易验证/迭代本项目。

## 3. 快速运行（本地）

**依赖**
- Python 3.11+
- `uv`

**启动**
```bash
cp .env.example .env
uv sync
uv run uvicorn chordcode.api.app:app --reload --port 4096
```

**访问**
- Web UI：`http://127.0.0.1:4096/`

## 4. 配置（.env）

模板：`.env.example`

**必需（OpenAI-compatible Chat Completions）**
- `OPENAI_BASE_URL`
- `OPENAI_API_KEY`
- `OPENAI_MODEL`

**可选**
- `CHORDCODE_SYSTEM_PROMPT`：全局 system prompt
- `CHORDCODE_DB_PATH`：SQLite 路径（默认 `./data/chordcode.sqlite3`）
- `CHORDCODE_DEFAULT_WORKTREE`：默认 worktree（未填会自动探测 git worktree）
- `CHORDCODE_HOOK_DEBUG=1`：输出 hooks 调试日志
- `CHORDCODE_DEFAULT_PERMISSION_ACTION=ask|allow|deny`：创建 session 时的默认权限策略（推荐默认 `ask`；本地快速测试可临时用 `allow`）

**Langfuse（可选）**
- 见 `docs/langfuse.md`；如果不配置 key，则等价于关闭追踪（代码会降级运行）。

## 5. 关键代码入口（建议从这里读）

- `src/chordcode/api/app.py`：HTTP API、SSE、web 静态文件挂载、run/interrupt 端点、组装工具上下文
- `src/chordcode/loop/session_loop.py`：核心编排（LLM 流式、tool_calls、permission gate、落库、发事件、中断）
- `src/chordcode/model.py`：Message/Part/Permission 等 Pydantic 模型（前后端对齐的“协议层”）
- `src/chordcode/tools/`：工具实现（bash/read/write）+ path 限制/截断 + registry
- `src/chordcode/permission/service.py`：权限询问与规则匹配、pending approvals、reply 流程
- `src/chordcode/store/sqlite.py`：表结构与 CRUD
- `web/`：前端渲染（按 Message Header + Part 展示、权限面板、事件面板）
- `docs/project.md`：本项目当前架构/数据流/API/事件结构（强烈建议先看）
- `CHANGES.md`：版本间改动点与决策背景

## 6. 开发约定（写代码前先对齐）

- **不要绕过 permission gate**：任何会执行命令/写文件/跨目录访问的能力，都应显式建模为 tool + permission。
- **事件驱动优先**：UI/CLI 的状态靠 SSE 订阅事件，而不是轮询/塞私货状态。
- **变更要“协议先行”**：若改 Part/Message/事件结构，先改 `model.py`，再同步 `session_loop.py` + `api/app.py` + `web/app.js`。
- `src/chord_code.egg-info/` 属于构建产物；一般不需要手改（如需清理，用构建/打包流程处理）。
- 不要提交 `.env`（包含密钥）。

## 6.1 Agent 开发指南（务必遵守）

**文档规范**
- 文档必须简洁、专业、可维护；避免“长篇大论”和重复叙述。
- 若无明确要求，**不要随意新增文档/新增大段说明**；优先在既有文档里补充最小必要信息。
- 任何对行为/协议/接口有影响的改动完成后，必须自查是否需要同步更新：
  - `docs/` 下相关文档（尤其 `docs/project.md`）
  - `Agent.md`（确保新来的 Agent 依然能 5 分钟上手）

**代码风格**
- 追求：简单、可扩展、功能解耦（编排/执行/存储/展示清晰分层）。
- 尽可能少用继承；优先使用组合、显式依赖注入、`Protocol`/接口抽象来构建层级。
- 保持模块边界：`loop` 只负责编排；`tools/permission/store/llm` 各自负责具体执行与副作用。
- 在关键逻辑处添加**英文注释**（解释 “why / contract / edge cases”），避免大段注释，保持可读性。

## 7. 测试与验证

当前测试主要覆盖 hooks/loop 的关键行为：`tests/test_hooks.py`

```bash
uv run python -m unittest discover -s tests -q
```

（如后续引入 pytest，再补充统一测试入口。）

## 8. 常见改动路径（给 Agent 的“导航”）

**新增一个工具**
1. 在 `src/chordcode/tools/` 实现一个 `Tool`
2. 在 `src/chordcode/api/app.py` 的 `/sessions/{session_id}/run` 里注册到 `ToolRegistry(...)`
3. 在 `permission` 里定义/使用对应 permission（通常是 tool 名或更细粒度的类别）
4. 更新 `docs/project.md`（工具列表/权限类别）与 Web UI（如需要新展示）

**近期要加的 3 个工具（建议的最小可用定义）**
- `todo`：输入为“目标/约束/偏好”，输出为结构化 todo（步骤、优先级、可并行项、验收标准）；用于 UI 展示与后续执行跟踪（先不要求自动执行）。
- `task`：输入为“任务说明 + 目标输出格式 + 限制”，输出为子任务结果摘要（后续再接入真正的 subagent/多 agent runtime）。
- `skill`：按名称加载技能正文（`skill(name=...)`）；可用技能列表通过 `skill` 工具 description 内嵌的 `<available_skills>` 暴露（对齐 OpenCode 的按需加载思路）。

**新增/调整一个 Hook 点**
1. 在 `src/chordcode/hookdefs.py` 添加 hook 名与 input/output schema
2. 在调用点（通常 `session_loop.py` / `api/app.py` / `permission/service.py`）触发 `hooks.trigger(...)`
3. 补充 `tests/test_hooks.py` 覆盖行为

**改事件结构 / 前端展示**
1. 先改 `src/chordcode/model.py` / 事件发布处
2. 再改 `web/app.js` 的 state 管理与渲染函数
3. 用浏览器 + SSE Events 面板核对是否符合预期

## 9. 推荐参考信息（本仓库内）

**强烈推荐优先阅读**
- `docs/project.md`
- `CHANGES.md`

**可选参考**
- `docs/agent-core-roadmap.md`：对标 OpenCode 的能力缺口与 v0.2+ 规划
- `docs/langfuse.md`：可观测性配置与排障
- `refs/opencode/`：OpenCode（开源 AI Coding Agent）。用于对齐“目标体验/能力边界”，重点关注：多 Agent（build/plan）权限差异、终端/TUI 交互与 client/server 架构思路。
- `refs/nanobot/`：nanobot（超轻量个人 AI Assistant）。用于参考“最小但可用”的工程组织方式，重点关注：agent loop ↔ tools、配置/CLI、bus/路由、skills loader、subagent/后台任务与 cron 思路。
- `refs/learn-claude-code/`：learn-claude-code（从零构建 Agent 的教学项目）。用于补齐“Agent 模式方法论”，重点关注：核心循环、显式规划（Todo）、子任务（Subagent/Task）、按需知识注入（Skills）——与本仓库计划补齐的 `todo/task/skill` 工具强相关。

## 10. 工程化建设清单（优先做“让 Agent 更好用”）

- 日志：统一 logging（服务端、loop、tools、permission），支持 `CHORDCODE_LOG_LEVEL`/输出到文件（便于回放问题）。
- 运行脚本：提供 `scripts/`（dev/run/test/format/lint/doctor）统一入口，减少每次手动拼命令。
- CLI 调试工具：可以一键 `create session -> send message -> run -> stream events -> reply permissions`，用于快速回归与对比不同 LLM/provider 的行为。
- 固化“验收动作”：为每个新增工具/协议变更提供可重复的 CLI/脚本验证路径（比只靠浏览器点点点更可靠）。
