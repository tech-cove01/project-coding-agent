from __future__ import annotations

import asyncio
import os
import platform
import re
import shlex

from pydantic import BaseModel, Field

from coding_agent.tools.base import Tool, ToolResult

MAX_TIMEOUT = 600

_IS_WINDOWS = platform.system() == "Windows"

if _IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _CREATE_NEW_PROCESS_GROUP = 0x00000200

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

    class _JobObjectBasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JobObjectExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JobObjectBasicLimitInformation),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _kernel32.CloseHandle.restype = wintypes.BOOL

    class _JobObject:
        def __init__(self) -> None:
            self.handle = _kernel32.CreateJobObjectW(None, None)
            info = _JobObjectExtendedLimitInformation()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            _kernel32.SetInformationJobObject(
                self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)
            )

        def assign(self, pid: int) -> None:
            _kernel32.AssignProcessToJobObject(self.handle, pid)

        def terminate(self) -> None:
            _kernel32.TerminateJobObject(self.handle, 1)

        def close(self) -> None:
            if self.handle:
                _kernel32.CloseHandle(self.handle)
                self.handle = None

# 特殊命令的退出码语义映射
# 这些命令的 exit code 1 不代表错误，只有 >= 阈值才算真正的错误
# 例如 grep 返回 1 仅表示"没有匹配行"，不是执行出错
_COMMAND_ERROR_THRESHOLDS: dict[str, int] = {
    "grep": 2,   # exit 1 = 没有匹配到内容
    "egrep": 2,
    "fgrep": 2,
    "rg": 2,     # ripgrep，与 grep 语义一致
    "diff": 2,   # exit 1 = 文件内容有差异
    "find": 2,   # exit 1 = 部分成功（如权限不足跳过某些目录）
    "test": 2,   # exit 1 = 条件为假
    "[": 2,      # test 的别名形式
}


def _extract_last_command_name(command: str) -> str | None:
    """从命令字符串中提取最后一个管道段的基础命令名。

    管道中最后一个命令决定了整体退出码，所以只看最后一段。
    例如 "cat file | grep pattern" → "grep"
    """
    # 按管道符拆分，取最后一段
    last_segment = command.rsplit("|", maxsplit=1)[-1].strip()
    if not last_segment:
        return None

    # 跳过常见的环境变量赋值前缀，如 "FOO=bar command ..."
    # 也要处理 sudo/env 等包装命令
    try:
        tokens = shlex.split(last_segment)
    except ValueError:
        # shlex 解析失败时，用简单的空格分割兜底
        tokens = last_segment.split()

    for token in tokens:
        # 跳过形如 VAR=VALUE 的环境变量赋值
        if re.match(r"^[A-Za-z_]\w*=", token):
            continue
        # 取 basename（去掉路径前缀，如 /usr/bin/grep → grep）
        base = token.rsplit("/", maxsplit=1)[-1]
        return base

    return None


def _interpret_exit_code(command: str, exit_code: int) -> bool:
    """根据命令语义判断退出码是否代表真正的错误。

    返回 True 表示是错误，False 表示不是错误。
    """
    if exit_code == 0:
        return False

    cmd_name = _extract_last_command_name(command)
    if cmd_name and cmd_name in _COMMAND_ERROR_THRESHOLDS:
        # 只有退出码 >= 阈值时才视为错误
        return exit_code >= _COMMAND_ERROR_THRESHOLDS[cmd_name]

    # 默认行为：非零退出码即为错误
    return True


class Params(BaseModel):
    command: str = Field(description="Shell command to execute")
    timeout: int = Field(default=120, description="Timeout in seconds (max 600)")


class Bash(Tool):
    name = "Bash"
    description = "Execute a shell command and return stdout and stderr."
    params_model = Params
    category = "command"


    async def execute(self, params: Params) -> ToolResult:
        timeout = min(params.timeout, MAX_TIMEOUT)

        job = _JobObject() if _IS_WINDOWS else None
        creation_flags = _CREATE_NEW_PROCESS_GROUP if _IS_WINDOWS else 0
        env = {k: os.environ[k] for k in ("PATH", "SystemRoot", "TEMP", "TMP") if k in os.environ} if _IS_WINDOWS else None

        try:
            proc = await asyncio.create_subprocess_shell(
                params.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=os.getcwd(),
                env=env,
                creationflags=creation_flags,
            )
            if job:
                job.assign(proc.pid)
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            if job:
                job.terminate()
            else:
                proc.kill()
            await proc.wait()
            if job:
                job.close()
            return ToolResult(output=f"Error: command timed out after {timeout}s", is_error=True)
        except Exception as e:
            if job:
                job.close()
            return ToolResult(output=f"Error executing command: {e}", is_error=True)

        if job:
            job.close()

        parts: list[str] = []
        if stdout:
            parts.append(f"STDOUT:\n{stdout.decode(errors='replace')}")
        if stderr:
            parts.append(f"STDERR:\n{stderr.decode(errors='replace')}")
        if not parts:
            parts.append("(no output)")

        output = "\n".join(parts)
        return ToolResult(
            output=output,
            is_error=_interpret_exit_code(params.command, proc.returncode or 0),
        )

