"""L3 LLM 兜底判定。

只处理 L2 打分落在「灰区」的候选：分数太低的已被丢弃，分数太高的已自动接受。
这样 LLM 调用量通常只占商品总数的一小部分，成本可控。

未配置 ANTHROPIC_API_KEY 时整层自动跳过，主流程不受影响。
"""

from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor

from ..models import MatchLevel, MatchPair, Product

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是商超比价系统的商品匹配判定员。

判定标准：两个商品能否作为**比价对象**，即消费者会认为它们是可替代的同一款商品。

判定为匹配（is_match=true）需同时满足：
1. 品牌一致（或都是无品牌的生鲜/散装同品类商品）
2. 品类与口味/型号一致（原味 vs 香辣 = 不匹配；全脂 vs 脱脂 = 不匹配）
3. 规格可比 —— 注意：包装不同但单位量可折算的**算匹配**（500ml×6 与 1.5L×2 都是水，可按单位价比），
   但净含量差异超过 3 倍的一般不作为比价对象

判定为不匹配的常见情况：
- 同品牌不同产品线（特仑苏 vs 蒙牛纯牛奶）
- 同品类不同品牌
- 赠品装/礼盒装 与 常规装（规格构成不同，无法稳定折算）

confidence 表示你的把握程度：1.0 = 确信，0.5 = 拿不准，0.0 = 确信不匹配。
拿不准就给低分，系统会转人工复核 —— 错配比漏配代价大得多。"""

# 结构化输出 schema，保证返回一定是可解析的 JSON
_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "对应输入候选的序号"},
                    "is_match": {"type": "boolean"},
                    "confidence": {"type": "number", "description": "0~1"},
                    "reason": {"type": "string", "description": "一句话判定依据，中文"},
                },
                "required": ["index", "is_match", "confidence", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}


def is_available() -> bool:
    """未配置密钥或未安装 SDK 时返回 False，调用方据此跳过整层。"""
    if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")):
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def _describe(p: Product) -> str:
    bits = [f"标题：{p.title}"]
    if p.brand:
        bits.append(f"品牌：{p.brand}")
    if p.spec.parsed:
        bits.append(f"规格：{p.spec.display()}")
    elif p.spec_text:
        bits.append(f"规格：{p.spec_text}")
    if p.price is not None:
        bits.append(f"售价：{p.price:.2f}元")
    return " | ".join(bits)


def _build_prompt(batch: list[tuple[Product, Product]]) -> str:
    lines = ["请逐条判定以下商品对是否可作为比价对象。\n"]
    for i, (a, b) in enumerate(batch):
        lines.append(f"[{i}]")
        lines.append(f"  我方商品：{_describe(a)}")
        lines.append(f"  竞品商品：{_describe(b)}")
        lines.append("")
    lines.append("对每一条给出判定，results 数组长度必须等于输入条数。")
    return "\n".join(lines)


def _judge_batch(
    client, model: str, batch: list[tuple[Product, Product]]
) -> dict[int, dict]:
    """判定一批候选，返回 {输入序号: 判定结果}。失败时返回空字典（降级为人工复核）。"""
    import anthropic

    try:
        response = client.messages.create(
            model=model,
            max_tokens=8000,
            system=SYSTEM_PROMPT,
            # 这是批量分类任务，低 effort 足够且显著省钱
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": _OUTPUT_SCHEMA},
            },
            messages=[{"role": "user", "content": _build_prompt(batch)}],
        )
    except anthropic.RateLimitError:
        logger.warning("LLM 限流，该批候选转人工复核")
        return {}
    except anthropic.APIStatusError as e:
        logger.warning("LLM 调用失败 (%s)，该批候选转人工复核", e.status_code)
        return {}
    except anthropic.APIConnectionError:
        logger.warning("LLM 网络异常，该批候选转人工复核")
        return {}

    if response.stop_reason == "refusal":
        logger.warning("LLM 拒绝响应，该批候选转人工复核")
        return {}

    try:
        text = next(b.text for b in response.content if b.type == "text")
        payload = json.loads(text)
    except (StopIteration, json.JSONDecodeError):
        logger.warning("LLM 返回无法解析，该批候选转人工复核")
        return {}

    return {int(r["index"]): r for r in payload.get("results", [])}


def adjudicate(
    candidates: list[tuple[Product, Product, float, str]],
    model: str = "claude-opus-5",
    batch_size: int = 20,
    max_concurrency: int = 4,
    accept_confidence: float = 0.7,
) -> list[MatchPair]:
    """判定灰区候选。

    Args:
        candidates: (我方商品, 竞品商品, L2分数, L2依据) 列表
        accept_confidence: 低于此置信度的判定标记为需人工复核

    Returns:
        MatchPair 列表。判定为不匹配的返回 rival_product=None。
    """
    if not candidates:
        return []

    if not is_available():
        logger.info("未配置 ANTHROPIC_API_KEY，跳过 L3，%d 组候选转人工复核", len(candidates))
        return [
            MatchPair(
                self_product=a,
                rival_product=b,
                score=score,
                level=MatchLevel.FUZZY,
                confidence=score / 100.0,
                reason=f"{reason}（未启用 AI 判定）",
                need_review=True,
            )
            for a, b, score, reason in candidates
        ]

    import anthropic

    client = anthropic.Anthropic()
    batches = [
        candidates[i : i + batch_size] for i in range(0, len(candidates), batch_size)
    ]

    verdicts: list[dict[int, dict]] = [{}] * len(batches)
    with ThreadPoolExecutor(max_workers=max_concurrency) as pool:
        futures = {
            pool.submit(
                _judge_batch, client, model, [(a, b) for a, b, _, _ in batch]
            ): bi
            for bi, batch in enumerate(batches)
        }
        for future in futures:
            verdicts[futures[future]] = future.result()

    pairs: list[MatchPair] = []
    for bi, batch in enumerate(batches):
        verdict = verdicts[bi]
        for i, (a, b, score, reason) in enumerate(batch):
            v = verdict.get(i)
            if v is None:
                # 判定缺失（调用失败或模型漏答），保守处理：保留候选但转人工
                pairs.append(
                    MatchPair(
                        self_product=a,
                        rival_product=b,
                        score=score,
                        level=MatchLevel.FUZZY,
                        confidence=score / 100.0,
                        reason=f"{reason}（AI 未给出判定）",
                        need_review=True,
                    )
                )
                continue

            confidence = float(v.get("confidence", 0.0))
            if not v.get("is_match"):
                pairs.append(
                    MatchPair(
                        self_product=a,
                        rival_product=None,
                        score=score,
                        level=MatchLevel.NONE,
                        confidence=confidence,
                        reason=f"AI 判定不匹配：{v.get('reason', '')}",
                        # 低置信的「不匹配」也值得人工看一眼，避免漏配
                        need_review=confidence < accept_confidence,
                    )
                )
                continue

            pairs.append(
                MatchPair(
                    self_product=a,
                    rival_product=b,
                    score=score,
                    level=MatchLevel.LLM,
                    confidence=confidence,
                    reason=f"AI 判定匹配：{v.get('reason', '')}",
                    need_review=confidence < accept_confidence,
                )
            )

    return pairs
