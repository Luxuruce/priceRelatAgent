"""L2 模糊召回。

对 L1 没命中的商品，用文本相似度召回 Top-K 候选并打分。
纯文本相似度会把「农夫山泉 550ml」和「农夫山泉 4L」判成高度相似，
所以加了两道修正：品类不同直接排除，规格差异按倍率扣分。
"""

from __future__ import annotations

from rapidfuzz import fuzz, process

from ..models import MatchPair, Product
from ..normalize.text import char_bigrams, sub_brand_mismatch, variant_conflict


# 产品线不一致的扣分。取值要够大，保证原本 90+ 的候选会落进灰区。
_SUB_BRAND_PENALTY = 20.0


def _bigram_sim(a: str, b: str) -> float:
    """字符二元组 Jaccard 相似度。对中文比编辑距离更稳。"""
    sa, sb = char_bigrams(a), char_bigrams(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb) * 100


def text_score(a: Product, b: Product) -> float:
    """标题相似度：token_set_ratio 与字符二元组取加权。"""
    ta, tb = a.norm_title, b.norm_title
    if not ta or not tb:
        return 0.0
    return 0.6 * fuzz.token_set_ratio(ta, tb) + 0.4 * _bigram_sim(ta, tb)


def spec_penalty(a: Product, b: Product, max_penalty: float) -> tuple[float, str]:
    """规格差异惩罚。

    双方规格都解析成功时，按整包总量的倍率差扣分：
    同规格不扣分，倍率差越大扣得越多，封顶 max_penalty。
    单位类别不同（重量 vs 体积）视为不可比，直接扣满。
    """
    if not (a.spec.parsed and b.spec.parsed):
        return 0.0, ""
    if a.spec.unit is not b.spec.unit:
        return max_penalty, f"单位类别不同（{a.spec.unit.value} vs {b.spec.unit.value}）"

    ratio = max(a.spec.total_base, b.spec.total_base) / min(
        a.spec.total_base, b.spec.total_base
    )
    if ratio <= 1.05:
        return 0.0, ""
    # ratio 1.05→轻微，2→中等，>=4→扣满
    penalty = min(max_penalty, (ratio - 1.0) / 3.0 * max_penalty)
    return penalty, f"规格差 {ratio:.1f}×（{a.spec.display()} vs {b.spec.display()}）"


def recall(
    self_product: Product,
    rival_products: list[Product],
    top_k: int = 5,
    spec_mismatch_penalty: float = 12.0,
) -> list[tuple[Product, float, str]]:
    """为单个我方商品召回候选竞品，返回按分数降序的 (竞品, 分数, 依据)。"""
    if not rival_products:
        return []

    # 品类分桶：品类都识别出来且不同的，直接排除，避免跨品类误配
    pool = [
        r
        for r in rival_products
        if not (self_product.category and r.category and self_product.category != r.category)
    ]
    if not pool:
        pool = rival_products

    # 先用 rapidfuzz 批量粗筛，只对粗筛结果做精算，避免 O(n²) 全量精算
    choices = {i: r.norm_title for i, r in enumerate(pool) if r.norm_title}
    if not choices:
        return []
    rough = process.extract(
        self_product.norm_title,
        choices,
        scorer=fuzz.token_set_ratio,
        limit=max(top_k * 4, 20),
    )

    scored: list[tuple[Product, float, str]] = []
    for _, _, idx in rough:
        rival = pool[idx]

        # 口味/属性冲突的候选直接剔除。这类商品文本相似度往往很高
        # （只差两个字），靠分数阈值挡不住，必须硬排除。
        if conflict := variant_conflict(self_product.title, rival.title):
            continue

        base = text_score(self_product, rival)
        penalty, spec_note = spec_penalty(self_product, rival, spec_mismatch_penalty)
        reason = f"文本相似 {base:.1f}"
        if spec_note:
            reason += f"，{spec_note} 扣 {penalty:.1f}"

        # 产品线不一致不硬排除（标题可能只是省略了系列名），但要扣够分，
        # 把候选压进灰区交 L3 或人工判定，而不是直接当成匹配。
        if sub_note := sub_brand_mismatch(self_product.title, rival.title):
            penalty += _SUB_BRAND_PENALTY
            reason += f"，{sub_note} 扣 {_SUB_BRAND_PENALTY:.0f}"

        final = max(0.0, base - penalty)
        scored.append((rival, final, reason))

    scored.sort(key=lambda t: t[1], reverse=True)
    return scored[:top_k]


def to_pair(
    self_product: Product, candidate: tuple[Product, float, str]
) -> MatchPair:
    from ..models import MatchLevel

    rival, score, reason = candidate
    return MatchPair(
        self_product=self_product,
        rival_product=rival,
        score=score,
        level=MatchLevel.FUZZY,
        confidence=min(score / 100.0, 0.99),
        reason=reason,
    )
