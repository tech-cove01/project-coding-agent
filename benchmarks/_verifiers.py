"""自进化评测：任务验证器集合。

每个验证器接收 (spec: dict, workdir: Path, agent_output: str)，
返回 (passed: bool, detail: str)。spec 是任务 YAML 里 verify 列表中的一项。

支持的验证器类型：
    file_exists     ：指定文件存在
    file_contains   ：文件内容包含指定文本（pattern 支持正则）
    command_succeeds：运行命令退出码为 0
    output_contains ：agent 最终输出包含指定文本
    dir_created     ：指定目录存在
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


def _resolve(spec_path: str, workdir: Path) -> Path:
    """解析 spec 里的路径（支持 {{workdir}} 占位，相对路径基于 workdir）。"""
    text = spec_path.replace("{{workdir}}", str(workdir))
    p = Path(text)
    return p if p.is_absolute() else workdir / p


def verify_file_exists(spec: dict, workdir: Path, _output: str) -> tuple[bool, str]:
    path = _resolve(spec.get("path", ""), workdir)
    ok = path.exists()
    return ok, (f"文件存在: {path}" if ok else f"文件缺失: {path}")


def verify_file_contains(spec: dict, workdir: Path, _output: str) -> tuple[bool, str]:
    path = _resolve(spec.get("path", ""), workdir)
    pattern = spec.get("pattern", "")
    if not path.exists():
        return False, f"文件缺失: {path}"
    content = path.read_text(encoding="utf-8", errors="ignore")
    if spec.get("regex", False):
        ok = re.search(pattern, content) is not None
        return ok, (f"匹配正则: {pattern}" if ok else f"未匹配正则: {pattern}")
    ok = pattern in content
    return ok, (f"包含文本: {pattern}" if ok else f"缺少文本: {pattern}")


def verify_command_succeeds(spec: dict, workdir: Path, _output: str) -> tuple[bool, str]:
    cmd = spec.get("command", "").replace("{{workdir}}", str(workdir))
    timeout = float(spec.get("timeout", 30))
    # 把 workdir 注入环境变量，供命令里用 $BENCH_WORKDIR 引用，
    # 避免 Windows 反斜杠路径在 shell 字符串里被转义破坏。
    env = dict(os.environ)
    env["BENCH_WORKDIR"] = str(workdir)
    try:
        proc = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            cwd=workdir, env=env, timeout=timeout,
        )
        ok = proc.returncode == 0
        detail = f"命令成功(rc=0): {cmd}" if ok else f"命令失败(rc={proc.returncode}): {cmd}"
        if not ok and proc.stderr.strip():
            detail += f" | stderr: {proc.stderr.strip()[:200]}"
        return ok, detail
    except subprocess.TimeoutExpired:
        return False, f"命令超时: {cmd}"
    except Exception as e:  # noqa: BLE001
        return False, f"命令异常: {cmd} ({e})"


def verify_output_contains(spec: dict, _workdir: Path, agent_output: str) -> tuple[bool, str]:
    pattern = spec.get("pattern", "")
    if spec.get("regex", False):
        ok = re.search(pattern, agent_output) is not None
        return ok, (f"输出匹配正则: {pattern}" if ok else f"输出未匹配正则: {pattern}")
    ok = pattern in agent_output
    return ok, (f"输出包含: {pattern}" if ok else f"输出缺少: {pattern}")


def verify_dir_created(spec: dict, workdir: Path, _output: str) -> tuple[bool, str]:
    path = _resolve(spec.get("path", ""), workdir)
    ok = path.is_dir()
    return ok, (f"目录存在: {path}" if ok else f"目录缺失: {path}")


_VERIFIERS = {
    "file_exists": verify_file_exists,
    "file_contains": verify_file_contains,
    "command_succeeds": verify_command_succeeds,
    "output_contains": verify_output_contains,
    "dir_created": verify_dir_created,
}


def verify(spec: dict, workdir: Path, agent_output: str) -> tuple[bool, str]:
    """运行单个验证器 spec，返回 (passed, detail)。"""
    vtype = spec.get("type", "")
    fn = _VERIFIERS.get(vtype)
    if fn is None:
        return False, f"未知验证器类型: {vtype}"
    try:
        return fn(spec, workdir, agent_output)
    except Exception as e:  # noqa: BLE001
        return False, f"验证器异常({vtype}): {e}"
