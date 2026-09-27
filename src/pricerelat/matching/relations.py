"""关系库与匹配流程的衔接。

匹配前：对每个「我方 SKU × 竞品平台」先查关系库
    - 有已确认 / 待复核关系且竞品本期仍在 → 直接复用，不跑 L1-L3
    - 已确认竞品本期缺席 → 本期无该竞品价格；连续 N 期缺席置为失效，再重新匹配
    - 已否决的竞品从 L1-L3 候选中剔除
    - 上次未匹配、且双方数据都没变 → 沿用上次结果

匹配后：新结果写回关系库（自动确认 / 待复核），未匹配的记下本次结果。
"""

from __future__ import annotations

import logging
from datetime import datetime

from ..models import MatchLevel, MatchPair, Product, RelationStatus, ReviewReason
from ..store.db import Attempt, Relation, Store, fingerprint, pool_fingerprint

logger = logging.getLogger(__name__)


def _level_of(source: str) -> MatchLevel:
    try:
        return MatchLevel(source)
    except ValueError:
        return MatchLevel.MANUAL


def change_note(rel: Relation, self_product: Product, rival: Product) -> str:
    """与确认时快照对比，返回变更描述；未变更返回空串。"""
    notes = []
    for side, product, old_fp, old_title, old_spec in (
        ("我方", self_product, rel.self_fp, rel.self_title, rel.self_spec),
        ("竞品", rival, rel.rival_fp, rel.rival_title, rel.rival_spec),
    ):
        if not old_fp or fingerprint(product) == old_fp:
            continue
        if product.spec.display() != old_spec:
            notes.append(f"{side}规格：{old_spec} → {product.spec.display()}")
        if product.title != old_title:
            notes.append(f"{side}标题：{old_title} → {product.title}")
    return "；".join(notes)


class RelationMatcher:
    def __init__(self, store: Store, rival_products: list[Product], cfg: dict, period: str):
        self.store = store
        self.period = period
        self.platform = rival_products[0].platform
        self.rivals = {r.sku_id: r for r in rival_products}
        self.pool_fp = pool_fingerprint(rival_products)
        self.invalid_after = cfg.get("store", {}).get("invalid_after_periods", 3)

    # ------------------------------------------------------------------
    # 匹配前
    # ------------------------------------------------------------------

    def resolve(
        self, self_products: list[Product]
    ) -> tuple[dict[str, MatchPair], list[Product], dict[str, set[str]]]:
        """Returns: (已解决的匹配, 需要跑 L1-L3 的商品, 每个商品被否决的竞品 sku)"""
        relations = self.store.relations_by_self(self.platform)
        resolved: dict[str, MatchPair] = {}
        to_match: list[Product] = []
        excluded: dict[str, set[str]] = {}

        for p in self_products:
            rels = relations.get(p.sku_id, [])
            rejected = {r.rival_sku for r in rels if r.status is RelationStatus.REJECTED}
            if rejected:
                excluded[p.uid] = rejected

            pair = self._from_relations(p, rels)
            if pair is None:
                pair = self._from_attempt(p)
            if pair is None:
                to_match.append(p)
            else:
                resolved[p.uid] = pair

        return resolved, to_match, excluded

    def _from_relations(self, p: Product, rels: list[Relation]) -> MatchPair | None:
        by_status = {s: [r for r in rels if r.status is s] for s in RelationStatus}
        for rel in by_status[RelationStatus.CONFIRMED] + by_status[RelationStatus.PENDING]:
            rival = self.rivals.get(rel.rival_sku)
            if rival is not None:
                return self._reuse(p, rival, rel)
            if rel.status is RelationStatus.CONFIRMED and not self._mark_missing(rel):
                return MatchPair(
                    self_product=p,
                    rival_product=None,
                    reason=f"已确认竞品 {rel.rival_sku} 本期未采集到",
                    status=rel.status,
                    from_store=True,
                )
        return None

    def _reuse(self, p: Product, rival: Product, rel: Relation) -> MatchPair:
        rel.last_seen_period = self.period
        rel.missing_count = 0

        note = change_note(rel, p, rival)
        if note and rel.status is RelationStatus.CONFIRMED:
            rel.status = RelationStatus.PENDING
            rel.review_reason = ReviewReason.INFO_CHANGED.value
            rel.change_note = note
            logger.info("[%s] %s ↔ %s 商品信息变更，转待复核", self.platform, p.sku_id, rival.sku_id)
        self.store.save_relation(rel)

        pending = rel.status is not RelationStatus.CONFIRMED
        return MatchPair(
            self_product=p,
            rival_product=rival,
            score=rel.score,
            level=_level_of(rel.source),
            confidence=rel.confidence,
            reason=f"{rel.reason}（关系库{rel.status.value}）",
            need_review=pending,
            status=rel.status,
            review_reason=rel.review_reason if pending else "",
            change_note=rel.change_note if pending else "",
            from_store=True,
        )

    def _mark_missing(self, rel: Relation) -> bool:
        """已确认竞品本期缺席。同一期只计一次；达到 N 期置为失效，返回 True。"""
        if rel.missing_period != self.period and rel.last_seen_period != self.period:
            rel.missing_count += 1
            rel.missing_period = self.period
        if rel.missing_count >= self.invalid_after:
            rel.status = RelationStatus.INVALID
            logger.info(
                "[%s] %s ↔ %s 连续 %d 期未采集到，置为失效",
                self.platform, rel.self_sku, rel.rival_sku, rel.missing_count,
            )
        self.store.save_relation(rel)
        return rel.status is RelationStatus.INVALID

    def _from_attempt(self, p: Product) -> MatchPair | None:
        attempt = self.store.get_attempt(p.sku_id, self.platform)
        if attempt is None or attempt.self_fp != fingerprint(p) or attempt.pool_fp != self.pool_fp:
            return None
        return MatchPair(
            self_product=p,
            rival_product=None,
            score=attempt.score,
            reason=f"{attempt.reason}（双方数据未变，沿用 {attempt.period} 结果）",
            from_store=True,
        )

    # ------------------------------------------------------------------
    # 匹配后
    # ------------------------------------------------------------------

    def persist(self, pairs: dict[str, MatchPair]) -> None:
        from .pipeline import AUTO_CONFIRMER, classify

        now = datetime.now().isoformat(timespec="seconds")
        for pair in pairs.values():
            p = pair.self_product
            target = pair.review_target
            if target is None or (not pair.matched and not pair.need_review):
                self.store.save_attempt(
                    Attempt(p.sku_id, self.platform, fingerprint(p), self.pool_fp,
                            pair.score, pair.reason, self.period)
                )
                continue

            status, review_reason = classify(pair)
            self._drop_superseded_pending(p.sku_id, target.sku_id)
            rel = self.store.get_relation(p.sku_id, self.platform, target.sku_id) or Relation(
                self_sku=p.sku_id, platform=self.platform, rival_sku=target.sku_id,
                status=status, source=pair.level.value,
            )
            rel.status = status
            rel.source = pair.level.value if pair.matched else MatchLevel.LLM.value
            rel.auto = status is RelationStatus.CONFIRMED
            rel.score, rel.confidence, rel.reason = pair.score, pair.confidence, pair.reason
            rel.review_reason, rel.change_note = review_reason, ""
            rel.confirmed_by = AUTO_CONFIRMER if rel.auto else ""
            rel.confirmed_at = now if rel.auto else ""
            rel.last_seen_period, rel.missing_count = self.period, 0
            rel.snapshot(p, target)
            self.store.save_relation(rel)
            self.store.delete_attempt(p.sku_id, self.platform)

            pair.status = status
            pair.review_reason = review_reason
            # 只有已确认的关系才能作为正式调价依据
            pair.need_review = status is not RelationStatus.CONFIRMED

    def _drop_superseded_pending(self, self_sku: str, keep_rival: str) -> None:
        """同一「我方 SKU × 平台」的旧待复核候选被新候选取代，一并清掉。"""
        for rel in self.store.relations(platform=self.platform, self_sku=self_sku,
                                        status=RelationStatus.PENDING):
            if rel.rival_sku != keep_rival:
                self.store.delete_relation(rel)
