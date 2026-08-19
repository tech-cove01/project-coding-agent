"""工具网关（tool_gateway.py）测试。"""
from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel

from coding_agent.tool_gateway import (
    InputGuard,
    OutputNormalizer,
    RetryPolicy,
    ToolGateway,
)
from coding_agent.tools.base import Tool, ToolResult


class EchoParams(BaseModel):
    content: str = ""


class _EchoTool(Tool):
    """返回固定输出/错误的假工具。"""

    name = "EchoTool"
    description = "echo"
    params_model = EchoParams

    def __init__(self, output: str = "ok", error: bool = False) -> None:
        self._output = output
        self._error = error

    async def execute(self, params: BaseModel) -> ToolResult:
        return ToolResult(output=self._output, is_error=self._error)


class _FlakyTool(Tool):
    """前 N 次抛瞬时错误，之后成功。"""

    name = "FlakyTool"
    description = "flaky"
    params_model = EchoParams

    def __init__(self, failures_before_success: int = 2) -> None:
        self._failures = failures_before_success
        self.calls = 0

    async def execute(self, params: BaseModel) -> ToolResult:
        self.calls += 1
        if self.calls <= self._failures:
            raise ConnectionError("temporary network glitch")
        return ToolResult(output="recovered")


class _PermanentErrorTool(Tool):
    name = "PermTool"
    description = "permanent"
    params_model = EchoParams

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, params: BaseModel) -> ToolResult:
        self.calls += 1
        raise ValueError("bad args, should not retry")


class _ContentEchoTool(Tool):
    """返回传入 content 参数的工具（用于验证输入防护截断）。"""

    name = "ContentEchoTool"
    description = "echo content"
    params_model = EchoParams

    async def execute(self, params: BaseModel) -> ToolResult:
        return ToolResult(output=params.content)


# ---------------------------------------------------------------------------
# 输入防护
# ---------------------------------------------------------------------------

def test_guard_content_under_limit_passthrough() -> None:
    g = InputGuard()
    safe, truncated = g.guard_content("hello")
    assert safe == "hello"
    assert truncated is False


def test_guard_content_truncates_large() -> None:
    g = InputGuard(max_file_bytes=10)
    big = "a" * 100
    safe, truncated = g.guard_content(big)
    assert truncated is True
    assert "truncated" in safe
    assert len(safe) < 100


def test_guard_bytes_content() -> None:
    g = InputGuard(max_file_bytes=10)
    safe, truncated = g.guard_content(b"b" * 50)
    assert truncated is True


def test_guard_none() -> None:
    g = InputGuard()
    safe, truncated = g.guard_content(None)
    assert safe == ""
    assert truncated is False


# ---------------------------------------------------------------------------
# 重试策略
# ---------------------------------------------------------------------------

def test_retry_delay_exponential_and_capped() -> None:
    r = RetryPolicy(base_delay=1.0, max_delay=4.0)
    assert r.delay_for(0) == 1.0
    assert r.delay_for(1) == 2.0
    assert r.delay_for(2) == 4.0
    assert r.delay_for(10) == 4.0  # 封顶


def test_retry_is_retryable() -> None:
    r = RetryPolicy()
    assert r.is_retryable(ConnectionError("x"))
    assert r.is_retryable(TimeoutError("x"))
    assert not r.is_retryable(ValueError("x"))


# ---------------------------------------------------------------------------
# 输出归一化
# ---------------------------------------------------------------------------

def test_normalizer_truncates_long_output() -> None:
    n = OutputNormalizer(max_chars=10)
    result = n.normalize(ToolResult(output="x" * 100))
    assert "truncated" in result.output
    assert result.is_error is False


def test_normalizer_keeps_error_flag() -> None:
    n = OutputNormalizer()
    result = n.normalize(ToolResult(output="boom", is_error=True))
    assert result.is_error is True


# ---------------------------------------------------------------------------
# 网关执行
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gateway_success() -> None:
    gw = ToolGateway()
    result = await gw.execute(_EchoTool(output="hi"), EchoParams())
    assert result.output == "hi"
    assert result.is_error is False


@pytest.mark.asyncio
async def test_gateway_retries_then_succeeds() -> None:
    tool = _FlakyTool(failures_before_success=2)
    gw = ToolGateway(retry=RetryPolicy(max_attempts=4, base_delay=0.0))
    result = await gw.execute(tool, EchoParams())
    assert result.output == "recovered"
    assert tool.calls == 3  # 2 次失败 + 1 次成功


@pytest.mark.asyncio
async def test_gateway_gives_up_after_max_attempts() -> None:
    tool = _FlakyTool(failures_before_success=99)
    gw = ToolGateway(retry=RetryPolicy(max_attempts=3, base_delay=0.0))
    result = await gw.execute(tool, EchoParams())
    assert result.is_error is True
    assert "failed after" in result.output
    assert tool.calls == 3  # 正好尝试 3 次


@pytest.mark.asyncio
async def test_gateway_does_not_retry_permanent_error() -> None:
    tool = _PermanentErrorTool()
    gw = ToolGateway(retry=RetryPolicy(max_attempts=5))
    result = await gw.execute(tool, EchoParams())
    assert result.is_error is True
    assert tool.calls == 1  # 只尝试 1 次，永久错误不重试


@pytest.mark.asyncio
async def test_gateway_guards_large_content_param() -> None:
    tool = _ContentEchoTool()
    gw = ToolGateway(guard=InputGuard(max_file_bytes=10))
    # 构造一个超大 content 参数
    big = "z" * 100
    result = await gw.execute(tool, EchoParams(content=big))
    # 网关会把超大的 content 截断后传给工具，工具返回截断后的内容
    assert "truncated" in result.output
    assert len(result.output) < 100


@pytest.mark.asyncio
async def test_gateway_timeout() -> None:
    class SlowTool(Tool):
        name = "SlowTool"
        description = "slow"
        params_model = EchoParams

        async def execute(self, params: BaseModel) -> ToolResult:
            await asyncio.sleep(5)
            return ToolResult(output="late")

    gw = ToolGateway(timeout=0.1, retry=RetryPolicy(max_attempts=1))
    result = await gw.execute(SlowTool(), EchoParams())
    # 超时（asyncio.TimeoutError 是可重试异常），但因 max_attempts=1 直接失败
    assert result.is_error is True


@pytest.mark.asyncio
async def test_gateway_on_retry_callback() -> None:
    tool = _FlakyTool(failures_before_success=1)
    fired: list[int] = []
    gw = ToolGateway(
        retry=RetryPolicy(max_attempts=3, base_delay=0.0),
        on_retry=lambda name, n, e: fired.append(n),
    )
    result = await gw.execute(tool, EchoParams())
    assert result.output == "recovered"
    assert fired == [1]  # 触发过一次重试回调
