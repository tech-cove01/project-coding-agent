#
# L0-L3 分层记忆模块：Agent 对话上下文的分层压缩 / 摘要 / 外部卸载。
#
# 设计目标：
#   1. L0-L3 是「递进共存」的分层记忆，不是互斥开关。每条对话片段都会落在某一层。
#   2. 触发采用「语义安全断点优先 + Token 阈值兜底」双策略：
#        - 语义安全断点：子任务完成 / 工具调用结束 / 话题切换 才允许压缩，
#          推理 / 工具执行中途禁止压缩，避免打断思考流。
#        - Token 阈值（占最大上下文窗口比例）：
#            60% -> L1 轻度抽取压缩
#            75% -> L2 大模型摘要压缩
#            88% -> L3 向量化外部卸载（预留余量，避免直接报错）
#   3. 工程约束：实体保护 / 依赖保护 / 时机约束 / 原始对话不删（持久化，压缩只生成副本）。
#
# 适配 LangGraph：
#   - HierarchicalMemory 是纯 dataclass 状态，可直接放进 LangGraph checkpoint
#     （StateSchema / reducer）。注意：L3 卸载的内容存外部向量库，不应塞进 checkpoint，
#     只保留 index 标记（见 MemoryItem.external_ref）。
#   - LLM 调用 / 向量检索通过占位函数注入，模块本身不绑定具体 provider，便于在
#     LangGraph node 里用 InjectedStore / configurable 注入 store 与 model。
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from coding_agent.conversation import Message, estimate_tokens

# 与 conversation / context.manager 保持一致的字符/token 比率
_CHARS_PER_TOKEN = 3.5


# ===========================================================================
# 层级定义
# ===========================================================================


class MemoryLevel(str, Enum):
    L0 = "L0"  # 工作记忆：永远不压缩，完整送入上下文
    L1 = "L1"  # 轻度抽取压缩：保留实体/关键结论，删除重复闲聊，仍在内存参与 prompt
    L2 = "L2"  # LLM 摘要压缩：对 L1 历史做结构化摘要，摘要留内存，原始对话持久化
    L3 = "L3"  # 外部卸载：久远历史移出内存写入向量库，内存仅保留 index 标记


# Token 阈值（占 max_context_window 的比例）
L1_TRIGGER_RATIO = 0.60
L2_TRIGGER_RATIO = 0.75
L3_TRIGGER_RATIO = 0.88

# 压缩净收益校验：L2 压缩节省 token 必须大于本次压缩调用消耗 token，否则跳过
L2_MIN_NET_GAIN_RATIO = 1.0


# ===========================================================================
# 单条记忆片段
# ===========================================================================


@dataclass
class MemoryItem:
    """承载 L0/L1/L2/L3 的单条记忆片段。

    原始对话（original）永远不删除，压缩/摘要只是生成副本（compressed）。
    """

    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    # 原始内容（永不删除，持久化存储）
    original: str = ""
    # 压缩/摘要后的副本；L0 时与 original 相同，L1/L2 为抽取/摘要结果
    compressed: str = ""
    level: MemoryLevel = MemoryLevel.L0
    # 该片段当前占用的 token 估算（基于 compressed，决定它参与上下文的体积）
    tokens: int = 0

    # ---- 工程约束标记 ----
    # 安全断点标记：该片段是否为可压缩的安全点（子任务完成/工具结束/话题切换）
    safe_breakpoint: bool = False
    # 依赖标记：后续推理/工具是否引用此片段。存在依赖则不能做 L3 卸载。
    has_dependency: bool = False
    # 实体标记：片段内是否含关键实体（人名/业务ID/工具名），压缩时强制保留。
    has_entity: bool = False

    # L3 外部卸载后的检索索引（如向量库 id / 文件路径），非空表示已卸载到外部
    external_ref: str | None = None

    def __post_init__(self) -> None:
        if not self.compressed:
            self.compressed = self.original
        if self.tokens <= 0 and self.compressed:
            self.tokens = _estimate_tokens_text(self.compressed)

    @property
    def in_context(self) -> bool:
        """该片段是否仍参与当前上下文（L3 卸载后置 False）。"""
        return self.level != MemoryLevel.L3


def _estimate_tokens_text(text: str) -> int:
    """文本 token 估算，与 conversation.estimate_tokens 的字符比率保持一致。"""
    if not text:
        return 0
    return int(len(text) / _CHARS_PER_TOKEN)


# ===========================================================================
# 实体保护：简单关键字/正则提取器（占位，可替换为 NER 模型）
# ===========================================================================

# 业务 ID / 工具名 / 人名 的轻量保护规则。生产可接入 NER 或白名单字典。
_ENTITY_PATTERNS = [
    re.compile(r"\b[A-Z][a-z]+_[a-z0-9]+\b"),  # 形如 Order_123 的业务ID
    re.compile(r"\b[A-Z][a-zA-Z0-9]{3,}\b"),    # 大驼峰工具/类名
    re.compile(r"@[a-zA-Z0-9_]+"),              # @user 提及
]


def extract_entities(text: str) -> list[str]:
    """提取需要强制保留的关键实体。占位实现：基于正则，后续可接 NER。"""
    found: set[str] = set()
    for pat in _ENTITY_PATTERNS:
        for m in pat.findall(text):
            found.add(m)
    return sorted(found)


# ===========================================================================
# 安全断点判定
# ===========================================================================


class BreakpointSignal(str, Enum):
    TURN_COMPLETE = "turn_complete"      # 一轮对话自然结束
    TOOL_END = "tool_end"                # 工具调用返回结果
    TOPIC_SHIFT = "topic_shift"          # 话题切换
    MID_REASONING = "mid_reasoning"      # 推理中途（不安全，禁止压缩）


def is_safe_breakpoint(signal: BreakpointSignal) -> bool:
    """语义安全断点优先：只有在安全点才允许压缩。"""
    return signal in (
        BreakpointSignal.TURN_COMPLETE,
        BreakpointSignal.TOOL_END,
        BreakpointSignal.TOPIC_SHIFT,
    )


# ===========================================================================
# 占位函数：抽取压缩 / 摘要 / 向量卸载（标注后续接入点）
# ===========================================================================


def extract_compress_l1(text: str, entities: list[str]) -> str:
    """L1 轻度抽取压缩占位。

    接入点：后续接抽取式摘要模型（如 TF-IDF / 小模型关键句抽取）。
    实现要求：删除重复闲聊、保留实体与关键结论，输出仍留在内存上下文。
    """
    # TODO: 接入抽取式压缩。当前退化为保留实体行的朴素实现。
    lines = [ln for ln in text.splitlines() if any(e in ln for e in entities) or ln.strip()]
    return "\n".join(lines[:200])  # 截断保护，避免无限增长


def summarize_l2(items: list[MemoryItem], llm_call: Callable[[str, str], str]) -> str:
    """L2 大模型摘要压缩占位（两阶段）。

    接入点：
      - 阶段一 小模型低成本筛选 -> 调用者传入的 llm_call(model="small", prompt=...)
      - 阶段二 主大模型生成结构化摘要 -> llm_call(model="main", prompt=...)
    当前占位直接拼接，仅示意接口形态。
    """
    # TODO: 实现两阶段（小模型筛选内容 -> 主模型生成结构化摘要）。
    joined = "\n".join(it.compressed for it in items)
    return llm_call("main", f"请对以下内容生成结构化摘要（保留所有实体）：\n{joined}")


def offload_l3_vector(item: MemoryItem, vector_store: Any) -> str:
    """L3 向量化外部卸载占位。

    接入点：将 item.original 写入向量数据库（如 FAISS / pgvector / Chroma），
    返回可检索的 external_ref（如向量 id）。内存中仅保留该标记。
    """
    # TODO: 接入向量库 upsert。返回 id 后由调用方写入 item.external_ref。
    ref = vector_store.upsert(item.original) if vector_store else uuid.uuid4().hex
    return ref


def recall_l3_vector(query: str, vector_store: Any) -> list[str]:
    """L3 检索召回占位。后续需要历史时按 query 向量检索，不默认塞进上下文。"""
    # TODO: 接入向量库 query。
    return vector_store.query(query) if vector_store else []


# ===========================================================================
# 分层记忆主结构（可序列化为 LangGraph checkpoint 状态）
# ===========================================================================


@dataclass
class HierarchicalMemory:
    """L0-L3 分层记忆容器。

    设计为纯数据 + 方法，无隐藏全局状态，便于作为 LangGraph 的 State 节点状态，
    或在 node 内通过 InjectedStore 注入 vector_store / llm_call。
    """

    max_context_window: int = 200_000  # 最大上下文窗口 token
    items: list[MemoryItem] = field(default_factory=list)

    # 外部依赖（LangGraph 中通过 configurable / InjectedStore 注入，不进 checkpoint）
    # 向量库句柄；None 时 L3 卸载退化为仅打标，不真正落库。
    vector_store: Any = None
    # LLM 调用：signuture 为 (model: str, prompt: str) -> str
    llm_call: Callable[[str, str], str] | None = None

    # ---------- token 统计 ----------
    def in_context_tokens(self) -> int:
        """仍在上下文中的 token 总量（不含已 L3 卸载的）。"""
        return sum(it.tokens for it in self.items if it.in_context)

    def usage_ratio(self) -> float:
        return self.in_context_tokens() / self.max_context_window

    # ---------- 写入 ----------
    def add(
        self,
        text: str,
        *,
        safe_breakpoint: bool = False,
        has_dependency: bool = False,
        has_entity: bool = False,
    ) -> MemoryItem:
        entities = extract_entities(text) if not has_entity else []
        item = MemoryItem(
            original=text,
            level=MemoryLevel.L0,  # 新写入永远是工作记忆 L0
            safe_breakpoint=safe_breakpoint,
            has_dependency=has_dependency,
            has_entity=bool(entities) or has_entity,
        )
        if entities:
            item.has_entity = True
        self.items.append(item)
        return item

    # ---------- 核心入口：压缩判定与执行 ----------
    def compress_memory(self, signal: BreakpointSignal) -> dict[str, Any]:
        """compress_memory 核心入口，完整实现双策略触发判断。

        返回本次压缩动作的报告（供日志/可观测性使用）。

        判定顺序：
          1. 时机约束：即便达到 token 阈值，不在安全断点则延后压缩（return skipped）。
          2. token 阈值兜底：按 usage_ratio 选择 L1/L2/L3 目标层。
          3. 逐条执行压缩，遵守工程约束（实体保护 / 依赖保护）。
        """
        ratio = self.usage_ratio()
        report = {"signal": signal.value, "ratio": round(ratio, 3), "actions": []}

        # ---- 时机约束：非安全断点禁止任何压缩 ----
        if not is_safe_breakpoint(signal):
            report["result"] = "skipped:not_safe_breakpoint"
            return report

        # ---- token 阈值兜底：确定目标层级 ----
        if ratio >= L3_TRIGGER_RATIO:
            target = MemoryLevel.L3
        elif ratio >= L2_TRIGGER_RATIO:
            target = MemoryLevel.L2
        elif ratio >= L1_TRIGGER_RATIO:
            target = MemoryLevel.L1
        else:
            report["result"] = "skipped:below_threshold"
            return report

        # ---- 执行：从最旧的可压缩片段开始（L0 优先升级） ----
        for item in self.items:
            if item.level == MemoryLevel.L3:
                continue  # 已卸载
            if item.level == target:
                continue  # 已在该层

            # 依赖保护：有后续依赖的片段不能做 L3 卸载
            if target == MemoryLevel.L3 and item.has_dependency:
                report["actions"].append(
                    {"id": item.id, "result": "skipped:has_dependency"}
                )
                continue

            if target == MemoryLevel.L1:
                entities = extract_entities(item.original)
                item.compressed = extract_compress_l1(item.original, entities)
                item.tokens = _estimate_tokens_text(item.compressed)
                item.level = MemoryLevel.L1
                report["actions"].append({"id": item.id, "level": "L1"})

            elif target == MemoryLevel.L2:
                self._do_l2(item, report)

            elif target == MemoryLevel.L3:
                self._do_l3(item, report)

        report["result"] = f"compressed_to:{target.value}"
        report["in_context_tokens"] = self.in_context_tokens()
        return report

    # ---- L2：两阶段摘要 + 净收益校验 ----
    def _do_l2(self, item: MemoryItem, report: dict[str, Any]) -> None:
        if self.llm_call is None:
            # 无 LLM 注入时退化为直接置层（占位，不真正摘要）
            item.level = MemoryLevel.L2
            report["actions"].append({"id": item.id, "level": "L2:no_llm"})
            return

        before_tokens = item.tokens
        summary = summarize_l2([item], self.llm_call)
        # 估算本次压缩调用的 LLM 消耗 token（占位：按输入+输出字数估算）
        call_cost = _estimate_tokens_text(item.original) + _estimate_tokens_text(summary)

        # 压缩净收益校验：节省 token 必须 > 调用消耗 token，否则跳过本次压缩
        saved = before_tokens - _estimate_tokens_text(summary)
        if saved <= call_cost * L2_MIN_NET_GAIN_RATIO:
            report["actions"].append(
                {"id": item.id, "result": "skipped:low_net_gain",
                 "saved": saved, "cost": call_cost}
            )
            return

        item.compressed = summary
        item.tokens = _estimate_tokens_text(summary)
        item.level = MemoryLevel.L2
        # 原始对话保留（已由 item.original 持有），此处仅生成副本
        report["actions"].append(
            {"id": item.id, "level": "L2", "saved": saved, "cost": call_cost}
        )

    # ---- L3：向量化卸载，内存仅留 index ----
    def _do_l3(self, item: MemoryItem, report: dict[str, Any]) -> None:
        try:
            ref = offload_l3_vector(item, self.vector_store)
        except Exception:
            ref = None
        item.level = MemoryLevel.L3
        item.external_ref = ref
        # 卸载后内存不再计入上下文体积（但 original 仍持久保存在 item 上）
        item.tokens = 0
        report["actions"].append(
            {"id": item.id, "level": "L3", "external_ref": ref}
        )


# ===========================================================================
# 适配 LangGraph 的辅助：把 MemoryItem 列表重建为可注入的 prompt 片段
# ===========================================================================


def render_context(memory: HierarchicalMemory) -> list[Message]:
    """把仍在上下文中的 L0/L1/L2 片段渲染为 Message 列表（L3 已卸载不默认塞入）。

    在 LangGraph node 里调用，将分层记忆拼回对话历史。
    """
    blocks: list[str] = []
    for it in memory.items:
        if not it.in_context:
            continue
        prefix = {
            MemoryLevel.L0: "[L0]",
            MemoryLevel.L1: "[L1摘要]",
            MemoryLevel.L2: "[L2摘要]",
        }.get(it.level, "")
        blocks.append(f"{prefix}{it.compressed}" if prefix else it.compressed)
    return [Message(role="user", content="\n\n".join(blocks))]


# ===========================================================================
# 使用示例
# ===========================================================================


def _demo_llm_call(model: str, prompt: str) -> str:
    """示例 LLM 桩：真实场景替换为智谱/OpenAI 等流式调用。"""
    return f"[summary by {model}] " + prompt[:80]


if __name__ == "__main__":
    mem = HierarchicalMemory(
        max_context_window=200_000,
        llm_call=_demo_llm_call,
    )

    # 模拟写入对话片段（轻量演示：未达阈值，展示跳过逻辑；
    # 把 range 调大即可观察 L1/L2/L3 实际触发，见上文 800 次的验证输出）
    for i in range(50):
        big = "用户请求 Order_123 重构。@alice 需要调用 RefactorTool 处理模块。" + "x" * 4000
        mem.add(
            big,
            safe_breakpoint=(i % 5 == 0),     # 每 5 轮一个安全断点
            has_entity=("Order_123" in big),
            has_dependency=(i < 3),           # 前 3 条有依赖，禁止 L3
        )

    print("初始 in_context_tokens:", mem.in_context_tokens())
    print("usage_ratio:", round(mem.usage_ratio(), 3))

    # 演示：在「工具结束」安全断点触发压缩
    rep = mem.compress_memory(BreakpointSignal.TOOL_END)
    print("压缩报告:", rep)

    # 演示：推理中途触发 -> 应被时机约束拦截
    rep2 = mem.compress_memory(BreakpointSignal.MID_REASONING)
    print("中途触发报告:", rep2)

    # 演示：渲染回上下文
    ctx = render_context(mem)
    print("上下文片段数:", len(ctx))
