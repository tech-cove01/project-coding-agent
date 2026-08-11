"""benchmarks 包。

放置编码 agent 的量化学指标评测脚本（bench_*.py），配合 `run_all.py` 统一入口执行。
这些脚本用于为项目声称的性能数字（MCP 延迟加载节省、上下文压缩召回率、
多 Agent 并行加速比）提供可复现的实测支撑。

所有脚本均应支持从命令行直接运行：
    uv run python -m benchmarks.bench_mcp_lazy
    uv run python -m benchmarks.bench_compact_recall
    uv run python -m benchmarks.bench_parallel
    uv run python -m benchmarks.run_all
"""
