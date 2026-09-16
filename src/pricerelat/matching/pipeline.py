"""分层匹配编排。

L1 硬规则 → L2 模糊召回 → L3 LLM 兜底，每层只处理上一层没解决的商品。
这个顺序保证了：准确的用零成本方法解决，只有真正模糊的才花钱调模型。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..models import MatchLevel, MatchPair, Product
from . import fuzzy, llm, rules

logger = logging.getLogger(__name__)


@dataclass
class MatchStats:
    """各层命中统计，跑完打印出来能直观看到匹配质量。"""

    total: int = 0
    by_level: dict[str, int] = field(default_factory=dict)
    need_review: int = 0
    unmatched: int = 0
    llm_calls: int = 0

    def summary(self) -> str:
        parts = [f"共 {self.total} 个商品"]
        for level, n in self.by_level.items():
            parts.append(f"{level} {n}")
        parts.append(f"未匹配 {self.unmatched}")
        parts.append(f"待复核 {self.need_review}")
        return " | ".join(parts)


def match_platform(
    self_products: list[Product],
    rival_products: list[Product],
    cfg: dict,
) -> tuple[dict[str, MatchPair], MatchStats]:
    """把我方商品与单个竞品平台的商品做匹配。

    Returns:
        ({我方商品uid: MatchPair}, 统计)
    """
    stats = MatchStats(total=len(self_products))
    result: dict[str, MatchPair] = {}
    m_cfg = cfg.get("matching", {})

    # ---- L1 硬规则 ----
    if m_cfg.get("rules", {}).get("enable_barcode", True):
        result.update(rules.match_by_barcode(self_products, rival_products))

    if m_cfg.get("rules", {}).get("enable_brand_spec", True):
        remaining = [p for p in self_products if p.uid not in result]
        result.update(rules.match_by_brand_spec(remaining, rival_products))

    # ---- L2 模糊召回 ----
    f_cfg = m_cfg.get("fuzzy", {})
    auto_accept = f_cfg.get("auto_accept", 88)
    reject_below = f_cfg.get("reject_below", 55)

    gray_zone: list[tuple[Product, Product, float, str]] = []
    pending = [p for p in self_products if p.uid not in result]

    for p in pending:
        candidates = fuzzy.recall(
            p,
            rival_products,
            top_k=f_cfg.get("top_k", 5),
            spec_mismatch_penalty=f_cfg.get("spec_mismatch_penalty", 12),
        )
        if not candidates:
            result[p.uid] = MatchPair(self_product=p, rival_product=None, reason="无候选商品")
            continue

        best = candidates[0]
        rival, score, reason = best

        if score >= auto_accept:
            result[p.uid] = fuzzy.to_pair(p, best)
        elif score < reject_below:
            result[p.uid] = MatchPair(
                self_product=p,
                rival_product=None,
                score=score,
                reason=f"最高分 {score:.1f} 低于阈值 {reject_below}",
            )
        else:
            gray_zone.append((p, rival, score, reason))

    # ---- L3 LLM 兜底 ----
    l_cfg = m_cfg.get("llm", {})
    if gray_zone:
        if l_cfg.get("enabled", True):
            logger.info("灰区候选 %d 组，交由 L3 判定", len(gray_zone))
            stats.llm_calls = len(gray_zone)
            for pair in llm.adjudicate(
                gray_zone,
                model=l_cfg.get("model", "claude-opus-5"),
                batch_size=l_cfg.get("batch_size", 20),
                max_concurrency=l_cfg.get("max_concurrency", 4),
                accept_confidence=l_cfg.get("accept_confidence", 0.7),
            ):
                result[pair.self_product.uid] = pair
        else:
            for p, rival, score, reason in gray_zone:
                result[p.uid] = MatchPair(
                    self_product=p,
                    rival_product=rival,
                    score=score,
                    level=MatchLevel.FUZZY,
                    confidence=score / 100.0,
                    reason=reason,
                    need_review=True,
                )

    # ---- 统计 ----
    for pair in result.values():
        if pair.matched:
            stats.by_level[pair.level.value] = stats.by_level.get(pair.level.value, 0) + 1
        else:
            stats.unmatched += 1
        if pair.need_review:
            stats.need_review += 1

    return result, stats
