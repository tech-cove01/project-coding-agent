from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from coding_agent.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from coding_agent.agents.task_manager import TaskManager
    from coding_agent.teams.manager import TeamManager


class TaskStopParams(BaseModel):
    target: str


class TaskStopTool(Tool):
    """停止一个正在运行的 worker。

    对应 coordinator 系统提示词里承诺的 TaskStop：协调者可以用它把跑偏的
    worker 停下来（停下来的 worker 仍留在团队里，之后可以用 SendMessage 继续）。
    """

    name = "TaskStop"
    description = (
        "Stop a running worker by name or agent ID. "
        "The worker stays in the team and can be continued later with SendMessage."
    )
    params_model = TaskStopParams
    category = "command"
    is_concurrency_safe = False


    def __init__(
        self,
        task_manager: TaskManager,
        team_manager: TeamManager | None = None,
    ) -> None:
        self._task_manager = task_manager
        self._team_manager = team_manager


    async def execute(self, params: BaseModel) -> ToolResult:
        p: TaskStopParams = params  # type: ignore[assignment]

        # 目标可能是「队友名字」或「agent_id」：先经名称表解析成真实 agent_id，
        # 解析不出来时退回原样尝试（也许调用方直接给了 id）。
        resolved: str | None = None
        if self._team_manager is not None:
            resolved = self._team_manager.resolve_agent_id(p.target)

        for candidate in (resolved, p.target):
            if candidate and self._task_manager.cancel_by_agent(candidate):
                return ToolResult(output=f"Worker '{p.target}' stopped.")

        return ToolResult(
            output=(
                f"No running worker found for '{p.target}'. "
                f"It may already be idle, finished, or stopped."
            )
        )
