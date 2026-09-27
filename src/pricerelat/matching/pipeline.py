"""分层匹配编排。

关系库 → L1 硬规则 → L2 模糊召回 → L3 LLM 兜底，每层只处理上一层没解决的商品。
这个顺序保证了：确认过的直接复用，准确的用零成本方法解决，只有真正模糊的才花钱调模型。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..models import MatchLevel, MatchPair, Product, RelationStatus, ReviewReason
from . import fuzzy, llm, rules

logger = logging.getLogger(__name__)

# 系统自动确认的匹配层级：L1 硬规则与 L2 高分。L3 的结论一律要人工确认。
_AUTO_CONFIRM_LEVELS = (MatchLevel.BARCODE, MatchLevel.BRAND_SPEC, MatchLevel.FUZZY)
AUTO_CONFIRMER = "系统自动"


@dataclass
class MatchStats:
    """各层命中统计，跑完打印出来能直观看到匹配质量。"""

    total: int = 0
    by_level: dict[str, int] = field(default_factory=dict)
    need_review: int = 0
    unmatched: int = 0
    llm_calls: int = 0
    reused: int = 0        # 直接复用关系库或上次结果，未进入 L1-L3
    layered: int = 0       # 本次进入 L1-L3 的商品数

    def summary(self) -> str:
        parts = [f"共 {self.total} 个商品"]
        if self.reused:
            parts.append(f"复用 {self.reused}")
        for level, n in self.by_level.items():
            parts.append(f"{level} {n}")
        parts.append(f"未匹配 {self.unmatched}")
        parts.append(f"待复核 {self.need_review}")
        return " | ".join(parts)


def _run_layers(
    self_products: list[Product],
    rival_products: list[Product],
    cfg: dict,
    excluded: dict[str, set[str]] | None = None,
    stats: MatchStats | None = None,
) -> dict[str, MatchPair]:
    """L1 → L2 → L3。excluded 为 {我方uid: 已被人工否决的竞品 sku 集合}。"""
    excluded = excluded or {}
    stats = stats or MatchStats()
    result: dict[str, MatchPair] = {}
    m_cfg = cfg.get("matching", {})

    def allowed(pair: MatchPair) -> bool:
        return pair.rival_product.sku_id not in excluded.get(pair.self_product.uid, ())

    def pool_for(p: Product) -> list[Product]:
        banned = excluded.get(p.uid)
        return [r for r in rival_products if r.sku_id not in banned] if banned else rival_products

    # ---- L1 硬规则 ----
    if m_cfg.get("rules", {}).get("enable_barcode", True):
        hits = rules.match_by_barcode(self_products, rival_products)
        result.update({uid: pair for uid, pair in hits.items() if allowed(pair)})

    if m_cfg.get("rules", {}).get("enable_brand_spec", True):
        remaining = [p for p in self_products if p.uid not in result]
        hits = rules.match_by_brand_spec(remaining, rival_products)
        result.update({uid: pair for uid, pair in hits.items() if allowed(pair)})

    # ---- L2 模糊召回 ----
    f_cfg = m_cfg.get("fuzzy", {})
    auto_accept = f_cfg.get("auto_accept", 88)
    reject_below = f_cfg.get("reject_below", 55)

    gray_zone: list[tuple[Product, Product, float, str]] = []
    pending = [p for p in self_products if p.uid not in result]

    for p in pending:
        candidates = fuzzy.recall(
            p,
            pool_for(p),
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

    return result


def _finish_stats(stats: MatchStats, result: dict[str, MatchPair]) -> MatchStats:
    for pair in result.values():
        if pair.matched:
            label = "关系库" if pair.from_store else pair.level.value
            stats.by_level[label] = stats.by_level.get(label, 0) + 1
        else:
            stats.unmatched += 1
        if pair.need_review:
            stats.need_review += 1
    return stats


def match_platform(
    self_products: list[Product],
    rival_products: list[Product],
    cfg: dict,
    store=None,
    period: str = "",
) -> tuple[dict[str, MatchPair], MatchStats]:
    """把我方商品与单个竞品平台的商品做匹配。

    Args:
        store: 关系库（pricerelat.store.Store）。为 None 时每次从零匹配，行为同 V1。
        period: 比价期次，关系库用它计算「连续 N 期未出现」。

    Returns:
        ({我方商品uid: MatchPair}, 统计)
    """
    stats = MatchStats(total=len(self_products))
    if store is None or not rival_products:
        result = _run_layers(self_products, rival_products, cfg, stats=stats)
        stats.layered = len(self_products)
        return result, _finish_stats(stats, result)

    from .relations import RelationMatcher

    matcher = RelationMatcher(store, rival_products, cfg, period)
    result, to_match, excluded = matcher.resolve(self_products)
    stats.reused = len(result)
    stats.layered = len(to_match)

    if to_match:
        fresh = _run_layers(to_match, rival_products, cfg, excluded, stats)
        matcher.persist(fresh)
        result.update(fresh)

    return result, _finish_stats(stats, result)


def classify(pair: MatchPair) -> tuple[RelationStatus, str]:
    """新匹配结果写入关系库时的状态与复核原因。

    L1 与 L2 高分自动确认；L3 即使高置信也要人工确认（错配代价高于复核成本）；
    其余进入待复核。
    """
    if pair.matched and not pair.need_review and pair.level in _AUTO_CONFIRM_LEVELS:
        return RelationStatus.CONFIRMED, ""
    if pair.matched and pair.level is MatchLevel.LLM and not pair.need_review:
        return RelationStatus.PENDING, ReviewReason.AI_UNCONFIRMED.value
    if pair.level is MatchLevel.FUZZY:
        return RelationStatus.PENDING, ReviewReason.GRAY_ZONE.value
    return RelationStatus.PENDING, ReviewReason.AI_LOW_CONFIDENCE.value
