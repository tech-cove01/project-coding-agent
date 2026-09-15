"""Agent Team（智能体团队）系统的测试（第 14 章）。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coding_agent.teams.models  import (
    LEAD_INBOX,
    AgentTeam,
    BackendType,
    TeammateInfo,
    resolve_team_dir,
    unique_team_name,
)
from coding_agent.teams.shared_task  import SharedTask, SharedTaskStore
from coding_agent.teams.mailbox  import Mailbox, MailboxMessage, create_message
from coding_agent.teams.registry  import AgentNameRegistry
from coding_agent.teams.backend_detect  import BackendDetectionError, detect_backend, detect_pane_backend
from coding_agent.teams.coordinator  import (
    get_coordinator_system_prompt,
    get_coordinator_user_context,
    is_coordinator_mode,
    match_session_mode,
)
from coding_agent.agents.tool_filter  import (
    COORDINATOR_MODE_ALLOWED_TOOLS,
    IN_PROCESS_TEAMMATE_ALLOWED_TOOLS,
    TEAMMATE_COORDINATION_TOOLS,
    build_teammate_tools,
    apply_coordinator_filter,
)
from coding_agent.tools  import ToolRegistry
from coding_agent.tools.base  import Tool, ToolResult

# =====================================================================
# 辅助工具
# =====================================================================

class DummyTool(Tool):
    params_model = MagicMock

    def __init__(self, name: str, category: str = "read"):
        self.name = name
        self.description = f"Dummy {name}"
        self.category = category
        self.is_concurrency_safe = True
        self.is_system_tool = False

    def get_schema(self):
        return {"name": self.name, "description": self.description, "input_schema": {}}

    async def execute(self, params):
        return ToolResult(output=f"{self.name} executed")

def make_registry(*tool_names: str) -> ToolRegistry:
    reg = ToolRegistry()
    for name in tool_names:
        reg.register(DummyTool(name))
    return reg

@pytest.fixture(autouse=True)
def _reset_registry():
    AgentNameRegistry.reset()
    yield
    AgentNameRegistry.reset()

@pytest.fixture
def tmp_dir():
    d = tempfile.mkdtemp()
    yield d
    shutil.rmtree(d, ignore_errors=True)

# =====================================================================
# 1. AgentTeam / TeammateInfo
# =====================================================================

class TestModels:
    def test_teammate_info_roundtrip(self):
        info = TeammateInfo(
            name="alice",
            agent_id="abc123",
            agent_type="worker",
            model="sonnet",
            worktree_path="/tmp/wt",
            backend_type="tmux",
            is_active=True,
        )
        d = info.to_dict()
        restored = TeammateInfo.from_dict(d)
        assert restored.name == "alice"
        assert restored.agent_id == "abc123"
        assert restored.is_active is True

    def test_agent_team_save_load(self, tmp_dir):
        config_path = str(Path(tmp_dir) / "team" / "config.json")
        team = AgentTeam(
            name="test-team",
            lead_agent_id="lead-001",
            config_path=config_path,
            description="Test team",
        )
        team.add_member(TeammateInfo(
            name="alice", agent_id="a1", agent_type="worker",
            model="sonnet", worktree_path="/tmp/wt1", backend_type="tmux",
        ))
        team.save()

        loaded = AgentTeam.load(config_path)
        assert loaded.name == "test-team"
        assert loaded.lead_agent_id == "lead-001"
        assert len(loaded.members) == 1
        assert loaded.members[0].name == "alice"

    def test_get_member(self):
        team = AgentTeam(name="t", lead_agent_id="l")
        team.add_member(TeammateInfo(
            name="bob", agent_id="b1", agent_type="w",
            model="", worktree_path="", backend_type="in-process",
        ))
        assert team.get_member("bob") is not None
        assert team.get_member("b1") is not None
        assert team.get_member("nonexistent") is None

    def test_remove_member(self):
        team = AgentTeam(name="t", lead_agent_id="l")
        team.add_member(TeammateInfo(
            name="bob", agent_id="b1", agent_type="w",
            model="", worktree_path="", backend_type="in-process",
        ))
        assert team.remove_member("bob") is True
        assert len(team.members) == 0
        assert team.remove_member("bob") is False

    def test_set_member_active(self):
        team = AgentTeam(name="t", lead_agent_id="l")
        team.add_member(TeammateInfo(
            name="alice", agent_id="a1", agent_type="w",
            model="", worktree_path="", backend_type="in-process",
            is_active=True,
        ))
        team.set_member_active("alice", False)
        assert team.members[0].is_active is False
        assert team.all_idle() is True

    def test_all_idle(self):
        team = AgentTeam(name="t", lead_agent_id="l")
        team.add_member(TeammateInfo(
            name="alice", agent_id="a1", agent_type="w",
            model="", worktree_path="", backend_type="in-process",
            is_active=False,
        ))
        team.add_member(TeammateInfo(
            name="bob", agent_id="b1", agent_type="w",
            model="", worktree_path="", backend_type="in-process",
            is_active=True,
        ))
        assert team.all_idle() is False

    def test_unique_team_name(self, tmp_dir):
        with patch("coding_agent.teams.models.Path.home", return_value=Path(tmp_dir)):
            name1 = unique_team_name("my-team")
            assert name1 == "my-team"
            (Path(tmp_dir) / ".coding_agent" / "teams" / "my-team").mkdir(parents=True)
            name2 = unique_team_name("my-team")
            assert name2 == "my-team-2"

# =====================================================================
# 2. SharedTaskStore
# =====================================================================

class TestSharedTaskStore:
    def test_create_and_get(self, tmp_dir):
        store = SharedTaskStore(Path(tmp_dir) / "tasks.json")
        store.init_empty()
        task = store.create(title="Do something", description="Details", assignee="alice")
        assert task.id == "1"
        assert task.title == "Do something"

        fetched = store.get("1")
        assert fetched is not None
        assert fetched.assignee == "alice"

    def test_auto_increment_id(self, tmp_dir):
        store = SharedTaskStore(Path(tmp_dir) / "tasks.json")
        store.init_empty()
        t1 = store.create(title="First")
        t2 = store.create(title="Second")
        assert t1.id == "1"
        assert t2.id == "2"

    def test_list_with_filters(self, tmp_dir):
        store = SharedTaskStore(Path(tmp_dir) / "tasks.json")
        store.init_empty()
        store.create(title="A", assignee="alice")
        t2 = store.create(title="B", assignee="bob")
        store.update(t2.id, status="in_progress")

        all_tasks = store.list_tasks()
        assert len(all_tasks) == 2

        pending = store.list_tasks(status="pending")
        assert len(pending) == 1
        assert pending[0].title == "A"

        bobs = store.list_tasks(assignee="bob")
        assert len(bobs) == 1

    def test_update_with_dependencies(self, tmp_dir):
        store = SharedTaskStore(Path(tmp_dir) / "tasks.json")
        store.init_empty()
        store.create(title="Task A")
        store.create(title="Task B")

        updated = store.update("2", add_blocked_by=["1"])
        assert updated is not None
        assert "1" in updated.blocked_by

        updated = store.update("1", add_blocks=["2"])
        assert "2" in updated.blocks

    def test_update_nonexistent_returns_none(self, tmp_dir):
        store = SharedTaskStore(Path(tmp_dir) / "tasks.json")
        store.init_empty()
        assert store.update("999") is None

    def test_persistence(self, tmp_dir):
        path = Path(tmp_dir) / "tasks.json"
        store1 = SharedTaskStore(path)
        store1.init_empty()
        store1.create(title="Persisted task")

        store2 = SharedTaskStore(path)
        tasks = store2.list_tasks()
        assert len(tasks) == 1
        assert tasks[0].title == "Persisted task"

# =====================================================================
# 3. Mailbox
# =====================================================================

class TestMailbox:
    def test_write_and_consume(self, tmp_dir):
        mailbox = Mailbox(tmp_dir)
        msg = create_message("alice", "bob", "Hello bob", summary="greeting")
        mailbox.write("bob-agent-id", msg)

        messages = mailbox.consume("bob-agent-id")
        assert len(messages) == 1
        assert messages[0].content == "Hello bob"
        assert messages[0].from_agent == "alice"

        # 已被消费 —— 此时应该为空
        messages2 = mailbox.consume("bob-agent-id")
        assert len(messages2) == 0

    def test_read_without_consume(self, tmp_dir):
        mailbox = Mailbox(tmp_dir)
        msg = create_message("alice", "bob", "Peek")
        mailbox.write("bob-id", msg)

        messages = mailbox.read("bob-id")
        assert len(messages) == 1

        # 仍然存在
        messages2 = mailbox.read("bob-id")
        assert len(messages2) == 1

    def test_broadcast(self, tmp_dir):
        mailbox = Mailbox(tmp_dir)
        msg = create_message("alice", "*", "Team update", summary="update")
        mailbox.broadcast(["bob-id", "charlie-id", "alice-id"], msg, exclude="alice-id")

        bob_msgs = mailbox.consume("bob-id")
        charlie_msgs = mailbox.consume("charlie-id")
        alice_msgs = mailbox.consume("alice-id")

        assert len(bob_msgs) == 1
        assert len(charlie_msgs) == 1
        assert len(alice_msgs) == 0

    def test_cleanup(self, tmp_dir):
        mailbox = Mailbox(tmp_dir)
        msg = create_message("a", "b", "test")
        mailbox.write("agent-1", msg)
        mailbox.cleanup("agent-1")
        assert len(mailbox.read("agent-1")) == 0

    def test_empty_mailbox(self, tmp_dir):
        mailbox = Mailbox(tmp_dir)
        assert mailbox.consume("nonexistent") == []
        assert mailbox.read("nonexistent") == []

# =====================================================================
# 4. AgentNameRegistry
# =====================================================================

class TestAgentNameRegistry:

    def test_register_and_resolve(self):
        reg = AgentNameRegistry.instance()
        reg.register("alice", "agent-abc")
        assert reg.resolve("alice") == "agent-abc"
        assert reg.resolve("agent-abc") == "agent-abc"  # 直接按 ID 查找
        assert reg.resolve("nonexistent") is None

    def test_unregister(self):
        reg = AgentNameRegistry.instance()
        reg.register("bob", "agent-xyz")
        reg.unregister("bob")
        assert reg.resolve("bob") is None

    def test_list_all(self):
        reg = AgentNameRegistry.instance()
        reg.register("alice", "a1")
        reg.register("bob", "b1")
        all_names = reg.list_all()
        assert all_names == {"alice": "a1", "bob": "b1"}

    def test_singleton(self):
        r1 = AgentNameRegistry.instance()
        r2 = AgentNameRegistry.instance()
        assert r1 is r2

# =====================================================================
# 4.1 lead 收件箱地址一致性（回归测试）
# =====================================================================
#
# 历史问题：teammate 上报 idle 时写死 "lead"，落到 mailbox/lead.json；而
# drain_lead_mailbox 用 team.lead_agent_id（uuid）去读，读的是另一个文件，
# 导致消息永久滞留、lead 根本收不到。以下三条用例分别覆盖三个写入点。

class TestLeadInboxConsistency:

    def test_send_message_to_lead_is_delivered(self, tmp_dir):
        from coding_agent.teams.manager import TeamManager
        from coding_agent.tools.send_message import SendMessageParams, SendMessageTool

        with patch("coding_agent.teams.models.Path.home", return_value=Path(tmp_dir)):
            tm = TeamManager()
            team = tm.create_team("t-send", lead_agent_id="lead-uuid-001", is_interactive=False)
            mailbox = tm.get_mailbox(team.name)

            tool = SendMessageTool(tm, team.name, "alice-id-1", "alice")
            result = asyncio.run(
                tool.execute(SendMessageParams(to="lead", message="接口已确定", summary="接口确定"))
            )

            assert result.is_error is False
            assert len(mailbox.read(LEAD_INBOX)) == 1

            notes = tm.drain_lead_mailbox()
            assert len(notes) == 1
            assert "alice" in notes[0]
            assert "接口已确定" in notes[0]

    def test_teammate_idle_write_is_drained(self, tmp_dir):
        # 模拟 task_manager / spawn_inprocess 的写法：直接写 LEAD_INBOX
        from coding_agent.teams.manager import TeamManager

        with patch("coding_agent.teams.models.Path.home", return_value=Path(tmp_dir)):
            tm = TeamManager()
            team = tm.create_team("t-idle", lead_agent_id="lead-uuid-002", is_interactive=False)
            mailbox = tm.get_mailbox(team.name)

            mailbox.write(
                LEAD_INBOX,
                create_message(
                    from_agent="bob",
                    to_agent=LEAD_INBOX,
                    content="[idle] bob: completed initial task",
                    summary="bob idle",
                ),
            )

            notes = tm.drain_lead_mailbox()
            assert len(notes) == 1
            assert "bob" in notes[0]
            # 消费后不重复投递
            assert tm.drain_lead_mailbox() == []

    def test_set_member_idle_is_drained(self, tmp_dir):
        from coding_agent.teams.manager import TeamManager

        with patch("coding_agent.teams.models.Path.home", return_value=Path(tmp_dir)):
            tm = TeamManager()
            team = tm.create_team("t-setidle", lead_agent_id="lead-uuid-003", is_interactive=False)
            tm.register_member(team.name, TeammateInfo(
                name="carol", agent_id="carol-id-1", agent_type="worker",
                model="", worktree_path="", backend_type="in-process", is_active=True,
            ))

            tm.set_member_idle(team.name, "carol")

            notes = tm.drain_lead_mailbox()
            assert len(notes) == 1
            assert "carol" in notes[0]

# =====================================================================
# 4.2 shutdown 语义（回归测试）
# =====================================================================
#
# 历史问题：shutdown 有两种表达（message_type="shutdown_request" 与
# "[shutdown]" 内容前缀），但只有前缀被识别；而且生产路径 TaskManager 的
# 待命轮询里根本没有 shutdown 判定，关闭请求会被当成新一轮 prompt。

class TestShutdownHandling:

    def test_is_shutdown_request_by_type(self):
        from coding_agent.teams.mailbox import create_message, is_shutdown_request
        msg = create_message(
            from_agent="lead", to_agent="alice",
            content="stop now", message_type="shutdown_request",
        )
        assert is_shutdown_request(msg) is True

    def test_is_shutdown_request_by_prefix(self):
        from coding_agent.teams.mailbox import create_message, is_shutdown_request
        msg = create_message(from_agent="lead", to_agent="alice", content="[shutdown] stop")
        assert is_shutdown_request(msg) is True

    def test_normal_message_is_not_shutdown(self):
        from coding_agent.teams.mailbox import create_message, is_shutdown_request
        msg = create_message(from_agent="lead", to_agent="alice", content="please do X")
        assert is_shutdown_request(msg) is False

    def test_partition_shutdown(self):
        from coding_agent.teams.mailbox import create_message, partition_shutdown
        shutdown = create_message("lead", "alice", "stop", message_type="shutdown_request")
        normal = create_message("lead", "alice", "do X")

        keep, has_shutdown = partition_shutdown([shutdown, normal])
        assert has_shutdown is True
        assert [m.content for m in keep] == ["do X"]

        keep2, has_shutdown2 = partition_shutdown([normal])
        assert has_shutdown2 is False
        assert len(keep2) == 1

    @pytest.mark.asyncio
    async def test_background_teammate_stops_on_shutdown_request(self, tmp_dir):
        """生产路径：TaskManager 待命轮询必须响应 shutdown_request。"""
        from coding_agent.agents.task_manager import TaskManager
        from coding_agent.teams.mailbox import Mailbox

        mailbox = Mailbox(Path(tmp_dir) / "mailbox")
        teammate_id = "alice-id-1"
        # 刻意用 message_type 声明、不带 "[shutdown]" 前缀，验证结构化声明也生效
        mailbox.write(teammate_id, create_message(
            from_agent="lead", to_agent=teammate_id,
            content="stop working", message_type="shutdown_request",
        ))

        agent = MagicMock()
        agent.agent_id = teammate_id
        agent.team_name = "t1"
        agent.total_input_tokens = 0
        agent.total_output_tokens = 0
        agent.run_to_completion = AsyncMock(return_value="initial done")
        team_manager_mock = MagicMock()
        team_manager_mock.get_mailbox.return_value = mailbox
        agent._team_manager = team_manager_mock

        tm = TaskManager()
        task_id = tm.launch(agent, "initial task")
        await tm._async_tasks[task_id]

        # 只跑了初始任务；shutdown 请求没有被当成新一轮 prompt
        assert agent.run_to_completion.await_count == 1
        assert tm.get(task_id).status == "stopped"

# =====================================================================
# 4.3 任务板一致性 + 指派通知（回归测试）
# =====================================================================
#
# 任务板是「共享状态」：多个实例/进程写同一个文件。此前它既没有文件锁、
# 写入前也不重新加载，会静默丢更新；而且它的变更不产生任何通知，
# 被指派人无从得知。下面把这两处行为都锁死。

class TestSharedTaskStoreConsistency:

    def test_write_reloads_before_saving(self, tmp_dir):
        """写前必须重新加载，否则会用陈旧缓存覆盖别人的写入（丢数据）。"""
        path = Path(tmp_dir) / "tasks.json"
        a = SharedTaskStore(path)
        b = SharedTaskStore(path)          # b 先加载：此时文件还是空的

        t1 = a.create(title="t1")          # a 写入后，b 的缓存已陈旧
        t2 = b.create(title="t2")          # b 若不重载：会覆盖 t1，且 id 冲突

        assert t1.id != t2.id
        titles = {t.title for t in SharedTaskStore(path).list_tasks()}
        assert titles == {"t1", "t2"}

    def test_update_reloads_before_saving(self, tmp_dir):
        """update 同样不能在陈旧缓存上写，否则会抹掉别人的新任务与新状态。"""
        path = Path(tmp_dir) / "tasks.json"
        a = SharedTaskStore(path)
        t1 = a.create(title="t1")

        b = SharedTaskStore(path)
        b.create(title="t2")
        b.update(t1.id, status="completed")

        # a 仍持有旧缓存，此处更新 assignee：不能丢 t2，也不能把 completed 回退
        a.update(t1.id, assignee="bob")

        store = SharedTaskStore(path)
        assert {t.title for t in store.list_tasks()} == {"t1", "t2"}
        t1_after = store.get(t1.id)
        assert t1_after.assignee == "bob"
        assert t1_after.status == "completed"

    def test_lock_released_and_no_temp_left(self, tmp_dir):
        path = Path(tmp_dir) / "tasks.json"
        store = SharedTaskStore(path)
        store.create(title="t")

        assert not (Path(tmp_dir) / "tasks.json.lock").exists()
        assert list(Path(tmp_dir).glob("*.tmp*")) == []

    def test_stale_lock_is_recovered(self, tmp_dir):
        path = Path(tmp_dir) / "tasks.json"
        lock = Path(tmp_dir) / "tasks.json.lock"
        store = SharedTaskStore(path)

        lock.write_text("", encoding="utf-8")
        old = time.time() - 60
        os.utime(lock, (old, old))

        task = store.create(title="t")     # 应清理陈旧锁并正常写入
        assert task.id == "1"
        assert not lock.exists()

class TestTaskBoardNotification:

    def _make_team(self, tmp_dir, team_name):
        from coding_agent.teams.manager import TeamManager
        tm = TeamManager()
        team = tm.create_team(team_name, lead_agent_id="lead-uuid-n", is_interactive=False)
        tm.register_member(team.name, TeammateInfo(
            name="bob", agent_id="bob-id-1", agent_type="worker",
            model="", worktree_path="", backend_type="in-process", is_active=True,
        ))
        return tm, team

    @pytest.mark.asyncio
    async def test_task_create_pushes_to_assignee(self, tmp_dir):
        from coding_agent.tools.task_create import TaskCreateParams, TaskCreateTool

        with patch("coding_agent.teams.models.Path.home", return_value=Path(tmp_dir)):
            tm, team = self._make_team(tmp_dir, "t-notify")
            res = await TaskCreateTool(tm, team.name, "alice").execute(
                TaskCreateParams(title="实现登录接口", assignee="bob")
            )

            assert res.is_error is False
            msgs = tm.get_mailbox(team.name).consume("bob-id-1")
            assert len(msgs) == 1
            assert "实现登录接口" in msgs[0].content
            assert msgs[0].from_agent == "alice"

    @pytest.mark.asyncio
    async def test_task_create_pushes_to_lead_alias(self, tmp_dir):
        from coding_agent.tools.task_create import TaskCreateParams, TaskCreateTool

        with patch("coding_agent.teams.models.Path.home", return_value=Path(tmp_dir)):
            tm, team = self._make_team(tmp_dir, "t-notify-lead")
            await TaskCreateTool(tm, team.name, "alice").execute(
                TaskCreateParams(title="汇总结果", assignee="lead")
            )

            assert len(tm.get_mailbox(team.name).consume(LEAD_INBOX)) == 1

    @pytest.mark.asyncio
    async def test_task_create_unassigned_sends_nothing(self, tmp_dir):
        from coding_agent.tools.task_create import TaskCreateParams, TaskCreateTool

        with patch("coding_agent.teams.models.Path.home", return_value=Path(tmp_dir)):
            tm, team = self._make_team(tmp_dir, "t-notify-none")
            await TaskCreateTool(tm, team.name, "alice").execute(
                TaskCreateParams(title="无主任务")
            )

            assert tm.get_mailbox(team.name).consume("bob-id-1") == []

    @pytest.mark.asyncio
    async def test_task_create_unknown_assignee_warns_without_error(self, tmp_dir):
        from coding_agent.tools.task_create import TaskCreateParams, TaskCreateTool

        with patch("coding_agent.teams.models.Path.home", return_value=Path(tmp_dir)):
            tm, team = self._make_team(tmp_dir, "t-notify-unknown")
            res = await TaskCreateTool(tm, team.name, "alice").execute(
                TaskCreateParams(title="x", assignee="nobody")
            )

            assert res.is_error is False
            assert "未能通知" in res.output

    @pytest.mark.asyncio
    async def test_task_update_pushes_to_new_assignee(self, tmp_dir):
        from coding_agent.tools.task_create import TaskCreateParams, TaskCreateTool
        from coding_agent.tools.task_update import TaskUpdateParams, TaskUpdateTool

        with patch("coding_agent.teams.models.Path.home", return_value=Path(tmp_dir)):
            tm, team = self._make_team(tmp_dir, "t-notify-update")
            await TaskCreateTool(tm, team.name, "alice").execute(
                TaskCreateParams(title="重构模块")
            )
            task_id = tm.get_task_store(team.name).list_tasks()[0].id

            res = await TaskUpdateTool(tm, team.name).execute(
                TaskUpdateParams(task_id=task_id, assignee="bob")
            )

            assert res.is_error is False
            msgs = tm.get_mailbox(team.name).consume("bob-id-1")
            assert len(msgs) == 1
            assert "重构模块" in msgs[0].content

# =====================================================================
# 5. Backend Detection（后端探测）
# =====================================================================

class TestBackendDetect:
    def test_in_process_mode(self):
        result = detect_backend(teammate_mode="in-process")
        assert result == BackendType.IN_PROCESS

    def test_non_interactive(self):
        result = detect_backend(is_interactive=False)
        assert result == BackendType.IN_PROCESS

    def test_detect_backend_always_in_process(self):
        # detect_backend 统一返回 IN_PROCESS，pane 检测由 detect_pane_backend 负责
        with patch.dict(os.environ, {"TMUX": "/tmp/tmux-1234/default,12345,0"}):
            result = detect_backend()
            assert result == BackendType.IN_PROCESS

    def test_pane_tmux_session(self):
        with patch.dict(os.environ, {"TMUX": "/tmp/tmux-1234/default,12345,0"}):
            result = detect_pane_backend()
            assert result == BackendType.TMUX

    def test_pane_iterm2_with_it2(self):
        env = {"TERM_PROGRAM": "iTerm.app"}
        with patch.dict(os.environ, env, clear=False):
            with patch("coding_agent.teams.backend_detect.shutil.which") as mock_which:
                def which_side_effect(cmd):
                    if cmd == "it2":
                        return "/usr/local/bin/it2"
                    if cmd == "tmux":
                        return None
                    return None
                mock_which.side_effect = which_side_effect
                with patch.dict(os.environ, {"TMUX": ""}, clear=False):
                    os.environ.pop("TMUX", None)
                    result = detect_pane_backend()
                    assert result == BackendType.ITERM2

    def test_pane_tmux_installed_not_in_session(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TMUX", None)
            os.environ.pop("TERM_PROGRAM", None)
            with patch("coding_agent.teams.backend_detect.shutil.which") as mock_which:
                mock_which.return_value = "/usr/bin/tmux"
                result = detect_pane_backend()
                assert result == BackendType.TMUX

    def test_pane_no_backend_falls_back_to_in_process(self):
        # 没有外部终端时，detect_pane_backend 回退到 IN_PROCESS 而非抛异常
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TMUX", None)
            os.environ.pop("TERM_PROGRAM", None)
            with patch("coding_agent.teams.backend_detect.shutil.which", return_value=None):
                result = detect_pane_backend()
                assert result == BackendType.IN_PROCESS

# =====================================================================
# 6. Tool Filtering（工具过滤）
# =====================================================================

class TestToolFilter:
    def test_teammate_coordination_tools_in_allowed(self):
        for tool_name in TEAMMATE_COORDINATION_TOOLS:
            assert tool_name in IN_PROCESS_TEAMMATE_ALLOWED_TOOLS

    def test_coordinator_mode_tools(self):
        assert "Agent" in COORDINATOR_MODE_ALLOWED_TOOLS
        assert "SendMessage" in COORDINATOR_MODE_ALLOWED_TOOLS
        assert "TaskStop" in COORDINATOR_MODE_ALLOWED_TOOLS
        assert "SyntheticOutput" in COORDINATOR_MODE_ALLOWED_TOOLS
        assert "ReadFile" not in COORDINATOR_MODE_ALLOWED_TOOLS
        assert "WriteFile" not in COORDINATOR_MODE_ALLOWED_TOOLS
        assert "Bash" not in COORDINATOR_MODE_ALLOWED_TOOLS

    def test_apply_coordinator_filter(self):
        reg = make_registry(
            "Agent", "ReadFile", "WriteFile", "Bash", "SendMessage",
            "TaskStop", "SyntheticOutput", "TeamCreate", "TeamDelete",
        )
        filtered = apply_coordinator_filter(reg)
        names = {t.name for t in filtered.list_tools()}
        assert "Agent" in names
        assert "SendMessage" in names
        assert "SyntheticOutput" in names
        assert "ReadFile" not in names
        assert "Bash" not in names

# =====================================================================
# 7. Coordinator Mode（协调者模式）
# =====================================================================

class TestCoordinatorMode:
    def test_disabled_by_default(self):
        assert is_coordinator_mode(enable_flag=False) is False

    def test_enabled_with_flag_and_env(self):
        with patch.dict(os.environ, {"CODING_AGENT_COORDINATOR_MODE": "1"}):
            assert is_coordinator_mode(enable_flag=True) is True

    def test_flag_without_env(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CODING_AGENT_COORDINATOR_MODE", None)
            assert is_coordinator_mode(enable_flag=True) is False

    def test_env_without_flag(self):
        with patch.dict(os.environ, {"CODING_AGENT_COORDINATOR_MODE": "1"}):
            assert is_coordinator_mode(enable_flag=False) is False

    def test_system_prompt_contains_phases(self):
        prompt = get_coordinator_system_prompt()
        assert "Research" in prompt
        assert "Synthesis" in prompt
        assert "Implementation" in prompt
        assert "Verification" in prompt

    def test_system_prompt_anti_pattern(self):
        prompt = get_coordinator_system_prompt()
        assert "based on your findings" in prompt.lower()
        assert "Anti-pattern" in prompt or "BAD" in prompt

    def test_system_prompt_continue_vs_spawn(self):
        prompt = get_coordinator_system_prompt()
        assert "Continue" in prompt
        assert "Spawn fresh" in prompt

    def test_system_prompt_task_notification(self):
        prompt = get_coordinator_system_prompt()
        assert "<task-notification>" in prompt
        assert "<task-id>" in prompt

    def test_match_session_mode_no_switch(self):
        with patch.dict(os.environ, {"CODING_AGENT_COORDINATOR_MODE": "1"}):
            result = match_session_mode("coordinator", enable_flag=True)
            assert result is None

    def test_match_session_mode_switch_to_coordinator(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CODING_AGENT_COORDINATOR_MODE", None)
            result = match_session_mode("coordinator", enable_flag=True)
            assert result is not None
            assert "Entered" in result
            assert os.environ.get("CODING_AGENT_COORDINATOR_MODE") == "1"

    def test_match_session_mode_none(self):
        result = match_session_mode(None)
        assert result is None

    def test_coordinator_user_context(self):
        ctx = get_coordinator_user_context()
        assert "workerToolsContext" in ctx
        assert "Workers" in ctx["workerToolsContext"]

# =====================================================================
# 8. Config Extensions（配置项扩展）
# =====================================================================

class TestConfigExtensions:
    def test_teammate_mode_defaults(self):
        from coding_agent.config import AppConfig
        cfg = AppConfig(providers=[])
        assert cfg.teammate_mode == ""
        assert cfg.enable_coordinator_mode is False

    def test_load_config_with_team_fields(self, tmp_dir):
        from coding_agent.config import load_config
        config_path = Path(tmp_dir) / "config.yaml"
        config_path.write_text(
            "providers:\n"
            "  - name: test\n"
            "    protocol: anthropic\n"
            "    base_url: http://localhost\n"
            "    model: test-model\n"
            "teammate_mode: 'in-process'\n"
            "enable_coordinator_mode: true\n"
        )
        cfg = load_config(config_path)
        assert cfg.teammate_mode == "in-process"
        assert cfg.enable_coordinator_mode is True

    def test_invalid_teammate_mode(self, tmp_dir):
        from coding_agent.config import ConfigError, load_config
        config_path = Path(tmp_dir) / "config.yaml"
        config_path.write_text(
            "providers:\n"
            "  - name: test\n"
            "    protocol: anthropic\n"
            "    base_url: http://localhost\n"
            "    model: test-model\n"
            "teammate_mode: 'invalid'\n"
        )
        with pytest.raises(ConfigError):
            load_config(config_path)

# =====================================================================
# 9. Transcript Persistence（会话记录持久化）
# =====================================================================

class TestTranscript:

    def test_save_and_load(self, tmp_dir):
        from coding_agent.conversation import ConversationManager
        from coding_agent.teams.transcript import load_transcript, save_transcript

        conv = ConversationManager()
        conv.add_user_message("Hello agent")
        conv.add_assistant_message("Hello user")

        with patch("coding_agent.teams.models.Path.home", return_value=Path(tmp_dir)):
            save_transcript("test-team", "agent-001", conv)
            restored = load_transcript("test-team", "agent-001")

        assert restored is not None
        assert len(restored.history) == 2
        assert restored.history[0].role == "user"
        assert restored.history[0].content == "Hello agent"
        assert restored.history[1].role == "assistant"

    def test_load_nonexistent(self, tmp_dir):
        from coding_agent.teams.transcript import load_transcript
        with patch("coding_agent.teams.models.Path.home", return_value=Path(tmp_dir)):
            result = load_transcript("no-team", "no-agent")
        assert result is None

# =====================================================================
# 10. Agent build_system_prompt 集成测试
# =====================================================================

class TestAgentCoordinatorIntegration:
    def test_normal_prompt(self):
        from coding_agent.prompts import build_system_prompt, IDENTITY_SECTION
        prompt = build_system_prompt()
        # 验证 identity section 内容包含在 prompt 中
        assert "Coding Agent" in prompt
        assert IDENTITY_SECTION.content[:30] in prompt

    def test_coordinator_prompt(self):
        from coding_agent.prompts import build_system_prompt
        prompt = build_system_prompt(coordinator_mode=True)
        assert "coordinator" in prompt.lower()

    def test_coordinator_mode_overrides_normal(self):
        from coding_agent.prompts import build_system_prompt
        # coordinator 模式走独立的 prompt 生成路径，不包含普通 identity 段
        prompt = build_system_prompt(coordinator_mode=True)
        assert "coordinator" in prompt.lower()
