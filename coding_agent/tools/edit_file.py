from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from coding_agent.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from coding_agent.cache import FileCache
from coding_agent.tools.file_state_cache import FileStateCache


class Params(BaseModel):
    file_path: str = Field(description="Path to the file to edit")
    old_string: str = Field(description="The exact string to find and replace (must be unique in file)")
    new_string: str = Field(description="The replacement string")


class EditFile(Tool):
    name = "EditFile"
    description = (
        "Replace an exact string in a file. The old_string must appear exactly once in the file.\n"
        "You MUST read the file with ReadFile before editing. This tool will fail otherwise."
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
        if not path.exists():
            return ToolResult(output=f"Error: file not found: {params.file_path}", is_error=True)

        if self._state_cache:
            resolved = str(path.resolve())
            ok, err_msg = self._state_cache.check(resolved)
            if not ok:
                return ToolResult(output=err_msg, is_error=True)

        try:
            content = path.read_text(encoding="utf-8")
        except Exception as e:
            return ToolResult(output=f"Error reading file: {e}", is_error=True)

        count = content.count(params.old_string)
        if count == 0:
            return ToolResult(output="Error: old_string not found in file", is_error=True)
        if count > 1:
            return ToolResult(
                output=f"Error: old_string found {count} times, must be unique",
                is_error=True,
            )

        new_content = content.replace(params.old_string, params.new_string, 1)

        # 记忆目录写入安全扫描（扫描编辑后的完整内容，避免注入被拆开）
        scan_err = self._scan_memory_write(str(path.resolve()), new_content)
        if scan_err:
            return ToolResult(output=scan_err, is_error=True)

        try:
            path.write_text(new_content, encoding="utf-8")
            if self._cache:
                self._cache.invalidate(str(path.resolve()))
            if self._state_cache:
                self._state_cache.update(str(path.resolve()))
        except Exception as e:
            return ToolResult(output=f"Error writing file: {e}", is_error=True)

        return ToolResult(output=f"Successfully edited {params.file_path}")

    def _scan_memory_write(self, abs_path: str, content: str) -> str:
        """若目标位于记忆目录，对编辑后的完整内容做安全检查。"""
        if not self._project_root:
            return ""
        from coding_agent.memory import is_auto_mem_path, MemoryWriteSanitizer

        if not is_auto_mem_path(abs_path, self._project_root):
            return ""
        result = MemoryWriteSanitizer().scan(content)
        if result.blocked:
            return result.render()
        return ""
