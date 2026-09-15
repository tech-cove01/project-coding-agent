"""进程内队友的独立运行入口（**非生产主路径**）。

**收件箱键的不变量**：一个队友的收件箱文件名是 ``<agent_id>.json``，
**不是队友名字**。发送方经 ``TeamManager.resolve_recipient`` 解析收件人时，除
``"lead"`` 外一律走名称表拿到 ``agent_id``；因此这里读自己的邮箱、以及别人读它，
都必须用 ``agent.agent_id``。

历史问题：这里曾误用队友 ``name`` 当收件箱键 —— 读 ``<name>.json``，而发送方写
``<agent_id>.json``，**两个文件**，发给队友的消息永久滞留（与 lead 收件箱问题是
同一类"地址不一致"）。

**生产路径的队友长驻循环在 ``TaskManager._run_background``**，本模块的邮箱循环
用于独立/测试场景；两者是同一语义的两处实现，改动时请同步（否则会再次漂移）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from coding_agent.teams.mailbox import (
    Mailbox,
    MailboxMessage,
    create_message,
    partition_shutdown,
)
from coding_agent.teams.progress import TeammateProgress, random_verb

if TYPE_CHECKING:
    from coding_agent.agent import Agent
from coding_agent.conversation import ConversationManager
from coding_agent.teams.models import LEAD_INBOX, TeammateInfo

log = logging.getLogger(__name__)

# Idle 轮询间隔（秒），对齐 Go 的 IdlePollInterval = 500ms
IDLE_POLL_INTERVAL = 0.5

# shutdown 判定统一由 mailbox.is_shutdown_request / partition_shutdown 提供
# （同时识别 message_type 与 "[shutdown]" 内容前缀），此处不再重复实现，
# 避免两套语义再次漂移。

# lead 收件箱键，对齐 Go 的 LeadName；统一从 models 引入，避免地址不一致。
# 注意：这是「写给 lead」用的键；队友**自己的**收件箱键是 agent.agent_id。
LEAD_NAME = LEAD_INBOX


def _create_idle_notification(member_name: str, reason: str) -> MailboxMessage:
    """构造 idle 通知消息，发给 lead 表明 teammate 当前轮次已完成。"""
    return create_message(
        from_agent=member_name,
        to_agent=LEAD_NAME,
        content=f"[idle] {member_name} (reason: {reason})",
        summary="idle",
    )


def _inject_pending_messages(mailbox: Mailbox, inbox_key: str) -> str:
    """读取 teammate 邮箱中的未读消息，拼成 system-reminder 字符串。

    *inbox_key* 必须是 **agent_id**（收件箱文件名 = ``<agent_id>.json``），
    **不能传队友名字** —— 发送方是按 agent_id 投递的。
    """
    msgs = mailbox.consume(inbox_key)
    if not msgs:
        return ""
    parts = ["You have new messages:\n"]
    for m in msgs:
        parts.append(f"From {m.from_agent}: {m.content}\n")
    return "\n".join(parts)


async def _wait_for_next_prompt_or_shutdown(
    mailbox: Mailbox,
    inbox_key: str,
) -> tuple[str, bool]:
    """阻塞轮询邮箱，等到有新消息后返回 (prompt, is_shutdown)。

    *inbox_key* 必须是 **agent_id**（同 ``_inject_pending_messages``）。

    对齐 Go 的 waitForNextPromptOrShutdown：循环 sleep + 检查邮箱。
    收到 shutdown 消息返回 ("", True)；否则把普通消息拼成下一轮的 prompt。
    """
    while True:
        await asyncio.sleep(IDLE_POLL_INTERVAL)

        msgs = mailbox.consume(inbox_key)
        if not msgs:
            continue

        keep, has_shutdown = partition_shutdown(msgs)

        if has_shutdown:
            return "", True

        # 把剩余消息拼成下一轮的 user prompt
        if not keep:
            continue
        parts = ["You have new messages from your team:\n"]
        for m in keep:
            parts.append(f"From {m.from_agent}: {m.content}\n")
        return "\n".join(parts), False


class InProcessTeammateHandle:
    def __init__(
        self,
        agent: Agent,
        task: asyncio.Task[str],
        name: str,
        progress: TeammateProgress | None = None,
    ) -> None:
        self.agent = agent
        self.task = task
        self.name = name
        self.progress = progress


    @property
    def done(self) -> bool:
        return self.task.done()

    @property
    def result(self) -> str | None:
        if self.task.done():
            try:
                return self.task.result()
            except (asyncio.CancelledError, Exception):
                return None
        return None


    def cancel(self) -> None:
        if not self.task.done():
            self.task.cancel()


def spawn_inprocess_teammate(
    agent: Agent,
    prompt: str,
    name: str,
    conversation: ConversationManager | None = None,
    member: TeammateInfo | None = None,
    team_name: str = "",
    mailbox: Mailbox | None = None,
) -> InProcessTeammateHandle:

    # Create progress tracker and attach to member if provided
    progress = TeammateProgress(
        name=name,
        team_name=team_name,
        spinner_verb=random_verb(),
    )
    if member is not None:
        member.progress = progress

    def _on_event(event: dict[str, Any]) -> None:
        """Event callback wired into agent.run_to_completion."""
        event_type = event.get("type")
        if event_type == "tool_use":
            tool_name = event.get("toolName", "")
            args = event.get("args", {})
            progress.record_tool_use(tool_name, args)
        elif event_type == "usage":
            usage = event.get("usage", {})
            progress.record_tokens(
                usage.get("inputTokens", 0),
                usage.get("outputTokens", 0),
            )
        elif event_type == "stream_text":
            text = event.get("text")
            if text:
                with progress._lock:
                    progress.last_message = text

    async def _run() -> str:
        """teammate 主循环，对齐 Go 的 RunInProcessTeammate。

        有 mailbox 时进入长驻循环：执行 agent → 发 idle 通知 → 轮询等待新任务。
        没有 mailbox 时退化为单次执行（向后兼容）。
        """
        try:
            if conversation is not None:
                conv = conversation
            else:
                from coding_agent.conversation import ConversationManager as CM
                conv = CM()

            next_prompt = prompt
            idle_reason = "available"
            # 收件箱键 = agent_id（**不是队友名字**）：发送方按 agent_id 投递，
            # 用名字读会读到另一个空文件（见模块 docstring 的不变量说明）。
            inbox_key = agent.agent_id

            while True:
                # 注入本轮开始前邮箱里堆积的消息
                if mailbox is not None:
                    reminder = _inject_pending_messages(mailbox, inbox_key)
                    if reminder:
                        conv.add_system_reminder(reminder)

                # 执行一个完整的 agent turn
                if next_prompt:
                    result = await agent.run_to_completion(
                        next_prompt, conv, event_callback=_on_event,
                    )
                else:
                    result = await agent.run_to_completion(
                        "", conv, event_callback=_on_event,
                    )
                next_prompt = ""

                # 没有 mailbox 时退化为单次执行（向后兼容旧调用方式）
                if mailbox is None:
                    progress.status = "completed"
                    return result

                # 更新进度状态
                if idle_reason == "failed":
                    progress.status = "failed"
                else:
                    progress.status = "idle"

                # 通知 lead 本轮已完成
                mailbox.write(
                    LEAD_NAME,
                    _create_idle_notification(name, idle_reason),
                )
                idle_reason = "available"

                # 轮询等待 lead 下发新任务或 shutdown 指令
                new_prompt, shutdown = await _wait_for_next_prompt_or_shutdown(
                    mailbox, inbox_key,
                )
                if shutdown:
                    progress.status = "completed"
                    return result

                next_prompt = new_prompt

        except asyncio.CancelledError:
            progress.status = "stopped"
            raise
        except Exception:
            progress.status = "failed"
            raise

    task = asyncio.create_task(_run(), name=f"teammate-{name}")
    log.info("Spawned in-process teammate %s (verb=%s)", name, progress.spinner_verb)
    return InProcessTeammateHandle(agent=agent, task=task, name=name, progress=progress)
