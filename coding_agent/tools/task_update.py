from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from coding_agent.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from coding_agent.teams.manager import TeamManager


class TaskUpdateParams(BaseModel):
    task_id: str
    status: str | None = None
    assignee: str | None = None
    description: str | None = None
    add_blocks: list[str] | None = None
    add_blocked_by: list[str] | None = None


VALID_STATUSES = {"pending", "in_progress", "completed", "blocked"}


class TaskUpdateTool(Tool):
    name = "TaskUpdate"
    description = (
        "Update a shared task's status, assignee, description, or dependencies. "
        "Use add_blocks/add_blocked_by to add dependency relations."
    )
    params_model = TaskUpdateParams
    category = "command"
    is_concurrency_safe = True


    def __init__(
        self,
        team_manager: TeamManager,
        team_name: str,
        agent_name: str = "",
    ) -> None:
        self._team_manager = team_manager
        self._team_name = team_name
        # 用于"指派给自己不发通知"——与 TaskCreateTool 保持同一套排除规则
        self._agent_name = agent_name


    async def execute(self, params: BaseModel) -> ToolResult:
        p: TaskUpdateParams = params  # type: ignore[assignment]

        if p.status and p.status not in VALID_STATUSES:
            return ToolResult(
                output=f"Invalid status '{p.status}'. Must be one of: {', '.join(sorted(VALID_STATUSES))}",
                is_error=True,
            )

        store = self._team_manager.get_task_store(self._team_name)
        if store is None:
            return ToolResult(output=f"Task store not found for team '{self._team_name}'", is_error=True)

        result = store.update(
            task_id=p.task_id,
            status=p.status,
            assignee=p.assignee,
            description=p.description,
            add_blocks=p.add_blocks,
            add_blocked_by=p.add_blocked_by,
        )

        if result is None:
            return ToolResult(output=f"Task '{p.task_id}' not found", is_error=True)

        task = result.task

        # assignee 变更 → push 通知新负责人（任务板本身不产生任何通知）。
        #
        # 两个必须同时满足的条件，缺一都会产生噪音：
        #   1) ``result.assignee_changed`` —— 只有**真的变了**才通知。此前只判断
        #      ``p.assignee`` 非空，于是模型"改状态时顺手把同一个 assignee 再写
        #      一遍"就会重复通知。
        #   2) ``task.assignee != self._agent_name`` —— 不通知自己，与
        #      ``TaskCreateTool`` 的排除规则保持一致。
        should_notify = (
            result.assignee_changed
            and bool(task.assignee)
            and task.assignee != self._agent_name
        )
        notified = False
        if should_notify:
            notified = self._team_manager.notify_assignee(
                self._team_name,
                task.assignee,
                content=(
                    f"你被指派了任务 #{task.id}：{task.title}。"
                    f"可用 TaskGet {task.id} 查看详情，完成后用 TaskUpdate 标记状态。"
                ),
                summary=f"assigned task #{task.id}",
            )

        changes: list[str] = []
        if p.status:
            changes.append(f"status → {p.status}")
        if p.assignee is not None:
            changes.append(f"assignee → {p.assignee or '(unassigned)'}")
        if p.description is not None:
            changes.append("description updated")
        if p.add_blocks:
            changes.append(f"blocks += {', '.join(p.add_blocks)}")
        if p.add_blocked_by:
            changes.append(f"blocked_by += {', '.join(p.add_blocked_by)}")

        output = f"Task {task.id} updated: {'; '.join(changes) if changes else 'no changes'}"
        if should_notify and not notified:
            output += f"\n(提示：未能通知 {task.assignee}——该收件人未注册)"
        return ToolResult(output=output)
