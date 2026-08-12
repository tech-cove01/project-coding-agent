"""任务级 Checkpoint：支持 Agent 在长任务执行中"断点续跑"。

在 run_to_completion 的每一轮工具调用完成后，把当前完整对话历史落盘为
Checkpoint 文件；若进程在任务中途崩溃/中断，恢复时可从最近 Checkpoint 重放
对话上下文继续推进，而不是从头重新规划。

复用了 session 模块已有的消息序列化链路（SessionRecord <-> Message），
避免重复造轮子，保证与会话持久化格式一致。

用法：
    cp = CheckpointManager(session_dir)
    await cp.save(conversation)          # 每轮工具调用后保存
    conv = cp.load()                     # 恢复时重建对话
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from coding_agent.conversation import ConversationManager, Message
from coding_agent.memory.session import (
    SessionRecord,
    records_to_messages,
    validate_message_chain,
)

_CHECKPOINT_FILENAME = "checkpoint.jsonl"


@dataclass
class CheckpointInfo:
    """Checkpoint 元信息，用于日志与可观测。"""

    path: Path
    message_count: int
    exists: bool


class CheckpointManager:
    """管理任务级 Checkpoint 的保存与恢复。"""

    def __init__(self, session_dir: str | Path | None = None) -> None:
        self.session_dir = Path(session_dir) if session_dir else None
        self._path: Path | None = None
        if self.session_dir is not None:
            self._path = self.session_dir / _CHECKPOINT_FILENAME

    def set_path(self, path: str | Path) -> None:
        """显式指定 Checkpoint 文件路径（覆盖默认 session_dir 下的路径）。"""
        self._path = Path(path)

    @property
    def path(self) -> Path | None:
        return self._path

    def info(self) -> CheckpointInfo:
        if self._path is None:
            return CheckpointInfo(path=Path(""), message_count=0, exists=False)
        count = self._count_records()
        return CheckpointInfo(
            path=self._path,
            message_count=count,
            exists=self._path.exists(),
        )

    def _count_records(self) -> int:
        if self._path is None or not self._path.exists():
            return 0
        n = 0
        with open(self._path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    n += 1
        return n

    # ---------- 保存 ----------

    def save(self, conversation: ConversationManager) -> Path | None:
        """把当前对话历史序列化为 Checkpoint 文件。

        仅当确有工具调用产物（tool_results 存在）才落盘，避免对无实质进展的
        轮次写空 Checkpoint。
        """
        if self._path is None:
            return None
        has_progress = any(m.tool_results for m in conversation.history)
        if not has_progress:
            return None
        records: list[SessionRecord] = []
        for msg in conversation.history:
            records.extend(SessionRecord.from_message(msg))
        if not records:
            return None
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(rec.to_jsonl() + "\n")
        return self._path

    # ---------- 恢复 ----------

    def load_from(self, path: str | Path) -> ConversationManager | None:
        """从指定路径的 Checkpoint 恢复对话上下文，失败返回 None。"""
        return self._load_path(Path(path))

    def load(self) -> ConversationManager | None:
        """从默认 Checkpoint 文件恢复对话上下文，失败返回 None。"""
        if self._path is None:
            return None
        return self._load_path(self._path)

    def _load_path(self, path: Path) -> ConversationManager | None:
        if not path.exists():
            return None
        records: list[SessionRecord] = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = SessionRecord.from_jsonl(line)
                    if rec is not None:
                        records.append(rec)
            valid = validate_message_chain(records)
            records = records[:valid]
            messages = records_to_messages(records)
            conv = ConversationManager()
            conv.replace_history(messages)
            return conv
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def clear(self) -> None:
        """删除 Checkpoint 文件（任务完成后清理）。"""
        if self._path is not None and self._path.exists():
            try:
                self._path.unlink()
            except OSError:
                pass
