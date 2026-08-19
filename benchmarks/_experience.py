"""自进化评测：失败经验库（沉淀 + 相似检索 + 注入）。

解决"失败案例二次跑还会出错"的核心：当前回归机制只把失败任务重新跑一遍
（regression.yaml 的 last_failure），但没把历史教训注入给 Agent。本模块：

  1. 沉淀（distill）：任务失败时，把失败详情整理成结构化经验写入 experience.yaml
  2. 检索（retrieve）：跑任务前，按"领域 + 关键词相似度"检索历史相关经验
  3. 注入（inject）：把命中的经验格式化后拼进 Agent 的 prompt

设计取舍（面试/演示友好）：
  - 无重依赖：用"领域 + 关键词"的确定性相似度，不引入向量库/嵌入模型
  - 可测试：纯函数，不依赖真实 LLM
  - 可替换：后续想升级为真正的向量检索，只需替换 similarity() 与 embed() 的实现
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

_EXPERIENCE_FILE = Path(__file__).resolve().parent / "experience.yaml"
_DEFAULT_TOP_K = 3
# 同领域强匹配约 0.5+，跨领域通常 < 0.3；阈值取 0.3 过滤串领域噪音
_DEFAULT_MIN_SCORE = 0.3

# 从任务 description / 校验失败详情里抽取"关键词"的简单切词（英文为主）
_WORD_RE = re.compile(r"[a-z_][a-z0-9_]*", re.IGNORECASE)
# 领域关键词 -> 相关词，用于跨任务泛化匹配（避免死板完全相等）
_DOMAIN_KEYWORDS: dict[str, set[str]] = {
    "coding": {"function", "def", "script", "python", "return", "list", "dict"},
    "debugging": {"syntax", "error", "fix", "traceback", "bug", "exception"},
    "refactor": {"refactor", "rename", "split", "class", "oop", "module"},
    "file-io": {"file", "read", "write", "append", "log", "open"},
    "testing": {"test", "assert", "pytest", "valueerror", "suite"},
    "data-processing": {"csv", "read", "data", "average", "parse", "pandas"},
}


# ---------------------------------------------------------------------------
# 关键词提取与相似度
# ---------------------------------------------------------------------------

def _tokens(text: str) -> set[str]:
    """把文本切成小写 token 集合。"""
    return {m.group(0).lower() for m in _WORD_RE.finditer(text or "")}


def _task_domain(task: dict[str, Any]) -> str:
    return str(task.get("domain", "")).strip().lower()


def similarity(task: dict[str, Any], exp: dict[str, Any]) -> float:
    """计算任务与经验的相关度分数 [0, 1]。

    检索策略（关键：避免串领域误命中）：
      - 领域是**强约束**：同领域 domain_score=1.0；不同领域若语义相关给 0.5，否则 0
      - 关键词重合（0~1，权重 0.35）：任务描述 vs 经验 lesson/strategy/task_name
      - 校验目标重合（0~1，权重 0.15）：任务描述 vs 失败快照
    整体分数 = 0.5*domain + 0.35*kw + 0.15*snap。
    不同领域且关键词重合低的，分数自然很低，会被 min_score 过滤掉。
    """
    domain = _task_domain(task)
    exp_domain = str(exp.get("domain", "")).lower()

    # 1) 领域匹配（强约束）
    if domain and exp_domain and domain == exp_domain:
        domain_score = 1.0
    elif _domains_related(domain, exp_domain):
        domain_score = 0.5
    else:
        domain_score = 0.0

    # 2) 关键词重合：任务描述 vs 经验教训/策略/任务名
    #    用"经验侧命中词占任务侧词的比重"，并对经验侧稀有词加权，提高区分度
    task_tokens = _tokens(str(task.get("description", "")))
    exp_tokens = _tokens(f"{exp.get('lesson', '')} {exp.get('strategy', '')} {exp.get('task_name', '')}")
    if task_tokens:
        inter = len(task_tokens & exp_tokens)
        # 只有超过阈值（2个词）才给分，避免偶然单个词重合
        kw_overlap = inter / max(1, len(task_tokens)) if inter >= 2 else 0.0
    else:
        kw_overlap = 0.0

    # 3) 校验目标重合：任务描述 vs 失败快照（弱信号，起辅助作用）
    snap_tokens = _tokens(str(exp.get("failure_snapshot", "")))
    snap_overlap = len(task_tokens & snap_tokens) / max(1, len(task_tokens)) if task_tokens else 0.0

    return round(0.5 * domain_score + 0.35 * kw_overlap + 0.15 * snap_overlap, 4)


def _domains_related(a: str, b: str) -> bool:
    """判断两个领域是否语义相关（通过领域关键词表）。"""
    if not a or not b:
        return False
    ka = _DOMAIN_KEYWORDS.get(a, set())
    kb = _DOMAIN_KEYWORDS.get(b, set())
    return bool(ka & kb)


# ---------------------------------------------------------------------------
# 经验库读写
# ---------------------------------------------------------------------------

def _load() -> list[dict[str, Any]]:
    if not _EXPERIENCE_FILE.exists():
        return []
    with open(_EXPERIENCE_FILE, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return list(data.get("experiences", []))


def _save(experiences: list[dict[str, Any]]) -> None:
    _EXPERIENCE_FILE.write_text(
        yaml.safe_dump(
            {"experiences": experiences},
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        ),
        encoding="utf-8",
    )


# 经验状态机：避免错误反思污染记忆库
#   candidate 失败后沉淀的反思，尚未验证 -> 不参与检索注入
#   valid     重测通过，反思被证实有效 -> 参与检索注入
#   discarded 重测失败，反思被证伪      -> 丢弃，不入库
STATUS_CANDIDATE = "candidate"
STATUS_VALID = "valid"
STATUS_DISCARDED = "discarded"


def add_experience(
    task: dict[str, Any],
    detail: str,
    *,
    lesson: str | None = None,
    strategy: str | None = None,
) -> dict[str, Any]:
    """把失败任务沉淀成一条**候选**反思，写入经验库（status=candidate）。

    反思需要经过"下一次重测"验证才转正（confirm_experience），否则被丢弃
    （discard_experience）。候选状态不参与检索注入，避免错误反思污染 Agent。

    参数：
      task    : 失败任务（含 name/domain/description/verify）
      detail  : 验证器失败详情（runner 里 add_to_regression 的 detail）
      lesson  : 提炼出的教训（可选，缺省用启发式从失败详情提炼）
      strategy: 正确策略（可选，缺省用启发式从失败详情提炼）
    返回：
      写入的经验 dict（status=candidate）
    """
    name = str(task.get("name", "unnamed"))
    detail = (detail or "").strip()[:1000]

    # 缺省 lesson / strategy 用启发式从失败详情提炼具体可操作的教训
    if not lesson or not strategy:
        auto_lesson, auto_strategy = _distill(task, detail)
        lesson = lesson or auto_lesson
        strategy = strategy or auto_strategy

    entry: dict[str, Any] = {
        "task_name": name,
        "domain": _task_domain(task),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "status": STATUS_CANDIDATE,
        "failure_snapshot": detail,
        "lesson": lesson[:300],
        "strategy": strategy[:300],
        "keywords": sorted(_tokens(f"{name} {lesson} {strategy}"))[:20],
    }

    experiences = _load()
    # 去重：同一任务名已存在则更新为新的候选（覆盖旧的未验证/已验证状态）
    for i, existing in enumerate(experiences):
        if existing.get("task_name") == name:
            experiences[i] = entry
            _save(experiences)
            return entry
    experiences.append(entry)
    _save(experiences)
    return entry


def confirm_experience(task_name: str) -> bool:
    """重测成功时调用：把该任务的 candidate 反思转正为 valid。

    返回 True 表示有候选被转正，False 表示无候选可转正。
    """
    name = str(task_name)
    experiences = _load()
    changed = False
    for exp in experiences:
        if exp.get("task_name") == name and exp.get("status") == STATUS_CANDIDATE:
            exp["status"] = STATUS_VALID
            exp["updated_at"] = datetime.now().isoformat(timespec="seconds")
            changed = True
    if changed:
        _save(experiences)
    return changed


def discard_experience(task_name: str) -> bool:
    """重测失败时调用：丢弃该任务的 candidate 反思（证伪，不入库）。

    返回 True 表示丢弃了候选，False 表示无候选可丢弃。
    """
    name = str(task_name)
    experiences = _load()
    remaining = [e for e in experiences if not (e.get("task_name") == name and e.get("status") == STATUS_CANDIDATE)]
    if len(remaining) != len(experiences):
        _save(remaining)
        return True
    return False


def _is_injectable(exp: dict[str, Any]) -> bool:
    """只有已验证有效（valid）的经验才可注入。"""
    return exp.get("status") == STATUS_VALID


def _verify_summary(task: dict[str, Any]) -> str:
    """把任务的验证器 spec 压缩成一行可读摘要。"""
    verify = task.get("verify") or []
    parts = []
    for spec in verify:
        t = spec.get("type", "?")
        if t == "file_exists":
            parts.append("文件存在")
        elif t in ("file_contains", "output_contains"):
            parts.append(f"包含[{spec.get('pattern', '')}]")
        elif t == "command_succeeds":
            parts.append(f"命令[{spec.get('command', '')}]退出码0")
        elif t == "dir_created":
            parts.append("目录存在")
        else:
            parts.append(t)
    return "、".join(parts) if parts else "通过校验器"


# 从失败详情里识别的失败模式
_MISSING_FILE = "文件缺失"
_MISSING_TEXT = "缺少文本"
_MISSING_REGEX = "未匹配正则"
_MISSING_DIR = "目录缺失"
_CMD_FAIL = "命令失败"
_CMD_TIMEOUT = "命令超时"
_CMD_EXC = "命令异常"
_SYNTAX_HINTS = ("SyntaxError", "unmatched", "unterminated", "invalid syntax", "IndentationError", "trailing")
_STDERR_HINTS = ("Traceback", "FileNotFoundError", "ModuleNotFoundError", "NameError", "TypeError", "IndexError")


def _distill(task: dict[str, Any], detail: str) -> tuple[str, str]:
    """从失败详情提炼**具体可操作**的教训与策略（启发式，不依赖 LLM）。

    失败 detail 由 _verifiers 拼接，形如：
      "文件缺失: /tmp/x/merge.py | 命令失败(rc=2): python merge.py | stderr: ..."
    解析各失败片段，映射到可执行的修复方向。
    返回 (lesson, strategy)。
    """
    name = str(task.get("name", "unnamed"))
    lessons: list[str] = []
    strategies: list[str] = []
    # 任务要求创建/包含的关键产物名（从 verify spec 里提取文件名）
    required_files = _required_files(task)
    required_patterns = _required_patterns(task)

    for part in detail.split(" | "):
        p = part.strip()
        if p.startswith(_MISSING_FILE):
            path = p.replace(_MISSING_FILE, "").strip()
            fname = os.path.basename(path.strip())
            lessons.append(f"任务要求的文件 {fname} 未生成")
            strategies.append(f"必须先在当前目录创建文件 {fname}（可先运行 python 生成或直接用写文件工具写入）")
        elif p.startswith(_MISSING_TEXT):
            pat = p.replace(_MISSING_TEXT, "").strip()
            lessons.append(f"文件内容缺少要求的关键内容：{pat}")
            strategies.append(f"在对应文件中写入/包含 '{pat}'（注意与任务要求完全一致）")
        elif p.startswith(_MISSING_REGEX):
            pat = p.replace(_MISSING_REGEX, "").strip()
            lessons.append(f"文件内容未匹配要求的关键模式：{pat}")
            strategies.append(f"在对应文件中加入能匹配 '{pat}' 的代码/文本")
        elif p.startswith(_MISSING_DIR):
            path = p.replace(_MISSING_DIR, "").strip()
            lessons.append(f"任务要求的目录 {path} 未创建")
            strategies.append(f"必须创建目录 {path}")
        elif p.startswith(_CMD_TIMEOUT):
            lessons.append("命令执行超时（可能死循环或脚本运行过慢）")
            strategies.append("确保脚本能在超时时间内正常结束，避免死循环")
        elif p.startswith(_CMD_EXC):
            lessons.append("命令执行异常")
            strategies.append("检查并确保命令/脚本能正常运行")
        elif p.startswith(_CMD_FAIL):
            # 命令失败：进一步看是否有语法错误或运行时错误
            cmd = p.replace(_CMD_FAIL, "").split("|")[0].strip()
            if any(h in detail for h in _SYNTAX_HINTS):
                lessons.append(f"生成的代码存在语法错误（{cmd}）")
                strategies.append("请重写代码，确保语法正确（括号/引号/缩进成对）、可直接运行")
            elif any(h in detail for h in _STDERR_HINTS):
                lessons.append(f"脚本运行时报错（{cmd}）")
                strategies.append("检查运行报错（如文件缺失、导入缺失、名称错误），修复后确保脚本成功运行")
            else:
                lessons.append(f"命令运行未成功（{cmd}）")
                strategies.append("确保命令成功执行并返回退出码 0")

    # 若完全没解析出具体教训，退回泛化提示
    if not lessons:
        lessons.append(f"任务 '{name}' 未通过")
        strategies.append(f"重新按任务描述实现，并确保通过验证器：{_verify_summary(task)}")
    elif not strategies:
        strategies.append(f"重新按任务描述实现，并确保通过验证器：{_verify_summary(task)}")

    # 附加任务级提醒：如果任务要求某些文件但被漏掉，显式提示
    if required_files:
        extra = "；".join(f"请确保创建文件 {f}" for f in required_files)
        strategies.append(extra)

    return "；".join(dict.fromkeys(lessons)), "；".join(dict.fromkeys(strategies))


def _required_files(task: dict[str, Any]) -> list[str]:
    """从 verify spec 里提取任务要求的文件名（用于提醒 agent 不要漏建文件）。"""
    files: list[str] = []
    for spec in task.get("verify") or []:
        t = spec.get("type", "")
        if t in ("file_exists", "file_contains", "command_succeeds"):
            path = spec.get("path") or spec.get("command") or ""
            # 提取文件名（含 .py/.csv/.txt 等）
            fname = os.path.basename(path.replace("{{workdir}}", "").strip())
            if fname and "." in fname:
                files.append(fname)
    # 去重且保持顺序
    return list(dict.fromkeys(files))


def _required_patterns(task: dict[str, Any]) -> list[str]:
    """从 verify spec 里提取要求包含的关键文本。"""
    pats: list[str] = []
    for spec in task.get("verify") or []:
        if spec.get("type") in ("file_contains", "output_contains"):
            pats.append(str(spec.get("pattern", "")))
    return [p for p in dict.fromkeys(pats) if p]


# ---------------------------------------------------------------------------
# 检索与注入
# ---------------------------------------------------------------------------

def retrieve(
    task: dict[str, Any],
    *,
    top_k: int = _DEFAULT_TOP_K,
    min_score: float = _DEFAULT_MIN_SCORE,
) -> list[dict[str, Any]]:
    """检索与 task 相关的历史失败经验，按相似度降序。

    返回已附加 'score' 字段的经验列表（≤ top_k 条，且 score ≥ min_score）。
    """
    experiences = _load()
    if not experiences:
        return []
    scored = []
    for exp in experiences:
        if exp.get("task_name") == task.get("name"):
            continue  # 跳过"自己"的历史经验（本次刚重新跑，不需要提醒自己上次怎么失败）
        if not _is_injectable(exp):
            continue  # 只注入已验证有效的经验，过滤候选/已丢弃反思
        score = similarity(task, exp)
        if score >= min_score:
            scored.append((score, {**exp, "score": score}))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [exp for _, exp in scored[:top_k]]


def build_injection_prompt(task: dict[str, Any]) -> str:
    """为任务生成"历史经验参考"段落（无命中返回空串）。

    拼到任务 description 之后，作为给 Agent 的参考，而非硬约束。
    """
    hits = retrieve(task)
    if not hits:
        return ""
    lines = ["", "【历史经验参考】（来自之前评测失败案例，仅作参考，以任务要求为准）"]
    for i, exp in enumerate(hits, 1):
        lines.append(
            f"{i}. [{exp['domain'] or '未知领域'}] {exp['task_name']}："
            f"{exp['lesson']} 策略：{exp['strategy']}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 便捷：清空经验库（测试用）
# ---------------------------------------------------------------------------

def clear_experience_library() -> None:
    """清空经验库（主要用于测试）。"""
    if _EXPERIENCE_FILE.exists():
        _EXPERIENCE_FILE.unlink()
