# Coding Agent

一个用 Python 实现的多 Agent 编码助手，对标 Claude Code / Cursor 的编码工作流。

基于流式工具调用、分层记忆、多 Agent 协作与精细权限控制构建，支持终端 TUI 和 Web 远程控制两种界面。核心源码在 `coding_agent/`，可复现的量化评测在 `benchmarks/`。

## 功能亮点

### Agent 主循环
- **流式工具调用**：LLM 输出边生成边执行工具，`partition_tool_calls` 依据 `is_concurrency_safe` 把并发安全的调用（如 ReadFile）合并成并行批次用 `asyncio.gather` 执行，减少往返时延。
- **max_tokens 自动恢复**：碰到输出上限时自动升级 token 预算（最高 64K）并提示模型断点续写，最多恢复 3 次。
- **事件驱动架构**：`Agent.run()` 产出统一事件流（文本增量 / 工具调用 / 权限请求 / 压缩通知 / 钩子事件），UI 与远程层无耦合地消费。
- **计划模式（plan mode）**：只允许只读工具、生成计划文件，实现"先方案后动手"。

### 分层记忆系统
- **自动记忆沉淀**：用户级 + 项目级双路径，四种记忆类型（user / feedback / project / reference）路由到独立文件，每 5 轮自动提取并维护 `MEMORY.md` 索引。
- **记忆召回**：LLM selector 从记忆清单中挑选相关条目，**非阻塞**并行预取，工具执行后注入，不拖慢主流程。
- **会话持久化与续聊**：JSONL 追加式记录、元数据独立存储，支持 `create / list / resume / delete / cleanup`；压缩边界可精确恢复。
- **上下文压缩**：基于真实 token 用量锚点的双阈值触发，9 段结构化摘要 + 近期原文保留；大型工具结果落盘（保留 2KB 预览），`RecoveryState` 在压缩后重挂最近读过的文件与激活的技能。
- **记忆写入安全扫描**：写盘前拦截 prompt 注入、角色劫持、密钥泄露与隐藏文本攻击（记忆会注入后续会话，防跨会话污染）。

### 多 Agent 协作
- **子 Agent 工具**：主 agent 可派生子 agent 独立处理子任务，支持自定义提示词。
- **团队协作**：coordinator 模式（规划 → 并行研究 → 综合 → 实现 → 独立验证），带邮箱消息队列、成员生命周期管理、共享任务。
- **in-process 并行**：同进程内多 worker 并行执行，复用进程上下文，避免子进程开销。

### 权限与安全
- 五层权限决策：计划例外 → 安全命令白名单 → 危险命令拦截 → 路径沙箱 → 规则引擎 → 会话级放行 → 模式兜底 → 人工确认。
- 三种核心模式：`default`（写与命令需确认）、`acceptEdits`（写放行、命令确认）、`bypassPermissions`（全部放行）。
- **危险命令检测**：识别格式化/覆盖磁盘、权限提升、凭据窃取等风险命令。
- **路径沙箱**：限制文件访问边界，阻止越权读写。

### 工具与集成
- 内置工具：bash、grep、glob、read/write/edit file、子 Agent、文件状态缓存等。
- **MCP 支持**：stdio 与 HTTP（2025-03-26 规范）双传输，延迟加载避免 schema 注入上下文；工具名统一映射为 `mcp_{server}_{tool}`。
- **Hooks 事件系统**：事件驱动脚本钩子，可在工具调用前后、会话生命周期等节点插入自定义逻辑。
- **配置系统**：多 provider（OpenAI / Anthropic / 智谱 GLM 等 OpenAI 兼容接口），支持 `${ENV_VAR}` 环境变量替换与多文件分层合并。

## 快速开始

```bash
# 1. 安装依赖（uv 或 pip）
uv sync          # 或 pip install -e .

# 2. 准备配置（复制模板填写 API key）
cp config.example.yaml .coding_agent/config.yaml

# 3. 启动
uv run coding_agent           # 终端 TUI
uv run coding_agent --remote  # Web 远程控制（http://localhost:18888）
```

参考 [`config.example.yaml`](config.example.yaml) 了解全部配置项。

## 架构

```
coding_agent/
├── agent.py           # Agent 主循环、流式工具执行、事件流
├── conversation.py    # 会话管理、token 用量锚点估算
├── tools/             # 工具注册表与内置工具
├── context/           # 上下文压缩、分层记忆、恢复附件
├── memory/            # 自动记忆、召回、会话持久化、安全扫描
├── permissions/       # 五层权限、危险命令检测、路径沙箱
├── teams/             # 多 Agent 团队协作
├── mcp/               # MCP 客户端与工具包装
├── hooks/             # 事件钩子系统
├── client.py          # LLM 客户端（OpenAI / Anthropic / 兼容接口）
├── app.py             # 终端 TUI（Textual）
├── remote.py          # Web 远程服务
└── config.py          # 配置加载与环境变量
```

## 测试

```bash
uv run pytest tests/    # 577 个测试
```

覆盖：Agent 主循环、上下文压缩、记忆、权限、MCP、Hooks、团队协作、子 Agent、Skills、工作树、序列化、命令系统、远程权限模式。

## 评测（benchmarks）

为项目声称的量化性能提供可复现实测：

```bash
uv run python -m benchmarks.run_all
```

| 评测 | 需真实 LLM | 说明 |
|------|-----------|------|
| `bench_mcp_lazy` | 否（纯确定性） | MCP 延迟加载避免注入的 schema token 占比 |
| `bench_compact_recall` | 是 | 摘要对埋点 gold facts 的召回率 |
| `bench_parallel` | 是 | 多 worker 并行 vs 串行加速比 |

详细说明见 [`benchmarks/README.md`](benchmarks/README.md)。
