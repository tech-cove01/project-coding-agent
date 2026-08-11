from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from coding_agent.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from coding_agent.cache import FileCache
from coding_agent.tools.file_state_cache import FileStateCache


class Params(BaseModel):
    file_path: str = Field(description="Path to the file to write")
    content: str = Field(description="Content to write to the file")


class WriteFile(Tool):
    name = "WriteFile"
    description = (
        "Write content to a file, creating parent directories if needed. Overwrites existing files.\n"
        "You MUST read existing files with ReadFile before overwriting them. This tool will fail otherwise."
    )
    params_model = Params
    category = "write"


    def __init__(self, file_cache: FileCache | None = None, file_history: Any = None, file_state_cache: FileStateCache | None = None, project_root: str | None = None) -> None:
        self._cache = file_cache
        self.file_history = file_history
        self._state_cache = file_state_cache
        self._project_root = project_root


    async def execute(self, params: Params) -> ToolResult:
        if self.file_history is not None:
            self.file_history.track_edit(params.file_path)

        path = Path(params.file_path)

        # 记忆目录写入安全扫描：记忆会长期注入后续会话，必须先拦截风险内容
        scan_err = self._scan_memory_write(str(path.resolve()), params.content)
        if scan_err:
            return ToolResult(output=scan_err, is_error=True)

        if self._state_cache and path.exists():
            resolved = str(path.resolve())
            ok, err_msg = self._state_cache.check(resolved)
            if not ok:
                return ToolResult(output=err_msg, is_error=True)

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(params.content, encoding="utf-8")
            if self._cache:
                self._cache.invalidate(str(path.resolve()))
            if self._state_cache:
                self._state_cache.update(str(path.resolve()))
        except Exception as e:
            return ToolResult(output=f"Error writing file: {e}", is_error=True)
        return ToolResult(output=f"Successfully wrote to {params.file_path}")

    def _scan_memory_write(self, abs_path: str, content: str) -> str:
        """若目标位于记忆目录，对写入内容做安全检查。

        返回空字符串表示通过；否则返回需要反馈给模型的拦截信息。
        project_root 未提供时无法识别项目级记忆目录，则跳过（安全降级，
        用户级记忆目录仍会检查）。
        """
        if not self._project_root:
            return ""
        from coding_agent.memory import is_auto_mem_path, MemoryWriteSanitizer

        if not is_auto_mem_path(abs_path, self._project_root):
            return ""
        result = MemoryWriteSanitizer().scan(content)
        if result.blocked:
            return result.render()
        return ""
