# coding agent

一个 Python 多 Agent 编码助手。核心源码在 `coding_agent/`，评测脚本在 `benchmarks/`。

## 评测（benchmarks）

为项目声称的量化性能数字提供可复现的实测支撑。所有脚本通过
`benchmarks/_common.py` 复用项目的 config / client。

```bash
uv run python -m benchmarks.run_all                # 跑全部
uv run python -m benchmarks.run_all --only mcp     # 只跑 MCP 延迟加载

uv run python -m benchmarks.bench_mcp_lazy          # ② 延迟加载节省（零 API）
uv run python -m benchmarks.bench_compact_recall     # ③ 压缩召回率（需 LLM）
uv run python -m benchmarks.bench_parallel           # ④ 并行 vs 串行（需 LLM）
```

| 评测 | 需真实 LLM | 说明 |
|------|-----------|------|
| `bench_mcp_lazy` | 否（纯确定性） | MCP 延迟加载避免注入的 schema token 占比 |
| `bench_compact_recall` | 是 | auto_compact 摘要对埋点 gold facts 的召回率 |
| `bench_parallel` | 是 | in-process 多 worker 并行 vs 串行加速比 |

详细说明见 [`benchmarks/README.md`](benchmarks/README.md)。

## 测试

```bash
uv run pytest tests/
```
