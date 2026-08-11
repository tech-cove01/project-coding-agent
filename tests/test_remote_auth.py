"""Remote 服务的权限模式切换测试。

登录鉴权已移除，这里只覆盖保留的权限模式功能：
- _handle_permission_mode 的模式映射
- _current_permission_mode 的默认返回
"""
from __future__ import annotations

import pytest

from coding_agent.config import ProviderConfig
from coding_agent.permissions import PermissionMode
from coding_agent.remote import RemoteServer


def _make_server() -> RemoteServer:
    provider = ProviderConfig(
        name="mock",
        protocol="openai-compat",
        base_url="http://localhost",
        model="mock-model",
        api_key="mock-key",
    )
    return RemoteServer(providers=[provider])


class FakeAgent:
    """模拟 Agent，只记录权限模式切换。"""

    def __init__(self) -> None:
        self.mode = PermissionMode.DEFAULT

    def set_permission_mode(self, mode: PermissionMode) -> None:
        self.mode = mode


class TestPermissionMode:
    @pytest.mark.asyncio
    async def test_map_all_modes(self):
        server = _make_server()
        agent = FakeAgent()
        server.agent = agent

        cases = {
            "default": PermissionMode.DEFAULT,
            "acceptEdits": PermissionMode.ACCEPT_EDITS,
            "plan": PermissionMode.PLAN,
            "bypassPermissions": PermissionMode.BYPASS,
        }
        for mode_str, expected in cases.items():
            await server._handle_permission_mode({"mode": mode_str})
            assert agent.mode == expected, f"{mode_str} -> {expected}"

    @pytest.mark.asyncio
    async def test_unknown_mode_ignored(self):
        server = _make_server()
        agent = FakeAgent()
        server.agent = agent
        await server._handle_permission_mode({"mode": "not_a_mode"})
        assert agent.mode == PermissionMode.DEFAULT

    @pytest.mark.asyncio
    async def test_no_agent_ignored(self):
        server = _make_server()
        server.agent = None
        # 不崩溃即可
        await server._handle_permission_mode({"mode": "bypassPermissions"})


class TestCurrentMode:
    def test_returns_default_when_no_agent(self):
        server = _make_server()
        server.agent = None
        assert server._current_permission_mode() == "default"

    def test_returns_agent_mode(self):
        server = _make_server()
        agent = FakeAgent()
        agent.set_permission_mode(PermissionMode.ACCEPT_EDITS)
        server.agent = agent
        server.permission_checker = agent  # 复用 FakeAgent 暴露 mode
        assert server._current_permission_mode() == "acceptEdits"
