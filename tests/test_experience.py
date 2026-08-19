"""自进化评测：失败经验库（_experience.py）测试。"""
from __future__ import annotations

from pathlib import Path

import pytest

from benchmarks import _experience as exp


def _make_task(**overrides) -> dict:
    task: dict = {
        "name": "fibonacci",
        "label": "斐波那契数列",
        "description": (
            "编写 Python 脚本 fib.py，定义函数 fib(n) 返回斐波那契数列第 n 项"
            "（f(0)=0, f(1)=1），并包含自测校验 fib(10)==55。"
        ),
        "difficulty": "easy",
        "domain": "coding",
        "verify": [
            {"type": "file_exists", "path": "{{workdir}}/fib.py"},
            {"type": "file_contains", "path": "{{workdir}}/fib.py", "pattern": "def fib"},
            {"type": "command_succeeds", "command": "python {{workdir}}/fib.py"},
        ],
    }
    task.update(overrides)
    return task


@pytest.fixture
def isolated(monkeypatch, tmp_path: Path):
    """把经验库文件指向临时路径，并在测试后清理。"""
    exp_file = tmp_path / "experience.yaml"
    monkeypatch.setattr(exp, "_EXPERIENCE_FILE", exp_file)
    yield exp_file
    if exp_file.exists():
        exp_file.unlink()


# ---------------------------------------------------------------------------
# 沉淀
# ---------------------------------------------------------------------------

def test_add_experience_creates_candidate(isolated: Path) -> None:
    task = _make_task()
    entry = exp.add_experience(task, "缺少文本: def fib")

    assert entry["task_name"] == "fibonacci"
    assert entry["domain"] == "coding"
    assert entry["status"] == exp.STATUS_CANDIDATE  # 默认候选，待验证
    assert "def fib" in entry["failure_snapshot"]
    assert entry["lesson"]
    assert entry["strategy"]
    assert entry["keywords"]
    assert isolated.exists()


def test_add_experience_deduplicates_by_task_name(isolated: Path) -> None:
    exp.add_experience(_make_task(), "旧失败")
    exp.add_experience(_make_task(), "新失败")

    loaded = exp._load()
    assert len(loaded) == 1
    assert loaded[0]["failure_snapshot"] == "新失败"


def test_clear_experience_library(isolated: Path) -> None:
    exp.add_experience(_make_task(), "失败")
    assert isolated.exists()
    exp.clear_experience_library()
    assert not isolated.exists()
    assert exp._load() == []


# ---------------------------------------------------------------------------
# 状态机：候选 -> 转正 / 丢弃
# ---------------------------------------------------------------------------

def test_confirm_promotes_candidate_to_valid(isolated: Path) -> None:
    exp.add_experience(_make_task(), "失败")
    assert exp._load()[0]["status"] == exp.STATUS_CANDIDATE

    # 重测成功 -> 转正
    assert exp.confirm_experience("fibonacci") is True
    assert exp._load()[0]["status"] == exp.STATUS_VALID


def test_confirm_returns_false_when_no_candidate(isolated: Path) -> None:
    # 没有沉淀任何经验
    assert exp.confirm_experience("fibonacci") is False


def test_discard_removes_candidate(isolated: Path) -> None:
    exp.add_experience(_make_task(), "失败")
    assert len(exp._load()) == 1

    # 重测失败 -> 丢弃候选
    assert exp.discard_experience("fibonacci") is True
    assert exp._load() == []


def test_discard_returns_false_when_no_candidate(isolated: Path) -> None:
    exp.add_experience(_make_task(), "失败")
    exp.confirm_experience("fibonacci")  # 已转正为 valid，不是 candidate
    # 对 valid 经验调用 discard，无候选可丢弃
    assert exp.discard_experience("fibonacci") is False
    assert len(exp._load()) == 1  # valid 经验保留


def test_discard_only_affects_that_task(isolated: Path) -> None:
    exp.add_experience(_make_task(), "fib 失败")  # fibonacci 候选
    exp.add_experience(_make_task(name="other", description="其他任务。"), "其他失败")

    exp.discard_experience("fibonacci")
    remaining = exp._load()
    assert len(remaining) == 1
    assert remaining[0]["task_name"] == "other"


# ---------------------------------------------------------------------------
# 检索（只注入 valid，过滤 candidate）
# ---------------------------------------------------------------------------

def test_retrieve_only_injects_valid(isolated: Path) -> None:
    # 沉淀两条相似任务经验，都保持 candidate
    exp.add_experience(
        _make_task(name="factorial", description="编写 Python 脚本 fact.py，定义函数 fact(n) 返回阶乘。"),
        "失败1",
    )
    exp.add_experience(
        _make_task(name="sum_even", description="编写 Python 脚本 sum_even.py，定义函数 sum_even()。"),
        "失败2",
    )

    # candidate 不应被检索到（避免错误反思污染注入）
    hits = exp.retrieve(_make_task())
    assert hits == []

    # 转正 factorial 后，它才可被检索注入
    exp.confirm_experience("factorial")
    hits = exp.retrieve(_make_task())
    assert len(hits) == 1
    assert hits[0]["task_name"] == "factorial"


def test_retrieve_hits_similar_domain_and_keywords(isolated: Path) -> None:
    # 沉淀并转正一条"相似任务"经验：同 domain(coding)、关键词重叠
    similar_task = _make_task(
        name="factorial",
        description=(
            "编写 Python 脚本 fact.py，定义函数 fact(n) 返回阶乘，"
            "脚本底部含自测。"
        ),
    )
    exp.add_experience(similar_task, "命令失败: python fact.py")
    exp.confirm_experience("factorial")  # 转正

    hits = exp.retrieve(_make_task())
    assert hits, "转正后的相似任务经验应被检索到"
    assert hits[0]["task_name"] == "factorial"
    assert hits[0]["score"] > 0


def test_retrieve_skips_self(isolated: Path) -> None:
    # 沉淀并转正 fibonacci 自己的经验
    exp.add_experience(_make_task(), "fibonacci 失败")
    exp.confirm_experience("fibonacci")

    hits = exp.retrieve(_make_task())
    names = [h["task_name"] for h in hits]
    assert "fibonacci" not in names


def test_retrieve_returns_empty_for_unrelated(isolated: Path) -> None:
    exp.add_experience(_make_task(), "失败")  # coding 领域
    exp.confirm_experience("fibonacci")

    # 完全无关任务：不同领域 + 无关键词重叠
    unrelated = _make_task(
        name="web_ui",
        description="构建一个 React 组件，使用 CSS 布局，添加动画效果。",
        domain="frontend",
    )
    hits = exp.retrieve(unrelated)
    assert hits == []


# ---------------------------------------------------------------------------
# 注入
# ---------------------------------------------------------------------------

def test_build_injection_prompt_only_injects_valid(isolated: Path) -> None:
    exp.add_experience(
        _make_task(name="sum_even", description="编写函数 sum_even 计算偶数之和。"),
        "命令失败",
    )
    # candidate 不注入
    assert exp.build_injection_prompt(_make_task()) == ""

    # 转正后注入
    exp.confirm_experience("sum_even")
    prompt = exp.build_injection_prompt(_make_task())
    assert "【历史经验参考】" in prompt
    assert "策略" in prompt


def test_build_injection_prompt_empty_when_no_hits(isolated: Path) -> None:
    # 经验库里没有任何内容 -> 返回空串
    assert exp.build_injection_prompt(_make_task()) == ""


def test_top_k_limits_results(isolated: Path) -> None:
    # 沉淀 5 条 coding 领域相似任务经验，全部转正
    for i in range(5):
        name = f"task_{i}"
        exp.add_experience(
            _make_task(
                name=name,
                description=f"编写 Python 脚本 script_{i}.py，定义函数 fn_{i}() 返回结果。",
            ),
            "失败",
        )
        exp.confirm_experience(name)
    hits = exp.retrieve(_make_task(), top_k=2)
    assert len(hits) == 2


# ---------------------------------------------------------------------------
# 经验提炼（_distill）
# ---------------------------------------------------------------------------

def test_distill_file_missing_gives_concrete_lesson() -> None:
    task = _make_task()  # 要求创建 fib.py
    lesson, strategy = exp._distill(task, "文件缺失: /tmp/x/fib.py")
    assert "fib.py" in lesson  # 提到具体缺失的文件
    assert "创建" in strategy  # 有可操作策略


def test_distill_syntax_error_gives_rewrite_advice() -> None:
    task = _make_task()
    lesson, strategy = exp._distill(
        task,
        "命令失败(rc=1): python fib.py | stderr: SyntaxError: unmatched ')'",
    )
    assert "语法" in lesson
    assert "重写" in strategy or "语法正确" in strategy


def test_distill_fallback_when_unrecognized() -> None:
    task = _make_task()
    lesson, strategy = exp._distill(task, "某未知错误")
    # 无具体模式时退回泛化提示，但仍有内容
    assert lesson
    assert strategy


def test_distill_mentions_required_files() -> None:
    # 任务要求 fib.py，失败详情没提文件，策略里也应提醒创建 fib.py
    task = _make_task()
    lesson, strategy = exp._distill(task, "命令失败(rc=1): python fib.py")
    assert "fib.py" in strategy or "创建文件 fib.py" in strategy
