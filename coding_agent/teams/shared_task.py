from __future__ import annotations

import json
import os
import random
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class SharedTask:
    id: str
    title: str
    description: str = ""
    status: str = "pending"  # pending | in_progress | completed | blocked
    assignee: str = ""
    blocks: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    created_by: str = ""


    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SharedTask:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class TaskUpdateResult:
    """``SharedTaskStore.update()`` 的结果：变更后的任务 + 本次的真实变化。

    ``assignee_changed`` 必须在**锁内**判定后带出：调用方（``TaskUpdateTool``）
    要靠它决定是否推送指派通知。若放在锁外"先读再写"来判断，会引入 TOCTOU
    窗口（读到旧值 → 别人改完 → 我们基于陈旧判断多发/漏发通知）。
    """

    task: SharedTask
    assignee_changed: bool = False


# 陈旧锁判定阈值（秒），与 Mailbox._with_lock 保持一致
_STALE_LOCK_SECONDS = 10

# 锁重试次数：10 次 × (5–100ms 随机退避)，最坏约 1 秒
_LOCK_RETRIES = 10


class SharedTaskStore:
    """团队共享任务板（blackboard / 共享状态）。

    任务板是**共享状态**（pull 语义）：多个 agent、甚至多个进程会并发读写
    同一个 ``tasks.json``。它不像 Mailbox 那样"一人一个文件"，因此必须
    自己保证一致性：

    * **写**（create / update / init_empty）：加文件锁 → 重新 ``_load()`` →
      应用变更 → 原子替换落盘。
      拿到锁后**必须重新加载**，否则会用陈旧缓存覆盖其他进程刚写入的任务
      （会丢数据）。
    * **读**（get / list_tasks）：不加锁。因为写入是原子的
      （临时文件 + ``os.replace``），读者要么看到旧版本、要么看到新版本，
      不会读到半写内容。
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock_path = self._path.with_name(self._path.name + ".lock")
        self._next_id = 1
        self._tasks: dict[str, SharedTask] = {}
        self._load()

    # ---------- 持久化 ----------

    def _load(self) -> None:
        if not self._path.exists():
            self._next_id = 1
            self._tasks = {}
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        self._next_id = data.get("next_id", 1)
        self._tasks = {}
        for t in data.get("tasks", []):
            task = SharedTask.from_dict(t)
            self._tasks[task.id] = task

    def _save(self) -> None:
        """原子落盘：先写临时文件，再 os.replace 覆盖，避免读者看到半写内容。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "next_id": self._next_id,
            "tasks": [t.to_dict() for t in self._tasks.values()],
        }
        tmp = self._path.with_name(f"{self._path.name}.tmp{os.getpid()}")
        tmp.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(tmp, self._path)

    # ---------- 文件锁 ----------

    def _with_lock(self, fn: Callable[[], Any]) -> Any:
        """在文件锁保护下执行一次「重新加载 → 变更 → 落盘」。

        与 ``Mailbox._with_lock`` 同款策略：``O_CREAT | O_EXCL`` 原子创建锁
        文件，冲突则随机退避重试，陈旧锁（>10s）自动清理。
        拿不到锁时**抛出异常而不是静默继续**——宁可失败，也不丢数据。
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self._lock_path
        acquired = False

        for _ in range(_LOCK_RETRIES):
            try:
                fd = os.open(
                    str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644
                )
                os.close(fd)
                acquired = True
                break
            except FileExistsError:
                # 锁已存在：若已陈旧则清理，否则随机退避后重试
                try:
                    if time.time() - lock_file.stat().st_mtime > _STALE_LOCK_SECONDS:
                        lock_file.unlink(missing_ok=True)
                except OSError:
                    pass
                time.sleep((5 + random.randint(0, 95)) / 1000)
            except OSError:
                break

        if not acquired:
            raise TimeoutError(
                f"无法获取任务板锁（{lock_file}），可能有其他进程正在写入，请重试"
            )

        try:
            # 关键：拿到锁后重新加载，避免用陈旧缓存覆盖别人刚写入的任务
            self._load()
            result = fn()
            self._save()
            return result
        finally:
            lock_file.unlink(missing_ok=True)

    # ---------- 公开 API ----------

    def create(
        self,
        title: str,
        description: str = "",
        assignee: str = "",
        blocks: list[str] | None = None,
        blocked_by: list[str] | None = None,
        created_by: str = "",
    ) -> SharedTask:
        def _apply() -> SharedTask:
            task_id = str(self._next_id)
            self._next_id += 1
            task = SharedTask(
                id=task_id,
                title=title,
                description=description,
                assignee=assignee,
                blocks=blocks or [],
                blocked_by=blocked_by or [],
                created_by=created_by,
            )
            self._tasks[task_id] = task
            return task

        return self._with_lock(_apply)

    def get(self, task_id: str) -> SharedTask | None:
        self._load()
        return self._tasks.get(task_id)


    def list_tasks(
        self,
        status: str | None = None,
        assignee: str | None = None,
    ) -> list[SharedTask]:
        self._load()
        result = list(self._tasks.values())
        if status:
            result = [t for t in result if t.status == status]
        if assignee:
            result = [t for t in result if t.assignee == assignee]
        return result


    def update(
        self,
        task_id: str,
        status: str | None = None,
        assignee: str | None = None,
        description: str | None = None,
        add_blocks: list[str] | None = None,
        add_blocked_by: list[str] | None = None,
    ) -> TaskUpdateResult | None:
        def _apply() -> TaskUpdateResult | None:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            # 在锁内判定「assignee 是否真的变了」：调用方靠它决定是否推送
            # 指派通知，放到锁外判断会有 TOCTOU 窗口。
            assignee_changed = False
            if status is not None:
                task.status = status
            if assignee is not None:
                assignee_changed = task.assignee != assignee
                task.assignee = assignee
            if description is not None:
                task.description = description
            if add_blocks:
                for bid in add_blocks:
                    if bid not in task.blocks:
                        task.blocks.append(bid)
            if add_blocked_by:
                for bid in add_blocked_by:
                    if bid not in task.blocked_by:
                        task.blocked_by.append(bid)
            return TaskUpdateResult(task=task, assignee_changed=assignee_changed)

        return self._with_lock(_apply)

    def init_empty(self) -> None:
        def _apply() -> None:
            self._tasks.clear()
            self._next_id = 1
            return None

        self._with_lock(_apply)
