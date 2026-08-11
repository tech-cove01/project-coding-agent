# benchmarks — 量化学指标评测

为编码 agent 声称的几项性能数字提供**可复现的实测支撑**。每个脚本均可独立运行，
也可通过 `run_all.py` 统一调度。

## 运行方式

```bash
# 统一入口（可 --only 选择）
uv run python -m benchmarks.run_all
uv run python -m benchmarks.run_all --only mcp
uv run python -m benchmarks.run_all --only mcp,compact

# 单独运行
uv run python -m benchmarks.bench_mcp_lazy          # 零 API
uv run python -m benchmarks.bench_compact_recall     # 需真实 LLM
uv run python -m benchmarks.bench_parallel           # 需真实 LLM
```

## 各评测一览

| 脚本 | 评测对象 | 需真实 LLM | 依赖 |
|------|---------|-----------|------|
| `bench_mcp_lazy.py` | ② MCP 延迟加载 token 节省比例 | 否（纯确定性） | 无 |
| `bench_compact_recall.py` | ③ 上下文压缩摘要召回率 | 是 | 已配置 provider |
| `bench_parallel.py` | ④ 多 Agent 并行 vs 串行加速比 | 是 | 已配置 provider |

所有脚本都通过 `benchmarks/_common.py` 复用项目的 `config` / `client`，
因此能读到真实 API key 与 provider 配置，而不会污染生产代码。

## ①~④ 映射说明

| 声称 | 评测入口 | 状态 |
|------|---------|------|
| ① 五层权限 + 沙箱 | 既有单测 `tests/test_permissions.py`（含短路径/软链逃逸用例） | 已有测试覆盖，暂未做评测脚本 |
| ② MCP 延迟加载 ≈74% | `bench_mcp_lazy.py` | 确定性，随时可跑 |
| ③ 两层压缩 + SWE-bench 34%→100% | `bench_compact_recall.py`（召回率） | 需真实 LLM |
| ④ 多 Agent 并行 ≈60% | `bench_parallel.py`（加速比） | 需真实 LLM |

## 关于 ③ 的边界说明

`auto_compact` 会**原样保留近期的 keep 尾部原文**，因此「靠 keep 躺赢」会虚高召回率。
`bench_compact_recall.py` 刻意把 gold facts 散布在**会被摘要的旧前缀**里，并在结果中
分别报告「摘要字符数 / keep 消息数 / 逐条召回」，让阅读者能区分「摘要召回」与「原文保留」。

## 常见问题

- **`bench_compact_recall.py` / `bench_parallel.py` 报「无 client」**：确认 `.coding_agent/config.yaml`
  已配置 providers 且 API key 可用。
- **`bench_parallel.py` 加速比偏低**：in-process 后端共享同一 event loop，LLM 调用是
  I/O 密集（非 CPU 密集），并行主要节省的是「模型推理 + 网络往返」的叠加等待；
  且本脚本聚焦调度层，不含 git worktree 写冲突并行的加速，是最保守的下界。
- 建议先跑零成本的 `bench_mcp_lazy`，再跑需要真金的 `bench_compact_recall` / `bench_parallel`。
