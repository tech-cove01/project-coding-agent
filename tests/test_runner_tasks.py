"""runner 任务加载去重逻辑测试。"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from benchmarks import runner


def _write_yaml(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


@pytest.fixture
def task_env(monkeypatch, tmp_path: Path):
    """构造临时 tasks 目录 + 回归文件，隔离 runner 的真实路径。"""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()

    # 3 个原始任务
    _write_yaml(tasks_dir / "alpha.yaml", {
        "name": "alpha", "description": "task alpha", "domain": "coding",
        "verify": [{"type": "file_exists", "path": "{{workdir}}/a.py"}],
    })
    _write_yaml(tasks_dir / "beta.yaml", {
        "name": "beta", "description": "task beta", "domain": "coding",
        "verify": [{"type": "file_exists", "path": "{{workdir}}/b.py"}],
    })
    _write_yaml(tasks_dir / "gamma.yaml", {
        "name": "gamma", "description": "task gamma", "domain": "file-io",
        "verify": [{"type": "file_exists", "path": "{{workdir}}/g.txt"}],
    })

    # 回归文件：beta 与 tasks 重名（应去重），delta 只在回归里（应保留）
    reg_file = tmp_path / "regression.yaml"
    _write_yaml(reg_file, {"tasks": [
        {"name": "beta", "description": "beta old", "domain": "coding", "last_failure": "x",
         "verify": [{"type": "file_exists", "path": "{{workdir}}/b.py"}]},
        {"name": "delta", "description": "delta only in regression", "domain": "debugging", "last_failure": "y",
         "verify": [{"type": "file_exists", "path": "{{workdir}}/d.py"}]},
    ]})

    # load_tasks 的 tasks_dir 是默认参数（定义时绑定），需显式传参；
    # 回归文件用模块级变量，monkeypatch 生效
    monkeypatch.setattr(runner, "_REGRESSION_FILE", reg_file)
    return tasks_dir, reg_file


def test_load_tasks_deduplicates_regression(task_env) -> None:
    tasks_dir, _ = task_env
    tasks = runner.load_tasks(tasks_dir=tasks_dir)
    names = [t["name"] for t in tasks]
    # alpha, beta, gamma (tasks) + delta (仅回归)，beta 去重后不重复
    assert names == ["alpha", "beta", "gamma", "delta"]
    # beta 应采用 tasks 版本（描述更新），而非回归里的旧描述
    beta = next(t for t in tasks if t["name"] == "beta")
    assert beta["description"] == "task beta"


def test_load_tasks_marks_regression_only_task(task_env) -> None:
    tasks_dir, _ = task_env
    tasks = runner.load_tasks(tasks_dir=tasks_dir)
    delta = next(t for t in tasks if t["name"] == "delta")
    assert delta["description"] == "delta only in regression"


def test_load_tasks_empty_regression(monkeypatch, tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _write_yaml(tasks_dir / "alpha.yaml", {
        "name": "alpha", "description": "task alpha", "domain": "coding",
        "verify": [{"type": "file_exists", "path": "{{workdir}}/a.py"}],
    })
    reg_file = tmp_path / "regression.yaml"
    _write_yaml(reg_file, {"tasks": []})

    monkeypatch.setattr(runner, "_REGRESSION_FILE", reg_file)

    tasks = runner.load_tasks(tasks_dir=tasks_dir)
    assert [t["name"] for t in tasks] == ["alpha"]
