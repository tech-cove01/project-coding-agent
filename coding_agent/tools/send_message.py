from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from coding_agent.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from coding_agent.teams.manager import TeamManager

log = logging.getLogger(__name__)


class SendMessageParams(BaseModel):
    to: str
    message: str
    summary: str = ""
    message_type: str = "text"
    metadata: dict[str, Any] | None = None


VALID_MESSAGE_TYPES = {"text", "shutdown_request", "shutdown_response"}


class SendMessageTool(Tool):
    name = "SendMessage"
    description = (
        "Send a message to a teammate by name or agent ID. "
        "Use to='*' to broadcast to all teammates. "
        "For text messages, include a short summary (5-10 words). "
        "Supports structured types: shutdown_request, shutdown_response."
    )
    params_model = SendMessageParams
    category = "command"
    is_concurrency_safe = True


    def __init__(
        self,
        team_manager: TeamManager,
        team_name: str,
        from_agent_id: str,
        from_agent_name: str = "",
    ) -> None:
        self._team_manager = team_manager
        self._team_name = team_name
        self._from_agent_id = from_agent_id
        self._from_agent_name = from_agent_name


    async def execute(self, params: BaseModel) -> ToolResult:
        p: SendMessageParams = params  # type: ignore[assignment]

        if p.message_type not in VALID_MESSAGE_TYPES:
            return ToolResult(
                output=f"Invalid message_type '{p.message_type}'. Must be one of: {', '.join(sorted(VALID_MESSAGE_TYPES))}",
                is_error=True,
            )

        if p.message_type == "text" and not p.summary:
            return ToolResult(
                output="Text messages require a 'summary' field (5-10 words).",
                is_error=True,
            )

        from coding_agent.teams.mailbox import create_message
        from coding_agent.teams.models import LEAD_INBOX

        team = self._team_manager.get_team(self._team_name)
        if team is None:
            return ToolResult(output=f"Team '{self._team_name}' not found", is_error=True)

        mailbox = self._team_manager.get_mailbox(self._team_name)
        if mailbox is None:
            return ToolResult(output=f"Mailbox not found for team '{self._team_name}'", is_error=True)

        msg = create_message(
            from_agent=self._from_agent_name or self._from_agent_id,
            to_agent=p.to,
            content=p.message,
            summary=p.summary,
            message_type=p.message_type,
            metadata=p.metadata,
        )

        if p.to == "*":
            member_ids = [
                m.agent_id for m in team.members
                if m.agent_id != self._from_agent_id
            ]
            if team.lead_agent_id != self._from_agent_id:
                member_ids.append(LEAD_INBOX)
            mailbox.broadcast(member_ids, msg, exclude=self._from_agent_id)
            for aid in member_ids:
                self._team_manager.wake_pane(aid)
            return ToolResult(output=f"Message broadcast to {len(member_ids)} teammates.")

        # 收件人解析 + 投递 + 唤醒面板队友统一走 TeamManager，
        # 避免多处各写一套地址逻辑（lead 别名 / 名称表解析）
        if not self._team_manager.deliver(self._team_name, p.to, msg):
            return ToolResult(
                output=f"Cannot resolve recipient '{p.to}'. Check the name or agent ID.",
                is_error=True,
            )

        return ToolResult(output=f"Message sent to '{p.to}'.")
