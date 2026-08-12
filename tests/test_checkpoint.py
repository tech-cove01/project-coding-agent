"""任务级 Checkpoint（断点续跑）的保存/恢复测试。"""
from __future__ import annotations

from pathlib import Path

from coding_agent.checkpoint import CheckpointManager
from coding_agent.conversation import (
    ConversationManager,
    ToolResultBlock,
    ToolUseBlock,
)


def _make_conversation() -> ConversationManager:
    """构造一个含工具调用与结果的多轮对话，模拟真实任务执行到中途。"""
    conv = ConversationManager()
    conv.add_user_message("编写 fib.py 并验证")
    conv.add_assistant_message(
        "我来创建文件",
        tool_uses=[ToolUseBlock(tool_use_id="t1", tool_name="WriteFile", arguments={"file_path": "fib.py", "content": "..."})],
    )
    conv.add_tool_results_message([
        ToolResultBlock(tool_use_id="t1", content="成功写入 fib.py", is_error=False),
    ])
    conv.add_assistant_message(
        "现在运行验证",
        tool_uses=[ToolUseBlock(tool_use_id="t2", tool_name="Bash", arguments={"command": "python fib.py"})],
    )
    conv.add_tool_results_message([
        ToolResultBlock(tool_use_id="t2", content="55", is_error=False),
    ])
    return conv


class TestCheckpointSaveLoad:
    def test_save_creates_file(self, tmp_path: Path) -> None:
        mgr = CheckpointManager(str(tmp_path))
        conv = _make_conversation()
        path = mgr.save(conv)
        assert path is not None
        assert path.exists()
        # 有实质进展（tool_results）才会保存
        assert mgr.info().exists

    def test_save_skips_no_progress(self, tmp_path: Path) -> None:
        mgr = CheckpointManager(str(tmp_path))
        conv = ConversationManager()
        conv.add_user_message("只有一句话，没有工具调用")
        assert mgr.save(conv) is None  # 无工具产物不落盘
        assert not mgr.info().exists

    def test_load_rebuilds_conversation(self, tmp_path: Path) -> None:
        mgr = CheckpointManager(str(tmp_path))
        conv = _make_conversation()
        mgr.save(conv)

        restored = mgr.load()
        assert restored is not None
        # 恢复的消息条数与原始一致（含环境注入前的原始消息）
        assert len(restored.history) == len(conv.history)
        # 工具调用与结果被正确重建
        assert restored.history[1].tool_uses[0].tool_name == "WriteFile"
        assert restored.history[2].tool_results[0].content == "成功写入 fib.py"
        assert restored.history[3].tool_uses[0].tool_name == "Bash"
        assert restored.history[4].tool_results[0].content == "55"

    def test_load_from_explicit_path(self, tmp_path: Path) -> None:
        mgr = CheckpointManager(str(tmp_path))
        conv = _make_conversation()
        mgr.save(conv)

        other = CheckpointManager("")  # 无默认路径
        restored = other.load_from(tmp_path / "checkpoint.jsonl")
        assert restored is not None
        assert len(restored.history) > 0

    def test_load_missing_returns_none(self, tmp_path: Path) -> None:
        mgr = CheckpointManager(str(tmp_path))
        assert mgr.load() is None

    def test_clear_removes_file(self, tmp_path: Path) -> None:
        mgr = CheckpointManager(str(tmp_path))
        mgr.save(_make_conversation())
        assert mgr.info().exists
        mgr.clear()
        assert not mgr.info().exists
