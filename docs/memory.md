# Local Memory

Chord Code 支持面向本地 session 的工作区记忆机制，设计目标参考 OpenClaw，但保持实现简洁、模块解耦，并优先适配当前项目的本地运行链路。

## Scope

- 仅本地 session 启用记忆能力
- Daytona session 不注入记忆 prompt，也不注册记忆工具
- 记忆按 `worktree` 隔离，每个工作区维护独立索引库

## File Conventions

- `memory.md`
  - 当前工作区的长期记忆
  - 会被注入到 agent 的系统上下文
- `memory/YYYY-MM-DD.md`
  - 按日期归档的会话日志和阶段性结论
  - 当前实现会在创建新的本地 session 时，将同一 worktree 下最近一个本地 session 的对话内容追加写入当天文件
- 参与索引的文件范围为：
  - `memory.md`
  - `memory/**/*.md`

## Architecture

实现位于 `src/chordcode/memory/`，分为四层：

- `chunking.py`
  - 按行切块，目标约 1000 字符，默认 200 字符重叠
  - 保留 1-based 行号，便于检索后回读原文
- `store.py`
  - 负责 SQLite 持久化和 FTS5 索引
  - 主要表：
    - `meta`
    - `files`
    - `chunks`
    - `chunks_fts`
- `embeddings.py`
  - 对接 OpenAI-compatible embeddings 接口
  - 向量以 JSON 形式存入 SQLite
  - 查询时在 Python 中做 cosine similarity
- `manager.py` / `service.py`
  - `MemoryManager` 负责单个 worktree 的扫描、增量同步和混合搜索
  - `MemoryService` 负责应用级后台轮询和 manager 生命周期
  - `archive.py` 负责把 session 历史渲染为追加式 Markdown 归档

## Storage Layout

每个 worktree 对应一个独立 SQLite 记忆库，路径为：

```text
<dirname(resolved db_path)>/memory/<worktree_sha>.sqlite3
```

这样可以避免不同项目之间的记忆互相污染，也便于后续单独清理或迁移索引文件。

## Sync Model

`MemoryService` 在应用启动后启动后台同步循环，并自动接管：

- `cfg.default_worktree`
- 已存在的本地 session worktree
- 新建本地 session 的 worktree

此外，创建新的本地 session 时会触发一次自动归档：

- 选择同一 `worktree` 下最近一个已有本地 session
- 提取其中的 user / assistant 文本内容
- 追加写入当天的 `memory/YYYY-MM-DD.md`
- 写入完成后调度一次后台同步，避免创建 session 时被索引/embedding 阻塞

同步机制特点：

- 扫描 `memory.md` 和 `memory/**/*.md`
- 基于文件内容 hash 判断新增、修改、删除
- 对变更文件做增量重建
- 基于 chunk hash 复用未变化 chunk 的 embeddings
- `memory_search` 在查询前会执行一次轻量 stale-check；如果发现索引过期，则只调度后台刷新，不阻塞当前查询

同步频率由以下配置控制：

```yaml
memory:
  sync_interval_seconds: 3
```

## Search Pipeline

`memory_search` 走单一的混合检索链路，方便后续继续优化：

1. 使用 FTS5 做 BM25 关键词检索
2. 如果配置了 embedding provider，则生成查询向量
3. 在 Python 中对已存储的 chunk 向量做 cosine similarity
4. 合并词法分数和向量分数，对重复 chunk 去重后排序

当前默认权重：

- 向量：`0.65`
- BM25：`0.35`

如果没有配置 embedding provider，或者查询时 embedding 失败，则会退化为纯 BM25 检索，并在工具返回结果中附带 warning 信息。

## Prompt Injection

记忆 prompt 通过 system transform hook 动态注入，不把分支逻辑塞进主 session loop。

行为包括：

- 将 `memory.md` 作为 `Workspace Memory` 段注入系统上下文
- 最大注入长度为 8000 字符
- 超出时带明确的截断标记
- 追加 OpenClaw 风格的使用规则

当前规则重点是：

- 涉及过往工作、偏好、决策、待办时先使用 `memory_search`
- 需要精确引用原文时再使用 `memory_get`
- 稳定、长期有效的信息写回 `memory.md`
- 带日期的阶段性结论追加到 `memory/YYYY-MM-DD.md`
- 写入使用现有文件读写工具完成，不额外引入 `memory_write`

## Logging

memory 模块补充了较完整的日志，便于观察索引与归档行为。

启动或接管 worktree 时会记录：

- 正在监控的 `memory` 目录
- 当前发现的 `memory.md` / `memory/**/*.md` 文件数量
- 当前归档文件数量
- 当前 SQLite 索引文件路径
- 已索引文件数和 chunk 数

同步过程中会记录：

- 是否检测到文件集合变化
- 哪些文件是新增、修改、删除
- 每次同步完成后的源文件统计与索引统计

自动归档时会记录：

- 触发归档的新 session
- 被归档的旧 session
- 目标归档文件路径
- 本次归档包含的消息条数
- 归档完成后索引中的文件数和 chunk 数

## Tools

### `memory_search`

用于基于混合检索查询当前 worktree 的记忆内容。

参数：

- `query`
- `max_results=5`
- `min_score=0.15`

返回 JSON 字符串，结构如下：

```json
{
  "hits": [
    {
      "path": "memory/2026-03-10.md",
      "start_line": 1,
      "end_line": 12,
      "score": 0.82,
      "snippet": "......",
      "source": "hybrid"
    }
  ],
  "stats": {
    "worktree": "/path/to/worktree",
    "index_age_ms": 42
  }
}
```

### `memory_get`

用于读取原始记忆文件内容，不经过索引回读。

参数：

- `path`
- `from_line=1`
- `max_lines=200`

返回 JSON 字符串，结构如下：

```json
{
  "path": "memory.md",
  "from_line": 1,
  "to_line": 20,
  "text": "......"
}
```

限制：

- 只允许读取 `memory.md` 或 `memory/` 目录下的文件
- 用于精确引用、核对上下文和读取完整片段

## Configuration

```yaml
memory:
  enabled: true
  embedding_base_url: "https://api.openai.com/v1"
  embedding_api_key: "REPLACE_ME"
  embedding_model: "text-embedding-3-small"
  sync_interval_seconds: 3
```

字段说明：

- `enabled`
  - 总开关
- `embedding_base_url`
  - embeddings 服务地址
- `embedding_api_key`
  - embeddings 服务密钥
- `embedding_model`
  - embeddings 模型名
- `sync_interval_seconds`
  - 后台同步周期

## Extension Points

当前实现故意把搜索链路集中在 `MemoryManager.search()`，便于后续替换或增强：

- 接入 `sqlite-vec` 或 ANN 索引
- 增加 reranker
- 调整 BM25 / 向量权重
- 替换 chunking 策略
- 增加记忆写入策略或自动归档策略

v1 先保证本地可用、可测、边界清晰，再继续做检索效果优化。
