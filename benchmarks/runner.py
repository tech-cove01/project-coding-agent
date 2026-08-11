"""自进化评测：端到端真实任务回归运行器。

核心闭环：
    改动代码 → 运行 runner → 对每个任务驱动真实 Agent 执行 → 验证器打分
    → 失败任务自动写入 benchmarks/regression.yaml（进回归集）
    → 下次运行自动包含回归任务，防止"修好 A 弄坏 B"、避免重复犯错。

用法：
    uv run python -m benchmarks.runner                # 跑全部任务
    uv run python -m benchmarks.runner --only <name>   # 只跑指定任务
    uv run python -m benchmarks.runner --max-tasks 5   # 最多跑 5 个

任务格式（benchmarks/tasks/*.yaml）：
    name: 任务名
    description: 让 agent 做的事（作为用户 prompt）
    difficulty: easy | medium | hard
    domain: coding | refactor | debugging | ...
    verify:
      - type: file_exists
        path: "{{workdir}}/fib.py"
      - type: file_contains
        path: "{{workdir}}/fib.py"
        pattern: "def fib"
      - type: command_succeeds
        command: "python {{workdir}}/fib.py"

需要真实 LLM（默认读取配置里第一个 provider）。失败任务自动追加进
regression.yaml，构成"每次改动自动跑分 + 失败案例进回归集"的自进化闭环。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from benchmarks._common import make_client, require_provider  # noqa: E402
from benchmarks._verifiers import verify  # noqa: E402
from coding_agent.agent import Agent  # noqa: E402
from coding_agent.conversation import ConversationManager  # noqa: E402
from coding_agent.permissions import (  # noqa: E402
    DangerousCommandDetector,
    PathSandbox,
    PermissionChecker,
    PermissionMode,
    RuleEngine,
)
from coding_agent.tools import create_default_registry  # noqa: E402

_TASKS_DIR = Path(__file__).resolve().parent / "tasks"
_REGRESSION_FILE = Path(__file__).resolve().parent / "regression.yaml"
_REPORT_FILE = Path(__file__).resolve().parent / "report.md"


# ---------------------------------------------------------------------------
# 任务加载
# ---------------------------------------------------------------------------

def _load_task(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_tasks(tasks_dir: Path = _TASKS_DIR) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    if tasks_dir.exists():
        for p in sorted(tasks_dir.glob("*.yaml")):
            t = _load_task(p)
            t["_file"] = str(p)
            tasks.append(t)
    # 回归集（历史失败案例）始终追加，保证不再重复犯错
    tasks.extend(load_regression_tasks())
    return tasks


def load_regression_tasks() -> list[dict[str, Any]]:
    if not _REGRESSION_FILE.exists():
        return []
    with open(_REGRESSION_FILE, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return [t for t in data.get("tasks", [])]


def add_to_regression(task: dict[str, Any], detail: str) -> None:
    """把失败任务写入 regression.yaml，构成自进化闭环的关键一步。"""
    reg = {"tasks": load_regression_tasks()}
    entry = {k: v for k, v in task.items() if not k.startswith("_")}
    entry.setdefault("last_failure", detail[:500])
    # 去重：已存在同名任务则不重复追加
    for existing in reg["tasks"]:
        if existing.get("name") == entry.get("name"):
            return
    reg["tasks"].append(entry)
    with open(_REGRESSION_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(reg, f, allow_unicode=True, sort_keys=False)


# ---------------------------------------------------------------------------
# 单任务执行
# ---------------------------------------------------------------------------

def _build_prompt(task: dict[str, Any]) -> str:
    desc = task.get("description", "").strip()
    # 任务工作在临时目录，给 agent 明确的工作位置和验收提示
    return (
        f"{desc}\n\n"
        "请在你的工作目录（当前目录）中完成上述任务。"
        "完成后请简要说明你做了什么。"
    )


async def _run_single_task(
    agent: Agent, task: dict[str, Any], workdir: Path
) -> tuple[bool, str]:
    """驱动 agent 完成单个任务并用验证器打分。"""
    prompt = _build_prompt(task)
    try:
        result_text = await agent.run_to_completion(prompt)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        return False, f"agent 执行异常: {e}"

    # 全部验证器通过才算 PASS
    verify_list = task.get("verify", [])
    if not verify_list:
        return bool(result_text.strip()), "无验证器，按输出非空判定"

    failures: list[str] = []
    for spec in verify_list:
        passed, detail = verify(spec, workdir, result_text or "")
        if not passed:
            failures.append(detail)
    if failures:
        return False, " | ".join(failures)
    return True, f"{len(verify_list)} 项验证全部通过"


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="自进化评测：端到端真实任务回归")
    parser.add_argument("--only", default=None, help="只跑指定任务名（逗号分隔）")
    parser.add_argument("--max-tasks", type=int, default=0, help="最多跑 N 个任务")
    parser.add_argument("--workdir", default=None, help="临时工作根目录（默认系统临时目录）")
    args = parser.parse_args()

    provider = require_provider()
    client = make_client(provider)
    if client is None:
        print("无法构造 LLM client（缺少 API key？），评测跳过。", file=sys.stderr)
        sys.exit(2)
    # 评测在受控临时目录中，放开权限让 agent 自由读写
    registry = create_default_registry()

    async def _run() -> int:
        tasks = load_tasks()
        if args.only:
            names = {n.strip() for n in args.only.replace(",", " ").split() if n.strip()}
            tasks = [t for t in tasks if t.get("name") in names]
        if args.max_tasks > 0:
            tasks = tasks[: args.max_tasks]

        print(f"加载 {len(tasks)} 个任务（含回归集）。\n")

        passed = failed = 0
        failures_report: list[tuple[str, str]] = []
        results: list[dict[str, Any]] = []

        for idx, task in enumerate(tasks, 1):
            name = task.get("name", "unnamed")
            print(f"[{idx}/{len(tasks)}] {name} ...", flush=True)
            workdir = Path(
                tempfile.mkdtemp(prefix=f"bench_{name[:20]}_", dir=args.workdir)
            )
            # 评测运行在受控临时目录，跳过权限确认（BYPASS），让 agent 自由读写
            checker = PermissionChecker(
                detector=DangerousCommandDetector(),
                sandbox=PathSandbox(str(workdir)),
                rule_engine=RuleEngine(
                    user_rules_path=Path.home() / ".coding_agent" / "permissions.yaml",
                    project_rules_path=workdir / ".coding_agent" / "permissions.yaml",
                    local_rules_path=workdir / ".coding_agent" / "permissions.local.yaml",
                ),
                mode=PermissionMode.BYPASS,
            )
            agent = Agent(
                client, registry, provider.protocol,
                work_dir=str(workdir), permission_checker=checker,
            )
            # 切换到任务工作目录，确保 agent 的相对路径（如 fib.py）写入评测目录而非进程 cwd
            prev_cwd = Path.cwd()
            os.chdir(workdir)
            try:
                ok, detail = await _run_single_task(agent, task, workdir)
            finally:
                os.chdir(prev_cwd)
                shutil.rmtree(workdir, ignore_errors=True)

            status = "PASS" if ok else "FAIL"
            print(f"    -> {status}: {detail}")
            results.append({
                "name": name,
                "difficulty": task.get("difficulty", "?"),
                "domain": task.get("domain", "?"),
                "status": status,
                "detail": detail,
            })
            if ok:
                passed += 1
            else:
                failed += 1
                failures_report.append((name, detail))
                add_to_regression(task, detail)

        _write_report(results, passed, failed)
        print(f"\n结果: {passed} 通过, {failed} 失败。报告见 {_REPORT_FILE.name}")
        print(f"失败任务已写入 {_REGRESSION_FILE.name}（下次自动进回归集）。")
        return 0 if failed == 0 else 1

    sys.exit(asyncio.run(_run()))


def _write_report(results: list[dict[str, Any]], passed: int, failed: int) -> None:
    lines = [
        "# 自进化评测报告",
        "",
        f"- 生成时间: 自动",
        f"- 通过: **{passed}** | 失败: **{failed}**",
        "",
        "| 任务 | 难度 | 领域 | 状态 | 详情 |",
        "|------|------|------|------|------|",
    ]
    for r in results:
        lines.append(
            f"| {r['name']} | {r['difficulty']} | {r['domain']} | "
            f"{r['status']} | {r['detail']} |"
        )
    _REPORT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
