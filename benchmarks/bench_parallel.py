"""Benchmark ④：多 Agent 并行（in-process teammate）vs 串行 的加速比。

背景：项目声称多 Agent 并行 + worktree 隔离能把同类任务整体提速到 60% 的水平
（即并行耗时约为串行的 0.4 倍）。本脚本为该声明提供实测。

原理：
1. 准备一组**相互独立、只读**的研究任务（如「总结某文件的首行结构」「统计某目录
   下 .py 文件数量」「查找某个符号的定义位置」），任务间无文件写入冲突。
2. **并行模式**：用 `spawn_inprocess_teammate` 把每个任务交给一个独立 worker，
   它们共享同一个 event loop 并发执行，互相不共享上下文。
3. **串行模式**：同一批任务，逐个 `run_to_completion`。
4. 加速比 = 串行耗时 / 并行耗时。

说明：
- 真实多 Agent 的加速还来自 git worktree 文件级隔离，允许写冲突并行。本脚本聚焦
  「同进程 in-process 后端的并行调度」这一层，是最保守、最可复现的加速比下界。
- 需要真实 LLM + 已配置 provider。用 --tasks 控制任务数，--workers 控制并发度。
- 用 --target-dir 指定要研究的目标目录（默认是本项目根）。

运行：
    uv run python -m benchmarks.bench_parallel
    uv run python -m benchmarks.bench_parallel --workers 4 --tasks 4
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
import time
from pathlib import Path

if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmarks._common import make_client, require_provider

from coding_agent.agent import Agent
from coding_agent.permissions import DangerousCommandDetector, PathSandbox, PermissionChecker, RuleEngine, PermissionMode
from coding_agent.tools import create_default_registry
from coding_agent.conversation import ConversationManager
from coding_agent.teams.spawn_inprocess import spawn_inprocess_teammate


def _make_task_specs(target_dir: Path, n: int) -> list[str]:
    """构造 n 条相互独立的只读研究任务。

    每条任务针对 target_dir 下不同的文件/维度，确保互不冲突、可并行。
    任务刻意设计为「有明确终点、不需要写文件」的自足式提问，让 worker
    一轮即可完成，从而把测量聚焦在「并行调度的开销」而非「任务本身复杂度」。
    """
    py_files = sorted(target_dir.rglob("*.py"))[: max(1, n)]
    specs: list[str] = []
    for i in range(n):
        if i < len(py_files):
            f = py_files[i]
            specs.append(
                f"只读研究任务：请读取文件 {f}，总结它的第一个顶层定义（类或函数）"
                f"的名称和用途，用一到两句话回答即可。不要修改任何文件，不要运行命令。"
            )
        else:
            specs.append(
                f"只读研究任务：请统计项目根目录下 .py 文件的总体数量（可用只读工具），"
                f"用一句话回答。不要修改任何文件。"
            )
    return specs


def _build_agent(provider, work_dir: str, temp_work: Path) -> Agent:
    """构造一个独立脚本环境的 Agent（复用生产 registry + permission checker）。"""
    from coding_agent.config import MCPServerConfig
    from coding_agent.tools.impl.tool_search import ToolSearchTool

    registry = create_default_registry(file_cache=None)
    # 注册 ToolSearch，worker 也能按需拉取延迟工具（与生产一致）。
    search = ToolSearchTool(registry, protocol=provider.protocol)
    registry.register(search)

    home = Path.home()
    checker = PermissionChecker(
        detector=DangerousCommandDetector(),
        sandbox=PathSandbox(work_dir),
        rule_engine=RuleEngine(
            user_rules_path=home / ".coding_agent" / "permissions.yaml",
            project_rules_path=Path(work_dir) / ".coding_agent" / "permissions.yaml",
            local_rules_path=Path(work_dir) / ".coding_agent" / "permissions.local.yaml",
        ),
        mode=PermissionMode.DEFAULT,
    )

    client = make_client(provider)
    if client is None:
        raise RuntimeError("无可用 client（缺 API key）")

    agent = Agent(
        client=client,
        registry=registry,
        protocol=provider.protocol,
        work_dir=str(temp_work),
        permission_checker=checker,
        context_window=provider.get_context_window(),
    )
    return agent


async def _run_parallel(provider, tasks: list[str], workers: int, work_dir: str,
                        temp_work: Path) -> tuple[float, list[str]]:
    """并行模式：spawn 多个 in-process worker，共享 event loop 并发执行。"""
    # 为每个任务分配一个独立 Agent（上下文隔离，与生产多 worker 一致）。
    agents = [_build_agent(provider, work_dir, temp_work) for _ in tasks]
    handles = [
        spawn_inprocess_teammate(agent, prompt, name=f"bench-worker-{i}")
        for i, (agent, prompt) in enumerate(zip(agents, tasks))
    ]
    start = time.perf_counter()
    results = await asyncio.gather(*(h.task for h in handles), return_exceptions=True)
    elapsed = time.perf_counter() - start
    outputs = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            outputs.append(f"task-{i}: ERROR {r}")
        else:
            outputs.append(str(r) if r else f"task-{i}: <no output>")
    return elapsed, outputs


async def _run_serial(provider, tasks: list[str], work_dir: str,
                      temp_work: Path) -> tuple[float, list[str]]:
    """串行模式：同一批任务，每个用独立 Agent 逐个执行。"""
    outputs: list[str] = []
    start = time.perf_counter()
    for i, prompt in enumerate(tasks):
        agent = _build_agent(provider, work_dir, temp_work)
        conv = ConversationManager()
        try:
            out = await agent.run_to_completion(prompt, conv)
            outputs.append(str(out) if out else f"task-{i}: <no output>")
        except Exception as e:  # noqa: BLE001
            outputs.append(f"task-{i}: ERROR {e}")
    elapsed = time.perf_counter() - start
    return elapsed, outputs


async def run(workers: int = 4, tasks: int = 4, target_dir: str | None = None) -> dict:
    provider = require_provider()
    target = Path(target_dir or Path(__file__).resolve().parent.parent)
    work_dir = str(target)

    specs = _make_task_specs(target, tasks)

    with tempfile.TemporaryDirectory(prefix="bench-parallel-") as tmp:
        temp_work = Path(tmp)

        parallel_elapsed, parallel_out = await _run_parallel(
            provider, specs, workers, work_dir, temp_work)
        serial_elapsed, serial_out = await _run_serial(
            provider, specs, work_dir, temp_work)

    speedup = serial_elapsed / parallel_elapsed if parallel_elapsed > 0 else 0.0
    return {
        "tasks": tasks,
        "workers": workers,
        "serial_seconds": round(serial_elapsed, 3),
        "parallel_seconds": round(parallel_elapsed, 3),
        "speedup_x": round(speedup, 2),
        "parallel_ok": sum(1 for o in parallel_out if "ERROR" not in o),
        "serial_ok": sum(1 for o in serial_out if "ERROR" not in o),
        "parallel_out": parallel_out,
        "serial_out": serial_out,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="多 Agent 并行 vs 串行加速比评测")
    parser.add_argument("--workers", type=int, default=4, help="并行 worker 数")
    parser.add_argument("--tasks", type=int, default=4, help="任务数")
    parser.add_argument("--target-dir", default=None, help="要研究的目标目录")
    args = parser.parse_args()

    result = asyncio.run(run(
        workers=args.workers, tasks=args.tasks, target_dir=args.target_dir))

    print("=" * 60)
    print("Benchmark ④: 多 Agent 并行 vs 串行 加速比")
    print("=" * 60)
    print(f"任务数        : {result['tasks']}")
    print(f"并行 worker 数: {result['workers']}")
    print(f"串行耗时      : {result['serial_seconds']}s")
    print(f"并行耗时      : {result['parallel_seconds']}s")
    print(f"加速比        : {result['speedup_x']}x")
    print(f"并行成功      : {result['parallel_ok']}/{result['tasks']}")
    print(f"串行成功      : {result['serial_ok']}/{result['tasks']}")
    print("-" * 60)
    print("各任务并行输出摘要：")
    for i, o in enumerate(result["parallel_out"]):
        snippet = o[:120].replace("\n", " ")
        print(f"  task-{i}: {snippet}")

    if result["parallel_ok"] < result["tasks"]:
        print(f"\n[!] 部分并行任务失败（成功 {result['parallel_ok']}/{result['tasks']}），"
              "加速比可能失真，请检查 API/配置。", file=sys.stderr)


if __name__ == "__main__":
    main()
