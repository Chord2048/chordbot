# CronJobs

`cronjobs` 能力用于让 agent 系统按计划自动唤醒，并在目标 session 内执行一次标准对话回合（注入一条用户消息 + 触发 `/sessions/{id}/run`）。

## 能力范围

- 定时调度：支持 `at`（一次性）、`every`（固定间隔）、`cron`（cron 表达式）
- 持久化：任务配置和运行状态存储在 SQLite
- 历史记录：每次执行都有 run 记录（开始/结束时间、状态、错误、assistant_message_id、trace_id）
- 管理方式：支持 REST API、CLI、Web 管理页

## 存储模型

数据库表：

- `cron_jobs`：任务定义 + 最新状态
- `cron_job_runs`：任务执行历史

`cron_jobs` 关键字段：

- `id`, `name`, `session_id`, `enabled`
- `schedule_kind`, `schedule_at_ms`, `schedule_every_ms`, `schedule_expr`, `schedule_tz`
- `payload_kind`（当前固定为 `agent_turn`）, `payload_message`
- `next_run_at_ms`, `last_run_at_ms`, `last_status`, `last_error`
- `last_assistant_message_id`, `last_trace_id`
- `delete_after_run`, `created_at`, `updated_at`

`cron_job_runs` 关键字段：

- `id`, `job_id`, `session_id`
- `started_at`, `finished_at`, `status`, `error`
- `assistant_message_id`, `trace_id`

## 调度行为

- 服务启动时自动加载并启动 `CronService`
- 当 `now >= next_run_at_ms` 时任务会被执行
- `at` 类型任务执行后：
  - `delete_after_run=true`：自动删除任务
  - 否则自动置为 `enabled=false`
- `every` / `cron` 类型任务在每次执行后重新计算 `next_run_at_ms`

## API

### 创建任务

`POST /cronjobs`

```json
{
  "name": "hourly-summary",
  "session_id": "session-id",
  "message": "请总结最近进展并给出下一步计划",
  "schedule": {
    "kind": "every",
    "every_ms": 3600000
  },
  "enabled": true,
  "delete_after_run": false
}
```

### 查询任务

- `GET /cronjobs?include_disabled=true`
- `GET /cronjobs?session_id=<sid>&include_disabled=true`
- `GET /cronjobs/{job_id}`

### 状态与历史

- `GET /cronjobs/status`
- `GET /cronjobs/{job_id}/runs?limit=50`

### 任务控制

- 启停：`POST /cronjobs/{job_id}/enabled`，body: `{"enabled": true|false}`
- 立即执行：`POST /cronjobs/{job_id}/run`，body: `{"force": true|false}`
- 删除：`DELETE /cronjobs/{job_id}`

## CLI

```bash
# 创建每小时任务
chordcode cronjobs create \
  --session-id <session-id> \
  --name hourly-summary \
  --message "请总结最近进展并给出下一步计划" \
  --kind every \
  --every-ms 3600000

# 查看任务
chordcode cronjobs list
chordcode cronjobs get <job-id>

# 立即触发（即使禁用也可）
chordcode cronjobs run <job-id> --force

# 启停与删除
chordcode cronjobs enable <job-id>
chordcode cronjobs disable <job-id>
chordcode cronjobs delete <job-id>

# 执行历史与服务状态
chordcode cronjobs runs <job-id> --limit 20
chordcode cronjobs status
```

## Web 管理页

在 Web UI 左侧 Activity Bar 点击 `Cron Jobs`（闹钟图标）可进入管理页，支持：

- 创建任务（选择 session、输入 message、配置 schedule）
- 查看任务列表与下次执行时间
- 启用/禁用任务
- 手动立即执行
- 删除任务
- 查看单任务执行历史

## 排障建议

- `cron` 类型创建报错：检查 `expr` 和 `tz` 是否有效
- 任务不触发：
  - 确认任务是 `enabled=true`
  - 检查 `next_run_at_ms` 是否存在且已到期
  - 查看 `GET /cronjobs/status` 中服务是否 `running=true`
- 执行失败：查看 `last_error` 或 `runs` 里的 `error` 字段
