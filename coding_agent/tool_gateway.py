"""工具网关（ToolGateway）：统一封装工具执行的稳定性与防护。

定位：在"Agent 主循环直接调用 tool.execute()"之上加一层可选封装，补齐
现有架构中尚未覆盖的工程能力：

  - InputGuard      输入防护：限制单文件读写大小，超大文件分片截断，防 OOM
  - RetryPolicy     异常重试：针对 MCP/远程工具的瞬时失败，指数退避重试
  - OutputNormalizer 输出归一化：统一输出截断与错误标注
  - ToolGateway     网关入口：按"防护 → 执行 → 重试 → 归一化"流水线封装

设计原则：
  - 纯 Python + asyncio，不依赖具体工具实现（面向 Tool 接口）。
  - 向后兼容：不修改 agent 主循环，作为可选组件供调用方接入。
  - 可独立测试：不依赖真实 MCP / LLM。

用法示例：
    gateway = ToolGateway(retry=RetryPolicy(max_attempts=3))
    result = await gateway.execute(tool, params)
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from pydantic import BaseModel

from coding_agent.tools.base import MAX_OUTPUT_CHARS, Tool, ToolResult

log = logging.getLogger(__name__)

# 单文件写入/读取的软上限（字节）。超过即触发防护。
DEFAULT_MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MB
# 单次工具输出保留的最大字符数（沿用现有 MAX_OUTPUT_CHARS 语义）
DEFAULT_MAX_OUTPUT_CHARS = MAX_OUTPUT_CHARS


# ---------------------------------------------------------------------------
# 输入防护
# ---------------------------------------------------------------------------

@dataclass
class InputGuard:
    """输入防护：限制文件读写大小，超大文件分片截断，防止 OOM。

    主要通过两种方式生效：
      1. 拦截超大内容的直接写入/返回（超过 max_file_bytes 即截断并提示）。
      2. 对大输出做归一化截断（见 OutputNormalizer，二者可配合）。
    """

    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    # 分片读取时每片的最大字节数
    chunk_size: int = 256 * 1024  # 256 KB

    def guard_content(self, content: str | bytes | None) -> tuple[str, bool]:
        """对工具输入/输出内容做大小防护。

        返回 (安全内容, 是否被截断)。content 超出 max_file_bytes 时，
        只保留开头片段并追加截断提示，避免超大字符串进入上下文造成 OOM。
        """
        if content is None:
            return "", False
        if isinstance(content, bytes):
            data = content
        else:
            try:
                data = content.encode("utf-8")
            except (UnicodeEncodeError, ValueError):
                return str(content)[: self.max_file_bytes], False
        if len(data) <= self.max_file_bytes:
            return (content if isinstance(content, str) else data.decode("utf-8", errors="replace")), False
        head = data[: self.max_file_bytes].decode("utf-8", errors="replace")
        return head + f"\n… (input truncated: {len(data)} bytes > {self.max_file_bytes})", True

    def file_size(self, path: str) -> int:
        """返回文件字节数（不存在返回 0，异常安全）。"""
        import os

        try:
            return os.path.getsize(path)
        except OSError:
            return 0


# ---------------------------------------------------------------------------
# 异常重试（指数退避）
# ---------------------------------------------------------------------------

@dataclass
class RetryPolicy:
    """指数退避重试策略，针对 MCP / 远程工具的瞬时失败。

    不重试永久性错误（如权限拒绝、参数错误），只重试可恢复的瞬时错误
    （网络抖动、MCP 短暂不可用、超时等），并受 max_attempts 限制。
    """

    max_attempts: int = 3
    base_delay: float = 0.5  # 首次等待秒数
    max_delay: float = 8.0  # 退避上限
    # 判定"可重试"的异常类型（MCP 超时/连接错误/临时故障）
    retryable_exceptions: tuple[type[BaseException], ...] = (
        TimeoutError,
        asyncio.TimeoutError,
        ConnectionError,
        OSError,
    )

    def delay_for(self, attempt: int) -> float:
        """第 attempt 次（0 起）重试前的等待时间：base * 2^attempt，封顶。"""
        return min(self.base_delay * (2**attempt), self.max_delay)

    def is_retryable(self, exc: BaseException) -> bool:
        return isinstance(exc, self.retryable_exceptions)


# ---------------------------------------------------------------------------
# 输出归一化
# ---------------------------------------------------------------------------

@dataclass
class OutputNormalizer:
    """输出归一化：统一截断超大输出，保证返回结构稳定。"""

    max_chars: int = DEFAULT_MAX_OUTPUT_CHARS

    def normalize(self, result: ToolResult) -> ToolResult:
        """对 ToolResult 做统一归一化（截断 + 错误标注兜底）。"""
        text = result.output or ""
        truncated = False
        if len(text) > self.max_chars:
            text = text[: self.max_chars] + "\n… (output truncated)"
            truncated = True
        if truncated and not result.is_error:
            # 截断通常意味着输出过大，但不算错误；仅当工具本就报错时才 is_error
            pass
        return ToolResult(output=text, is_error=result.is_error)


# ---------------------------------------------------------------------------
# 网关入口
# ---------------------------------------------------------------------------

class ToolGateway:
    """工具网关：把"输入防护 → 执行 → 指数退避重试 → 输出归一化"串成一条流水线。

    可在不修改 Agent 主循环的前提下，对工具执行做统一增强。
    """

    def __init__(
        self,
        guard: InputGuard | None = None,
        retry: RetryPolicy | None = None,
        normalizer: OutputNormalizer | None = None,
        *,
        timeout: float | None = None,
        on_retry: Callable[[str, int, BaseException], None] | None = None,
    ) -> None:
        self.guard = guard or InputGuard()
        self.retry = retry or RetryPolicy()
        self.normalizer = normalizer or OutputNormalizer()
        self.timeout = timeout  # 单次执行的超时秒数，None 表示不设超时
        self.on_retry = on_retry

    async def execute(self, tool: Tool, params: BaseModel) -> ToolResult:
        """执行工具，应用防护/重试/归一化。

        params 直接透传；对含 file 内容的参数，网关会先做输入防护。
        """
        # 1) 输入防护：对 WriteFile 等含大内容的参数截断
        guarded_params = self._guard_params(tool, params)

        # 2) 带指数退避重试的执行
        last_exc: BaseException | None = None
        for attempt in range(self.retry.max_attempts):
            try:
                coro = self._run_single(tool, guarded_params)
                if self.timeout is not None:
                    result = await asyncio.wait_for(coro, timeout=self.timeout)
                else:
                    result = await coro
                return self.normalizer.normalize(result)
            except self.retry.retryable_exceptions as e:
                last_exc = e
                is_last = attempt == self.retry.max_attempts - 1
                if is_last:
                    break
                delay = self.retry.delay_for(attempt)
                if self.on_retry:
                    self.on_retry(tool.name, attempt + 1, e)
                log.warning(
                    "tool %s attempt %d/%d failed (%s), retrying in %.1fs",
                    tool.name, attempt + 1, self.retry.max_attempts, e, delay,
                )
                await asyncio.sleep(delay)
            except Exception as e:  # 永久性错误，不重试
                return ToolResult(output=f"Tool '{tool.name}' error: {e}", is_error=True)

        return ToolResult(
            output=f"Tool '{tool.name}' failed after {self.retry.max_attempts} attempts: {last_exc}",
            is_error=True,
        )

    async def _run_single(self, tool: Tool, params: BaseModel) -> ToolResult:
        return await tool.execute(params)

    def _guard_params(self, tool: Tool, params: BaseModel) -> BaseModel:
        """对含超大内容/文件参数的调用做输入防护。"""
        # 常见的文件内容字段名（WriteFile/EditFile 等）
        content_fields = ("content", "new_string", "text")
        for field_name in content_fields:
            if hasattr(params, field_name):
                value = getattr(params, field_name)
                if isinstance(value, str):
                    safe, truncated = self.guard.guard_content(value)
                    if truncated:
                        try:
                            setattr(params, field_name, safe)
                        except Exception:
                            pass
        return params
