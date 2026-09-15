from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from coding_agent.agent import Agent

log = logging.getLogger(__name__)

# 队友空闲期的轮询间隔（秒）。空闲期**不设固定时长上限**——队友是「长驻」的，
# 一直待命到团队被删除或收到 shutdown 请求（见 _run_background）。
_IDLE_POLL_SECONDS = 1.0


@dataclass
class ProgressInfo:
    tool_call_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    last_activity: str = ""


@dataclass
class BackgroundTask:
    id: str
    name: str
    agent: Agent
    task: str
    status: str = "running"
    result: str = ""
    start_time: float = field(default_factory=time.monotonic)
    end_time: float | None = None
    cancel: Callable[[], None] | None = None
    progress: ProgressInfo = field(default_factory=ProgressInfo)


class TaskManager:


    def __init__(self) -> None:
        self._tasks: dict[str, BackgroundTask] = {}
        self._notify_queue: asyncio.Queue[str] = asyncio.Queue()
        self._async_tasks: dict[str, asyncio.Task[None]] = {}
        # agent_id -> task_id：用于按 agent 定位任务（例如 TaskStop 停止某个 worker）
        self._agent_tasks: dict[str, str] = {}


    def launch(
        self,
        agent: Agent,
        task: str,
        name: str = "",
        fork_conversation: Any = None,
    ) -> str:
        task_id = uuid.uuid4().hex[:8]
        bg = BackgroundTask(
            id=task_id,
            name=name or task_id,
            agent=agent,
            task=task,
        )
        self._tasks[task_id] = bg

        agent_id = getattr(agent, "agent_id", "")
        if agent_id:
            self._agent_tasks[agent_id] = task_id

        async_task = asyncio.create_task(
            self._run_background(task_id, fork_conversation)
        )
        self._async_tasks[task_id] = async_task

        bg.cancel = async_task.cancel
        return task_id


    async def _run_background(
        self, task_id: str, fork_conversation: Any = None
    ) -> None:
        bg = self._tasks.get(task_id)
        if bg is None:
            return

        try:
            if fork_conversation is not None:
                result = await bg.agent.run_to_completion("", fork_conversation)
            else:
                result = await bg.agent.run_to_completion(bg.task)
            bg.result = result
            bg.status = "completed"

            if bg.agent.team_name and bg.agent._team_manager:
                team_manager = bg.agent._team_manager
                mailbox = team_manager.get_mailbox(bg.agent.team_name)
                if mailbox:
                    from coding_agent.teams.mailbox import partition_shutdown

                    # 标记空闲 + 通知 lead：统一走 TeamManager.on_teammate_completed。
                    # 放在「生产方」（这里）而不是各入口的通知聚合里，保证 TUI / CLI
                    # 等所有入口行为一致——否则总有人漏掉，队友永远不算空闲，
                    # TeamDelete 就会一直报「有活跃成员」而删不掉团队。
                    team_manager.on_teammate_completed(bg.agent.agent_id)

                    # 队友是「长驻」的：一直待命，直到团队被删除或收到 shutdown。
                    # 刻意不设固定时长上限——一旦窗口过期，发给它的消息会静默滞留
                    # 在邮箱里，而 SendMessage 仍返回「已发送」（假成功）。
                    while True:
                        await asyncio.sleep(_IDLE_POLL_SECONDS)

                        # 团队被删除 → 队友随之退出（避免永久驻留）
                        if team_manager.get_team(bg.agent.team_name) is None:
                            break

                        msgs = mailbox.consume(bg.agent.agent_id)
                        if not msgs:
                            continue
                        keep, has_shutdown = partition_shutdown(msgs)
                        if has_shutdown:
                            # lead 发出关闭请求：停止待命，后台任务就此结束。
                            # 若不处理，shutdown_request 会被当成新一轮 prompt，
                            # 队友反而被唤醒继续干活。
                            bg.status = "stopped"
                            break
                        if not keep:
                            continue
                        prompt = "\n\n".join(
                            f"[Message from {m.from_agent}] {m.content}" for m in keep
                        )
                        result = await bg.agent.run_to_completion(prompt)
                        bg.result = result
                        # 每完成一轮都重新标记空闲并通知 lead
                        team_manager.on_teammate_completed(bg.agent.agent_id)

        except asyncio.CancelledError:
            bg.status = "cancelled"
            bg.result = "Task was cancelled"
        except Exception as e:
            log.error("Background task %s failed: %s", task_id, e)
            bg.status = "failed"
            bg.result = f"Error: {e}"
        finally:
            bg.end_time = time.monotonic()
            bg.progress.input_tokens = bg.agent.total_input_tokens
            bg.progress.output_tokens = bg.agent.total_output_tokens
            self._async_tasks.pop(task_id, None)
            agent_id = getattr(bg.agent, "agent_id", "")
            if self._agent_tasks.get(agent_id) == task_id:
                self._agent_tasks.pop(agent_id, None)
            await self._notify_queue.put(task_id)


    def cancel_by_agent(self, agent_id: str) -> bool:
        """按 agent_id 停止其后台任务（供 TaskStop 工具使用）。

        返回 False 表示该 agent 没有正在运行的后台任务。
        """
        task_id = self._agent_tasks.get(agent_id)
        if task_id is None:
            return False
        return self.cancel(task_id)


    def adopt_running(
        self,
        agent: Agent,
        task_description: str,
        partial_result: str = "",
        name: str = "",
    ) -> str:
        task_id = uuid.uuid4().hex[:8]
        bg = BackgroundTask(
            id=task_id,
            name=name or task_id,
            agent=agent,
            task=task_description,
            result=partial_result,
        )
        self._tasks[task_id] = bg

        async_task = asyncio.create_task(self._continue_background(task_id))
        self._async_tasks[task_id] = async_task
        bg.cancel = async_task.cancel
        return task_id


    async def _continue_background(self, task_id: str) -> None:
        bg = self._tasks.get(task_id)
        if bg is None:
            return

        try:
            result = await bg.agent.run_to_completion(bg.task)
            bg.result = (bg.result + "\n" + result).strip() if bg.result else result
            bg.status = "completed"
        except asyncio.CancelledError:
            bg.status = "cancelled"
        except Exception as e:
            log.error("Background task %s failed: %s", task_id, e)
            bg.status = "failed"
            bg.result = f"Error: {e}"
        finally:
            bg.end_time = time.monotonic()
            bg.progress.input_tokens = bg.agent.total_input_tokens
            bg.progress.output_tokens = bg.agent.total_output_tokens
            self._async_tasks.pop(task_id, None)
            await self._notify_queue.put(task_id)

    def get(self, task_id: str) -> BackgroundTask | None:
        return self._tasks.get(task_id)

    def list_tasks(self) -> list[BackgroundTask]:
        return list(self._tasks.values())

    def cancel(self, task_id: str) -> bool:
        bg = self._tasks.get(task_id)
        if bg is None or bg.status != "running":
            return False
        async_task = self._async_tasks.get(task_id)
        if async_task and not async_task.done():
            async_task.cancel()
            return True
        return False

    def poll_completed(self) -> list[BackgroundTask]:
        completed: list[BackgroundTask] = []
        while not self._notify_queue.empty():
            try:
                task_id = self._notify_queue.get_nowait()
                bg = self._tasks.get(task_id)
                if bg is not None:
                    completed.append(bg)
            except asyncio.QueueEmpty:
                break
        return completed
