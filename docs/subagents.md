# Subagents

本文档描述 Chord Code 当前已实现的 V1 subagent 机制，以及后续扩展 Multi-Agent 能力时应遵守的接口与边界。

## 1. 范围

V1 只实现 `SubAgent` 机制，不实现以下能力：

- `Agent Teams`
- `Agent-to-Agent`
- 后台长期运行的 subagent
- 跨 turn 的并行调度器

V1 当前唯一内置 subagent 类型是 `explore`。

## 2. 目标

V1 的目标是让 primary agent 能把一个聚焦的子任务委派给 child session，并把结果以标准化 tool output 回收到 parent context。

设计原则：

- parent / child 共享同一 worktree 和 runtime
- child conversation 独立持久化，避免把完整 transcript 污染 parent 上下文
- child 工具面严格按 agent profile 控制
- parent 只消费 summary + metadata，不消费 child reasoning 或历史 transcript
- 当前实现需要为更复杂的 Multi-Agent 机制保留扩展位

## 3. 核心抽象

### 3.1 AgentDefinition / AgentRegistry

代码位置：

- `src/chordcode/agents/types.py`
- `src/chordcode/agents/registry.py`

`AgentDefinition` 当前包含：

- `name`
- `mode: "primary" | "subagent"`
- `description`
- `prompt_template_path`
- `tool_allowlist`
- `permission_profile`
- `limits`
- `model_override`

`AgentRegistry` 是进程内 built-in registry。当前注册：

- `primary`
- `explore`

说明：

- `primary` 也被建模为 agent definition，用于统一 run request 和工具装配逻辑
- 是否暴露 `task` 工具，不再依赖字面值 `"primary"`，而是依赖 `agent.mode == "primary"`

### 3.2 RunRequest / RunResult

代码位置：`src/chordcode/agents/types.py`

`RunRequest` 当前用于显式描述一次 agent run 的上下文：

- `session_id`
- `agent_name`
- `source`
- `root_session_id`
- `parent_session_id`
- `parent_tool_call_id`
- `trace_id`
- `parent_observation_id`
- `limits`

`RunResult` 当前返回：

- `assistant_message_id`
- `trace_id`
- `finish`

## 4. Session 模型与持久化

代码位置：

- `src/chordcode/model.py`
- `src/chordcode/store/sqlite.py`

`Session` 新增字段：

- `kind: "primary" | "subagent"`
- `agent_name`
- `root_session_id`
- `parent_session_id`
- `parent_tool_call_id`

行为约定：

- root session 的 `root_session_id` 等于自身 `id`
- child session 的 `parent_session_id` 指向 parent session
- child session 的 `parent_tool_call_id` 指向触发它的 parent tool call
- `GET /sessions` 默认隐藏 child sessions
- 可通过 `include_children=true` 或 `parent_session_id=...` 显式查询 child sessions

## 5. task 工具

代码位置：`src/chordcode/tools/task.py`

### 5.1 schema

```json
{
  "type": "object",
  "properties": {
    "description": { "type": "string" },
    "prompt": { "type": "string" },
    "subagent_type": { "type": "string", "enum": ["explore"] },
    "session_id": { "type": "string" }
  },
  "required": ["description", "prompt", "subagent_type"]
}
```

字段语义：

- `description`: 父工具面板显示名
- `prompt`: 发给 child agent 的实际任务指令
- `subagent_type`: 当前只支持 `explore`
- `session_id`: 可选；用于在同一个 child session 内继续追问

### 5.2 Resume 语义

V1 的 resume 不是恢复挂起协程，而是：

1. 找到已有 child session
2. 追加一条新的 user message
3. 在该 session 上重新执行一次 child run

约束：

- 只能 resume 同一 root session 下面的 child session
- `agent_name` 必须匹配
- child session 当前若仍在运行，则拒绝 resume

## 6. task 结果如何产生

代码位置：`src/chordcode/agents/service.py`

`task` 工具返回结果不是 child transcript，也不是额外跑一轮 summarizer 得到的二次摘要。

当前固定流水线是：

1. child session 完成本次 run
2. 取“本次 run 创建的最后一条 assistant message”
3. 只抽取该 message 的 `text` parts，并按顺序拼接
4. 不复制 child 的 reasoning parts
5. 不复制 child 的 tool messages
6. 若没有最终文本，则使用 partial text 或标准 fallback 文本
7. 包装成 parent 可见的 tool output

返回格式：

```text
<subagent_summary>
...child assistant 最终文本或部分文本...
</subagent_summary>

<task_metadata>
session_id: ...
subagent_type: explore
assistant_message_id: ...
trace_id: ...
status: completed|timed_out|interrupted|error|max_turns_exceeded|max_tool_calls_exceeded
error_code: ...
error_message: ...
parallel_group_id: ...
</task_metadata>
```

说明：

- `ToolResult.metadata` 里也会同步保存上述字段
- 当前之所以保留文本版 `<task_metadata>`，是因为 loop 仍然只把 tool 文本回灌给模型
- 这是明确的 V1 tech debt；未来如果 loop 支持结构化 tool metadata 回灌，应优先迁移到 metadata-only 模式

## 7. explore subagent

### 7.1 prompt

代码位置：`src/chordcode/prompts/agents/explore.txt`

`explore` 的职责是：

- 进行只读调查
- 回答聚焦问题
- 给 parent agent 返回简短、证据导向的 summary

### 7.2 工具面

`explore` 当前 allowlist：

- `read`
- `glob`
- `grep`
- `memory_search`
- `memory_get`
- `websearch`
- `webfetch`

明确不暴露：

- `bash`
- `write`
- `todo`
- `task`
- `skill`

### 7.3 权限

`explore` child session 的默认 permission profile：

- `read/glob/grep/memory_search/memory_get/websearch/webfetch => allow "*"`
- `external_directory => ask "*"`
- `task => deny "*"`
- `* => deny`

继承规则：

- 只继承 parent session 的显式 `deny`
- 不继承 parent 的历史 ask/allow approvals
- 这样可以保证 read-only 探索默认顺畅，同时保留越界访问与显式 deny 的约束

## 8. 并行 task batches

代码位置：`src/chordcode/loop/session_loop.py`

当前仅在以下条件下启用并行：

- 同一 assistant 响应中有 `2..3` 个 tool calls
- 并且这些 tool calls 全部都是 `task`

不满足条件时，仍按普通串行 tool batch 执行。

并行语义：

- 每个 child run 在独立 async task 中执行
- 一个 child 失败、超时或命中限制，不会取消同批其它 child
- parent session 被 interrupt 时，整批 child 一起取消
- parent turn 必须等待整批 settle，才会进入下一轮 LLM

写回顺序：

- 执行完成顺序可以乱序
- parent assistant 上的 completed tool parts 与 synthetic tool messages 按原始 tool-call 顺序写回
- 这样 parent 下一轮看到的上下文是确定性的

## 9. limits / timeout / interrupt

`explore` 当前 limits：

- `max_turns = 8`
- `max_tool_calls = 24`
- `max_wall_time_ms = 300000`

收口规则：

- 命中 `max_turns` => `finish = max_turns_exceeded`
- 命中 `max_tool_calls` => `finish = max_tool_calls_exceeded`
- 超时 => `finish = timed_out`
- parent interrupt / user cancel => `finish = interrupted`

实现要点：

- child run 在独立 `asyncio.Task` 中执行
- parent `task` 工具以短轮询监控 child 完成、parent interrupt、wall timeout
- `SessionLoop` 对 `CancelledError` 做显式收口，确保 partial text 会落库，child session 不会卡在 busy 状态

## 10. 可观测性

当前可观测性包括：

- child session 自身完整消息历史
- parent 侧 `task.started` / `task.finished` / `task.failed` 事件
- JSONL 日志中的 `root_session_id` / `parent_session_id`
- Langfuse trace 通过显式 `trace_id + parent_observation_id` 传播

约定：

- parent `task` tool span 是 child run 的直接父 observation
- child 内部的普通消息事件仍属于 child session，不会串到 parent SSE

## 11. 关键代码地图

- `src/chordcode/agents/types.py`: agent definitions 与 run contracts
- `src/chordcode/agents/registry.py`: built-in primary / explore
- `src/chordcode/agents/service.py`: child session 生命周期、task 执行、结果回收、工具构建
- `src/chordcode/tools/task.py`: parent delegate tool
- `src/chordcode/loop/session_loop.py`: run orchestration、parallel task batch、tool execution、interrupt 收口
- `src/chordcode/store/sqlite.py`: session schema 与 child session 查询能力
- `src/chordcode/prompts/agents/explore.txt`: explore subagent prompt

## 12. 当前限制

V1 已知限制：

- 只有 `explore` 一个内置 subagent
- 没有 token budget enforcement
- 没有 child session 专门的 UI 导航
- 没有后台 subagent 或持久 worker
- 没有结构化 metadata 回灌到 parent model

如果后续要扩展 `Agent Teams` 或 `Agent-to-Agent`，请优先复用现有 `AgentDefinition / RunRequest / child session / trace propagation` 这条主线，而不是另起一套 runtime 协议。
