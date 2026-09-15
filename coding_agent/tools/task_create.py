from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from coding_agent.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from coding_agent.teams.manager import TeamManager


class TaskCreateParams(BaseModel):
    title: str
    description: str = ""
    assignee: str = ""
    blocks: list[str] | None = None
    blocked_by: list[str] | None = None


class TaskCreateTool(Tool):
    name = "TaskCreate"
    description = (
        "Create a shared task in the team's task board. "
        "Supports dependency tracking with blocks/blocked_by fields."
    )
    params_model = TaskCreateParams
    category = "command"
    is_concurrency_safe = True


    def __init__(self, team_manager: TeamManager, team_name: str, agent_name: str = "") -> None:
        self._team_manager = team_manager
        self._team_name = team_name
        self._agent_name = agent_name


    async def execute(self, params: BaseModel) -> ToolResult:
        p: TaskCreateParams = params  # type: ignore[assignment]

        store = self._team_manager.get_task_store(self._team_name)
        if store is None:
            return ToolResult(output=f"Task store not found for team '{self._team_name}'", is_error=True)

        task = store.create(
            title=p.title,
            description=p.description,
            assignee=p.assignee,
            blocks=p.blocks,
            blocked_by=p.blocked_by,
            created_by=self._agent_name,
        )

        # 任务板是共享状态（pull），变更本身不会通知任何人：被指派人若不主动
        # 来查，就永远不知道自己被派了活。这里补一条邮箱通知（push），
        # 让「状态走任务板、信号走邮箱」两套机制真正缝合起来。
        notified = False
        if task.assignee and task.assignee != self._agent_name:
            notified = self._team_manager.notify_assignee(
                self._team_name,
                task.assignee,
                content=(
                    f"你被指派了任务 #{task.id}：{task.title}。"
                    f"可用 TaskGet {task.id} 查看详情，完成后用 TaskUpdate 标记状态。"
                ),
                summary=f"assigned task #{task.id}",
                from_agent=self._agent_name or "task-board",
            )

        output = (
            f"Task created:\n"
            f"  ID: {task.id}\n"
            f"  Title: {task.title}\n"
            f"  Status: {task.status}\n"
            f"  Assignee: {task.assignee or '(unassigned)'}"
        )
        if task.assignee and task.assignee != self._agent_name and not notified:
            output += f"\n  (提示：未能通知 {task.assignee}——该收件人未注册)"
        return ToolResult(output=output)
