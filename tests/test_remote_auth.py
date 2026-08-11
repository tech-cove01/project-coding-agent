"""Remote 服务的登录鉴权与权限模式切换测试。

验证：
- auth_token 的来源优先级（显式传入 > 环境变量 > 自动生成）
- _authenticate 的 token 校验逻辑
- _handle_permission_mode 的模式映射
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

import pytest

from coding_agent.config import ProviderConfig
from coding_agent.permissions import PermissionMode
from coding_agent.remote import RemoteServer


def _make_server(token: str | None = None) -> RemoteServer:
    provider = ProviderConfig(
        name="mock",
        protocol="openai-compat",
        base_url="http://localhost",
        model="mock-model",
        api_key="mock-key",
    )
    return RemoteServer(providers=[provider], auth_token=token)


class FakeWebSocket:
    """模拟 websockets ServerConnection 的最小实现。

    同时作为 async iterable：__aiter__ 迭代预先提供的消息序列，
    send() 记录发出的消息，close() 记录关闭。
    """

    def __init__(self, incoming: list[dict] | None = None) -> None:
        self._incoming = [json.dumps(m) for m in (incoming or [])]
        self.sent: list[dict] = []
        self.closed = False

    def __aiter__(self) -> AsyncIterator[str]:
        async def gen() -> AsyncIterator[str]:
            for raw in self._incoming:
                yield raw
        return gen()

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True


class FakeAgent:
    """模拟 Agent，只记录权限模式切换。"""

    def __init__(self) -> None:
        self.mode = PermissionMode.DEFAULT

    def set_permission_mode(self, mode: PermissionMode) -> None:
        self.mode = mode


# ---------------------------------------------------------------------------
# auth_token 来源
# ---------------------------------------------------------------------------

class TestAuthTokenSource:
    def test_explicit_token_wins(self):
        server = _make_server(token="explicit-token")
        assert server.auth_token == "explicit-token"

    def test_auto_generate_when_none(self, monkeypatch):
        monkeypatch.delenv("CODING_AGENT_REMOTE_TOKEN", raising=False)
        server = _make_server(token=None)
        # 自动生成一个非空的随机 token
        assert server.auth_token != ""
        assert len(server.auth_token) >= 20


# ---------------------------------------------------------------------------
# _authenticate 登录校验
# ---------------------------------------------------------------------------

class TestAuthenticate:
    @pytest.mark.asyncio
    async def test_valid_token(self):
        server = _make_server(token="secret-token")
        ws = FakeWebSocket([
            {"type": "login", "data": {"token": "secret-token"}},
        ])
        result = await server._authenticate(ws)
        assert result is True
        # 成功时发送 auth_ok
        assert ws.sent[-1]["type"] == "auth_ok"

    @pytest.mark.asyncio
    async def test_wrong_token_rejected(self):
        server = _make_server(token="real-token")
        ws = FakeWebSocket([
            {"type": "login", "data": {"token": "wrong-token"}},
        ])
        result = await server._authenticate(ws)
        assert result is False
        assert ws.sent[-1]["type"] == "auth_error"

    @pytest.mark.asyncio
    async def test_first_message_must_be_login(self):
        server = _make_server(token="real-token")
        ws = FakeWebSocket([
            {"type": "user_message", "data": {"content": "hi"}},
        ])
        result = await server._authenticate(ws)
        assert result is False
        assert ws.sent[-1]["type"] == "auth_error"


# ---------------------------------------------------------------------------
# _handle_permission_mode 模式映射
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# _current_permission_mode
# ---------------------------------------------------------------------------

class TestCurrentMode:
    def test_returns_default_when_no_agent(self):
        server = _make_server()
        server.agent = None
        assert server._current_permission_mode() == "default"
