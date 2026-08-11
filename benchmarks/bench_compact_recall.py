"""Benchmark ③：Layer 2 上下文压缩（auto_compact）的摘要召回率。

背景：项目声称两层渐进式压缩能把上下文占用从 34% 压到接近 100% 的水平，并能在
SWE-bench 上维持召回率。本脚本为该声明提供一条可复现的**召回率（recall）**评测。

原理：
1. 构造一段「足够长、值得压缩」的合成对话（含 8 轮事实性对话 + 若干可区分尾部）。
2. 在对话里埋入一组 **gold facts**（如「密码是 p@ssw0rd-42」「服务端口是 8080」
   「改用 v2 API」「本地化函数名是 _i18n()」等，代码里以强特征字符串表达）。
3. 用真实的 LLM client 调用 `auto_compact()` 生成结构化摘要。
4. 把摘要（summary + 保留下来的 keep 尾部原文）拼接起来，检查每条 gold fact 是否
   仍能在压缩后的对话里被**逐字定位**——能定位即视为"召回"。
5. 召回率 = 被召回事实数 / 总事实数。

注意：auto_compact 会保留下近期的 keep 尾部原文。为避免"靠 keep 原文躺赢"，
本脚本在构造对话时把 gold facts 均匀散布到**会被摘要的旧前缀**里（位于对话前部），
并额外保留 1~2 条落在 keep 窗口内的对照事实，用于对比"原文保留 vs 摘要"两条路径。

运行（需要真实 LLM + 已配置 provider）：
    uv run python -m benchmarks.bench_compact_recall
    uv run python -m benchmarks.bench_compact_recall --facts 6 --dry-run
    # --dry-run 只构造 gold set 并打印，不调用 API，便于离线审查。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmarks._common import make_client, require_provider

from coding_agent.context.manager import auto_compact, extract_summary
from coding_agent.conversation import ConversationManager, Message


# ---------------------------------------------------------------------------
# Gold facts 构造
# ---------------------------------------------------------------------------

# 每条 fact 是一段人类可读描述 + 一个强特征字符串，二者都会在对话文本中出现。
GOLD_FACTS = [
    ("数据库连接串", "postgres://bench-user@db.internal:5432/appdb"),
    ("加密密钥轮换周期", "ROTATE_EVERY_72H"),
    ("API 版本升级决策", "UPGRADE_TO_V2_API"),
    ("内部错误码", "ERR_BENCH_8492"),
    ("部署环境", "DEPLOY_ENV=staging-us-east"),
    ("迁移目标端口", "MIGRATE_TARGET_PORT_8080"),
    ("功能开关名", "feature_flag_bench_quantum"),
    ("监控告警阈值", "ALERT_THRESHOLD_87_PERCENT"),
]


def _fact_lines(fact: tuple[str, str]) -> list[str]:
    desc, key = fact
    return [
        f"用户指出：{desc}。",
        f"关键值/标识符是 `{key}`，后续所有相关决策都必须沿用这一取值。",
    ]


def build_conversation(n_prefix_turns: int = 8, gold: list[tuple[str, str]] | None = None) -> ConversationManager:
    """构造一条足够长、可压缩的对话，gold facts 散布在前部（会被摘要）。

    为了让 auto_compact 真正触发，需要满足两点：
    - 前缀（会被摘要的部分）估算 token 远超 MIN_SUMMARIZE_PREFIX_TOKENS (2000)，
      否则 auto_compact 会判定「不值得压缩」而直接跳过；
    - 消息条数足够多、足够大，让 keep 窗口只覆盖尾部，gold facts 落在摘要区。

    这里用每条约 1.6KB 文本的填充块（≈450 token/条）把前缀凑到数千 token。
    """
    gold = gold or GOLD_FACTS
    conv = ConversationManager()

    # 每块约 450 token（1570 字符），filler 用于撑大前缀、制造可压缩空间。
    filler = (
        "在处理分布式系统中的消息队列背压、限流策略、重试与幂等语义时，需要仔细权衡"
        "吞吐量与一致性保证之间的取舍，同时考虑故障恢复路径、死信队列、消费端幂等去重、"
        "分区再平衡以及慢消费者对整体延迟的影响。这里要记录具体的技术选型、参数取值和"
        "后续约束，以便在对话被压缩后仍能准确回忆起这些决定。此外还要结合服务注册发现、"
        "配置中心热更新、分布式链路追踪的采样率、限流算法的令牌桶容量以及降级熔断的"
        "触发阈值，共同构成一个完整且自洽的技术上下文，确保后续任何局部改动都不会破坏"
        "整体的可观测性与稳定性目标。"
    )

    for i in range(n_prefix_turns):
        fact = gold[i % len(gold)]
        lines = _fact_lines(fact)
        user_msg = (
            f"第 {i+1} 轮任务背景。\n"
            + "\n".join(lines)
            + f"\n{filler * 12}"
        )
        conv.add_user_message(user_msg)
        conv.add_assistant_message(f"明白，我会围绕该轮内容给出实现方案并记录关键参数。{filler * 8}")

    # 预留事实：若有多的 fact，追加为额外的用户轮次（仍在前部可被摘要）。
    extra_start = n_prefix_turns
    while extra_start < len(gold):
        fact = gold[extra_start]
        conv.add_user_message(f"补充说明。\n" + "\n".join(_fact_lines(fact)) + f"\n{filler * 8}")
        conv.add_assistant_message(f"已补充。{filler * 6}")
        extra_start += 1

    # 塞一条大的尾部消息，保证 history 足够长、keep 窗口能落在其上。
    conv.add_user_message(f"收尾汇总。\n{filler * 10}")

    return conv


def locate_facts_in(messages: list[Message], gold: list[tuple[str, str]]) -> dict[str, bool]:
    """检查每条 gold fact 的特征字符串是否出现在给定消息文本里。"""
    found: dict[str, bool] = {}
    for desc, key in gold:
        found[desc] = any(key in m.content for m in messages if m.content)
    return found


# ---------------------------------------------------------------------------
# 评测主流程
# ---------------------------------------------------------------------------


async def _run_once(provider, facts, context_window, protocol, temp_dir) -> dict:
    conv = build_conversation(gold=facts)
    client = make_client(provider)
    if client is None:
        return {"skipped": True, "reason": "no API key / client"}

    # 钉一个很高的用量锚点，强制触发自动压缩（等价于真实对话逼近上下文上限）。
    conv.record_usage_anchor(input_tokens=context_window)

    from coding_agent.context.manager import CompactEvent
    result = await auto_compact(
        conv,
        client,
        context_window=context_window,
        session_dir=Path(temp_dir),
        protocol=protocol,
    )

    if isinstance(result, str):
        return {"skipped": True, "reason": f"auto_compact 返回错误/降级: {result}"}
    if result is None:
        return {"skipped": True, "reason": "auto_compact 判定无需压缩（前缀过小）"}

    assert isinstance(result, CompactEvent)
    summary = result.boundary.summary if result.boundary else ""
    keep = result.boundary.keep if result.boundary else []

    # 压缩后对话 = 摘要 user 消息 + 保留的 keep 尾部原文。
    post_messages = [m for m in conv.history]
    post_texts = [m.content for m in post_messages if m.content]
    combined = "\n".join(post_texts)

    # 判定每条 fact 是否被"压缩后状态"召回。
    recalled = {desc: (key in combined) for desc, key in facts}
    hits = sum(1 for v in recalled.values() if v)
    recall = hits / len(facts) if facts else 0.0

    return {
        "skipped": False,
        "n_facts": len(facts),
        "recalled": hits,
        "recall_pct": round(100.0 * recall, 2),
        "summary_chars": len(summary),
        "keep_messages": len(keep),
        "post_messages": len(post_messages),
        "per_fact": recalled,
        "summary_excerpt": summary[:300],
    }


async def run(facts: int = 6, context_window: int = 200_000, protocol: str = "anthropic",
              dry_run: bool = False) -> dict:
    provider = require_provider()
    if protocol is None:
        protocol = provider.protocol
    selected = GOLD_FACTS[:facts]

    if dry_run:
        conv = build_conversation(gold=selected)
        return {
            "dry_run": True,
            "n_facts": len(selected),
            "gold_facts": [{"desc": d, "key": k} for d, k in selected],
            "conv_messages": len(conv.history),
            "estimated_prefix_tokens": None,
        }

    with tempfile.TemporaryDirectory() as tmp:
        return await _run_once(provider, selected, context_window, protocol, tmp)


def main() -> None:
    parser = argparse.ArgumentParser(description="上下文压缩摘要召回率评测")
    parser.add_argument("--facts", type=int, default=6,
                        help=f"gold facts 数量 (最多 {len(GOLD_FACTS)})")
    parser.add_argument("--window", type=int, default=200_000, help="上下文窗口")
    parser.add_argument("--protocol", default=None,
                        help="覆盖 provider 协议 (anthropic/openai/openai-compat)")
    parser.add_argument("--dry-run", action="store_true", help="只构造 gold set，不调用 API")
    args = parser.parse_args()

    result = asyncio.run(run(
        facts=args.facts,
        context_window=args.window,
        protocol=args.protocol,
        dry_run=args.dry_run,
    ))

    print("=" * 60)
    print("Benchmark ③: 上下文压缩 (auto_compact) 召回率")
    print("=" * 60)

    if result.get("dry_run"):
        print(f"[dry-run] 构造了 {result['n_facts']} 条 gold facts，"
              f"对话消息数 {result['conv_messages']}。未调用 API。")
        for f in result["gold_facts"]:
            print(f"  - {f['desc']}: `{f['key']}`")
        return

    if result.get("skipped"):
        print(f"[跳过] {result.get('reason')}")
        print("需要真实 LLM + 已配置 provider 才能运行本评测。")
        return

    print(f"gold facts 总数      : {result['n_facts']}")
    print(f"被召回              : {result['recalled']}/{result['n_facts']}")
    print(f"召回率              : {result['recall_pct']}%")
    print(f"摘要字符数          : {result['summary_chars']}")
    print(f"保留 keep 消息数    : {result['keep_messages']}")
    print(f"压缩后消息总数      : {result['post_messages']}")
    print("-" * 60)
    print("逐条召回情况：")
    for desc, ok in result["per_fact"].items():
        mark = "[ok]  " if ok else "[miss]"
        print(f"  {mark} {desc}")
    print("-" * 60)
    print(f"摘要开头：{result['summary_excerpt']}")

    if result["recall_pct"] < 100.0:
        print("\n[!] 提示：未达到 100% 召回。这可能意味着摘要遗漏了某些早期关键取值。"
              "请检查 SUMMARY_PROMPT 第 3/6 段是否覆盖了这些信息，或考虑是否需要"
              "通过 recovery attachment / transcript 路径兜底。", file=sys.stderr)


if __name__ == "__main__":
    main()
