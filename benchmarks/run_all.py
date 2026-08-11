"""评测统一入口。

依次（或按需选择）运行 benchmarks/ 下的评测脚本：

    uv run python -m benchmarks.run_all            # 跑全部
    uv run python -m benchmarks.run_all --only mcp # 只跑 MCP 延迟加载

可用 --only 值：mcp | compact | parallel（可重复，或逗号分隔）。

各评测依赖说明：
    mcp      ：纯确定性，零 API，随时可跑。
    compact  ：需要真实 LLM（auto_compact 生成摘要），默认使用配置里的 provider。
    parallel ：需要真实 LLM，会真实调用多个 worker 的 API。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_BENCH_SCRIPTS = {
    "mcp": ("bench_mcp_lazy", "MCP 延迟加载 token 节省（纯确定性）"),
    "compact": ("bench_compact_recall", "上下文压缩召回率（需真实 LLM）"),
    "parallel": ("bench_parallel", "多 Agent 并行 vs 串行（需真实 LLM）"),
}


def _run_module(mod: str) -> int:
    print(f"\n{'='*72}\n运行: {mod}\n{'='*72}")
    proc = subprocess.run(
        [sys.executable, "-m", f"benchmarks.{mod}"],
        cwd=Path(__file__).resolve().parent.parent,
    )
    return proc.returncode


def main() -> None:
    parser = argparse.ArgumentParser(description="运行编码 agent 评测")
    parser.add_argument("--only", default=None,
                        help="只跑指定评测，逗号分隔或重复: mcp,compact,parallel")
    args = parser.parse_args()

    if args.only:
        keys = [k.strip() for k in args.only.replace(",", " ").split() if k.strip()]
        order = [k for k in keys if k in _BENCH_SCRIPTS]
    else:
        order = list(_BENCH_SCRIPTS.keys())

    if not order:
        print(f"未知评测项。可选: {', '.join(_BENCH_SCRIPTS)}", file=sys.stderr)
        sys.exit(1)

    failed: list[str] = []
    for key in order:
        mod, desc = _BENCH_SCRIPTS[key]
        print(f"\n[{desc}]")
        rc = _run_module(mod)
        if rc != 0:
            failed.append(key)

    print("\n" + "=" * 72)
    if failed:
        print(f"有评测未通过/异常: {', '.join(failed)}")
        sys.exit(1)
    print("全部评测完成。")

    # 统一入口的模块执行前提：benchmarks 是包。
    print("\n提示：可用 `uv run python -m benchmarks.bench_mcp_lazy` 单独运行任一评测。")


if __name__ == "__main__":
    main()
