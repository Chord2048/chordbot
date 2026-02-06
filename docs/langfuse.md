# Langfuse 可观测性

Langfuse 为 chord-code 提供全面的可观测性，自动追踪 LLM 调用、工具执行和会话流程。

## 快速开始

### 1. 获取凭证

访问 [https://cloud.langfuse.com](https://cloud.langfuse.com)：
1. 注册并创建项目
2. 在项目设置中创建 API 密钥
3. 复制 `Public Key` 和 `Secret Key`

### 2. 配置环境变量

在项目根目录的 `.env` 文件中添加：

```bash
# Langfuse 凭证 (必需)
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_BASE_URL=https://cloud.langfuse.com

# 可选配置
LANGFUSE_ENABLED=true
LANGFUSE_TRACING_ENVIRONMENT=development  # development/staging/production
LANGFUSE_SAMPLE_RATE=1.0  # 采样率 (0.0-1.0)
LANGFUSE_DEBUG=false
```

### 3. 启动服务

```bash
cd src/chordcode/api
python -m uvicorn app:app --reload
```

访问 http://localhost:8000 开始使用。

### 4. 查看追踪

API 返回的响应中包含追踪链接：

```json
{
  "assistant_message_id": "msg_123...",
  "trace_id": "trace_456...",
  "trace_url": "https://cloud.langfuse.com/trace/trace_456..."
}
```

点击 `trace_url` 即可在 Langfuse 中查看完整的追踪信息。

## 自动追踪的内容

### LLM 调用
- 模型名称和参数（temperature, top_p 等）
- 输入提示和输出结果
- Token 使用量（输入/输出/总计）
- 成本（美元）
- 延迟和流式响应时间
- API 错误和异常

### 工具执行
- 工具名称和调用 ID
- 输入参数
- 输出结果和元数据
- 执行时间
- 权限检查
- 执行错误

### 会话追踪
- 会话 ID 用于关联操作
- 会话元数据（工作目录、当前目录、模型等）
- 完整的对话流程
- 嵌套的工具和 LLM 调用

### 错误监控
- 异常类型和消息
- 上下文信息
- 权限拒绝记录

## 追踪结构

每个会话创建一个追踪，结构如下：

```
Session Trace
├─ OpenAI Generation (LLM 调用 1)
│  └─ Input/Output, Tokens, Cost
├─ Tool Span (工具执行 1)
│  └─ Input, Output, Time
├─ Tool Span (工具执行 2)
├─ OpenAI Generation (LLM 调用 2)
└─ ...
```

## 配置选项

### 环境隔离

使用不同的环境标签区分部署：

```bash
LANGFUSE_TRACING_ENVIRONMENT=development  # 本地开发
LANGFUSE_TRACING_ENVIRONMENT=staging      # 预发布环境
LANGFUSE_TRACING_ENVIRONMENT=production   # 生产环境
```

在 Langfuse UI 中可以按环境过滤追踪。

### 采样

生产环境高流量时可以使用采样：

```bash
LANGFUSE_SAMPLE_RATE=0.1  # 只追踪 10% 的请求
```

### 调试模式

启用详细日志：

```bash
LANGFUSE_DEBUG=true
```

将输出追踪创建、更新和刷新的详细信息。

### 禁用追踪

不需要追踪时：

```bash
LANGFUSE_ENABLED=false
```

或者直接不设置 `LANGFUSE_PUBLIC_KEY` 和 `LANGFUSE_SECRET_KEY`。

## 在 Langfuse 中查看

### 仪表板

1. 访问 Langfuse 项目仪表板
2. 侧边栏点击 "Traces"
3. 过滤条件：
   - **Session ID**: 查看完整对话
   - **Environment**: development/staging/production
   - **Date range**: 时间范围
   - **Tags**: 自定义标签

### 追踪详情

点击任意追踪查看：
- **时间线视图**: 所有操作的可视化展示
- **嵌套结构**: LLM 调用和工具执行的层级关系
- **输入/输出**: 每个操作的完整数据
- **元数据**: 会话信息、模型参数、自定义数据
- **指标**: Token 使用、成本、延迟
- **错误**: 异常详情和上下文

## 最佳实践

### 1. 设置合适的环境标签
根据部署环境设置 `LANGFUSE_TRACING_ENVIRONMENT`：
- `development`: 本地开发和测试
- `staging`: 预发布测试
- `production`: 生产系统

### 2. 监控成本
使用 Langfuse 的成本追踪功能：
- 按会话查看成本
- 按环境汇总成本
- 设置成本预警

### 3. 定期检查错误
在 Langfuse 错误仪表板中查看：
- 失败的工具执行
- LLM API 错误
- 权限拒绝

### 4. 生产环境使用采样
高流量应用使用采样降低开销：
```bash
LANGFUSE_SAMPLE_RATE=0.1  # 10% 采样
```

## 故障排除

### 追踪未显示

1. **检查凭证**: 确认 `LANGFUSE_PUBLIC_KEY` 和 `LANGFUSE_SECRET_KEY` 正确
2. **检查网络**: 确认可以访问 `LANGFUSE_BASE_URL`
3. **检查启用状态**: 确保 `LANGFUSE_ENABLED=true`
4. **查看日志**: 查找控制台中 `[Langfuse]` 开头的消息
5. **等待刷新**: 追踪数据是异步批量发送的，等待 10-30 秒

### 追踪不完整

如果追踪缺少操作：
1. 检查所有操作是否成功完成
2. 确保应用正常关闭（调用 `flush()`）
3. 查看日志中的错误消息

### 性能开销过大

如果追踪导致性能问题：
1. 启用采样: `LANGFUSE_SAMPLE_RATE=0.1`
2. 检查到 Langfuse 服务器的网络延迟
3. 确认异步处理正常工作（事件应该是批量发送的）

### 启用调试

查看详细信息：
```bash
LANGFUSE_DEBUG=true
```

将显示：
- 追踪创建时机
- Span 更新信息
- 事件刷新时机
- 处理过程中的错误

## 架构说明

### 核心组件

1. **langfuse_client.py**: 单例客户端管理器
   - 初始化 Langfuse 客户端
   - 提供全局访问接口
   - 处理刷新和关闭

2. **session_loop.py**: 会话级追踪
   - 为每个会话创建追踪
   - 追踪工具执行
   - 追踪错误和异常

3. **openai_chat.py**: LLM 调用追踪
   - 使用 Langfuse OpenAI 包装器
   - 自动记录 tokens、成本、延迟

4. **langfuse_hook.py**: 钩子系统集成
   - 记录模型参数
   - 记录工具执行详情
   - 记录自定义事件

5. **app.py**: 生命周期管理
   - 启动时初始化 Langfuse
   - 关闭时刷新和清理
   - 返回追踪 URL

### 数据流

```
User Request
    ↓
Session Loop (创建 Trace)
    ↓
    ├─→ OpenAI Call (自动创建 Generation)
    │   └─ Langfuse OpenAI 包装器自动记录
    │
    ├─→ Tool Execution (创建 Span)
    │   ├─ 记录输入
    │   ├─ 执行工具
    │   └─ 记录输出和时间
    │
    └─→ Error Handling (更新 Trace/Span)
        └─ 记录错误类型、消息、上下文
    ↓
Flush to Langfuse (异步批量)
```

## 相关资源

- [Langfuse 文档](https://langfuse.com/docs)
- [Python SDK 指南](https://langfuse.com/docs/sdk/python/sdk-v3)
- [OpenAI 集成](https://langfuse.com/docs/integrations/openai)
- [追踪概念](https://langfuse.com/docs/tracing)
