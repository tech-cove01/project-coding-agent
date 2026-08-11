"""Benchmark ②：MCP 工具延迟加载（Deferred Loading）节省的 token 比例。

设计目标：**纯确定性、零 API 调用**。它不连接任何真实 MCP server，而是用与
生产完全一致的 `ToolRegistry.get_all_schemas()` 计算逻辑，模拟「接入 N 个重型
MCP 工具」的场景，分别测量：

    - all      ：所有工具都被 discovered（等价于不做延迟加载，全量注入上下文）
    - deferred ：只有非 defer 的普通工具 + ToolSearch 在上下文中
                 （defer 工具通过 ToolSearch 按需拉取，默认不注入）

然后计算 schema token 节省比例 = 1 - deferred_tokens / all_tokens。

真实场景下 MCP 服务器自带大量工具，每个 schema 都较长。本脚本复用了
`test_tool_search.py` 里 `_HeavyParams`（10 个字段）级别的重型 schema，
并支持通过 --tools 参数控制工具数量，跑出「接入越多工具，节省越显著」的曲线。

运行：
    uv run python -m benchmarks.bench_mcp_lazy
    uv run python -m benchmarks.bench_mcp_lazy --tools 100
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import BaseModel

from coding_agent.tools import ToolRegistry
from coding_agent.tools.base import Tool, ToolResult


class _HeavyParams(BaseModel):
    """包含 10 个字段的重型参数 schema，模拟真实 MCP 工具的复杂入参。"""

    alpha: str = ""
    bravo: str = ""
    charlie: int = 0
    delta: float = 0.0
    echo: bool = False
    foxtrot: str = "default_foxtrot_value"
    golf: str = "default_golf_value"
    hotel: int = 42
    india: str = ""
    juliet: bool = True


class _NormalParams(BaseModel):
    text: str = ""


class _NormalTool(Tool):
    name = "NormalTool"
    description = "A normal, non-deferred tool"
    params_model = _NormalParams
    category = "read"
    should_defer = False

    async def execute(self, params: BaseModel) -> ToolResult:  # pragma: no cover
        return ToolResult(output="ok")


def _make_mcp_tool(index: int) -> Tool:
    """动态构造一个应被延迟加载的 MCP 工具（should_defer=True）。"""

    class _T(Tool):
        name = f"mcp_server_{index:03d}_tool"
        description = (
            f"External MCP tool number {index} that performs advanced operations: "
            f"querying external APIs, transforming large payloads, managing remote "
            f"resources, orchestrating multi-step workflows, and returning rich "
            f"structured results for downstream consumption in context {index}."
        )
        params_model = _HeavyParams
        category = "command"
        should_defer = True

        async def execute(self, params: BaseModel) -> ToolResult:  # pragma: no cover
            return ToolResult(output="ok")

    return _T()


def build_registry(n_mcp: int) -> ToolRegistry:
    """构造含 1 个普通工具 + n_mcp 个延迟 MCP 工具的 registry。"""
    reg = ToolRegistry()
    reg.register(_NormalTool())
    for i in range(n_mcp):
        reg.register(_make_mcp_tool(i))
    return reg


def measure(reg: ToolRegistry, n_mcp: int, protocol: str) -> dict[str, int | float]:
    """分别测量延迟加载前后 schema 的字节/近似 token 量。"""
    # 延迟加载生效时：defer 工具未 discovered，不进上下文。
    schemas_deferred = reg.get_all_schemas(protocol)
    size_deferred = len(json.dumps(schemas_deferred))

    # 全量注入：所有工具都被 discovered（等价的"不做延迟加载"基线）。
    for i in range(n_mcp):
        reg.mark_discovered(f"mcp_server_{i:03d}_tool")
    schemas_all = reg.get_all_schemas(protocol)
    size_all = len(json.dumps(schemas_all))

    # 用 3.5 chars/token 的启发式换算成近似 token，便于与上下文窗口对比。
    from benchmarks._common import token_estimate

    return {
        "n_mcp_tools": n_mcp,
        "deferred_chars": size_deferred,
        "all_chars": size_all,
        "deferred_tokens": token_estimate(str(schemas_deferred)),
        "all_tokens": token_estimate(str(schemas_all)),
        "savings_pct": round(100.0 * (1 - size_deferred / size_all), 2) if size_all else 0.0,
    }


def run(tools: int = 50, protocol: str = "anthropic") -> dict[str, int | float]:
    reg = build_registry(tools)
    return measure(reg, tools, protocol)


def main() -> None:
    parser = argparse.ArgumentParser(description="MCP 延迟加载 token 节省评测")
    parser.add_argument("--tools", type=int, default=50, help="模拟的 MCP 工具数量")
    parser.add_argument("--protocol", default="anthropic",
                        choices=["anthropic", "openai", "openai-compat"])
    args = parser.parse_args()

    result = run(tools=args.tools, protocol=args.protocol)

    print("=" * 60)
    print("Benchmark ②: MCP 延迟加载 token 节省")
    print("=" * 60)
    print(f"模拟 MCP 工具数量      : {result['n_mcp_tools']}")
    print(f"延迟加载下 schema 字符 : {result['deferred_chars']:,} "
          f"(≈{result['deferred_tokens']:,} tokens)")
    print(f"全量注入下 schema 字符 : {result['all_chars']:,} "
          f"(≈{result['all_tokens']:,} tokens)")
    print(f"token 节省比例        : {result['savings_pct']}%")
    print("-" * 60)
    print(f"解读：接入 {result['n_mcp_tools']} 个重型 MCP 工具时，延迟加载可避免把 "
          f"≈{result['all_tokens']:,} 个 schema token 注入每次请求。")
    print("提示：真实项目为降低首屏 latency 而禁用默认加载的 MCP 服务器，")
    print("     通过 ToolSearch 按需拉取，效果与此一致。")

    # 做一次越界断言，防止脚本退化后仍"通过"。
    if result["savings_pct"] < 50.0:
        print(f"\n[!] 警告：节省比例 {result['savings_pct']}% 偏低，"
              "请检查 ToolRegistry 是否真的在隐藏未 discovered 的 defer 工具。",
              file=sys.stderr)


if __name__ == "__main__":
    main()
