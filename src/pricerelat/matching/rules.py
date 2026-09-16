"""L1 硬规则匹配。

命中即确信，不需要再往下走模糊和 LLM。覆盖率不高但准确率接近 100%，
能把大部分「老商品」在零成本下直接锁定，只把真正的新品推给下游。
"""

from __future__ import annotations

from collections import defaultdict

from rapidfuzz import fuzz

from ..models import MatchLevel, MatchPair, Product
from ..normalize.text import sub_brand_mismatch, variant_conflict


def _spec_key(p: Product) -> str:
    """规格指纹：单位类别 + 整包总量。500ml*6 与 3L 会得到同一个 key。"""
    if not p.spec.parsed:
        return ""
    return f"{p.spec.unit.value}:{round(p.spec.total_base, 3)}"


def match_by_barcode(
    self_products: list[Product], rival_products: list[Product]
) -> dict[str, MatchPair]:
    """条码匹配。同条码即同商品，这是最硬的证据。"""
    index: dict[str, Product] = {}
    for r in rival_products:
        code = (r.barcode or "").strip()
        if code:
            index.setdefault(code, r)

    out: dict[str, MatchPair] = {}
    for s in self_products:
        code = (s.barcode or "").strip()
        if code and code in index:
            out[s.uid] = MatchPair(
                self_product=s,
                rival_product=index[code],
                score=100.0,
                level=MatchLevel.BARCODE,
                confidence=1.0,
                reason=f"条码一致 {code}",
            )
    return out


# L1 品牌规格规则的标题相似度下限。
# 品牌/品类/规格全同但标题差异过大的，多半是同系列的不同单品，
# 不该占用 L1 的高可信度，退给 L2/L3 更稳妥。
_BRAND_SPEC_TITLE_FLOOR = 60.0


def match_by_brand_spec(
    self_products: list[Product], rival_products: list[Product]
) -> dict[str, MatchPair]:
    """品牌 + 品类 + 规格三者一致视为强匹配。

    但这三个维度不足以区分同规格的不同口味 —— 「康师傅红烧牛肉面 103g*5」
    和「康师傅香辣牛肉面 103g*5」在三个维度上完全一致。所以还要额外过两道关：
    变体（口味/脂肪/糖分等）不冲突，且标题相似度达到下限。

    只命中两个维度的情况交给 L2 模糊层处理。
    """
    index: dict[tuple[str, str, str], list[Product]] = defaultdict(list)
    for r in rival_products:
        if not r.brand or not r.category:
            continue
        key = (r.brand.lower(), r.category, _spec_key(r))
        if key[2]:
            index[key].append(r)

    out: dict[str, MatchPair] = {}
    for s in self_products:
        if not s.brand or not s.category:
            continue
        key = (s.brand.lower(), s.category, _spec_key(s))
        if not key[2]:
            continue
        candidates = index.get(key)
        # 命中多个候选说明规则不足以区分，退给模糊层打分
        if not candidates or len(candidates) != 1:
            continue

        rival = candidates[0]

        # 口味/属性冲突 —— 规则层直接放弃，让 L2/L3 去判
        if variant_conflict(s.title, rival.title):
            continue

        # 产品线不一致（特仑苏 vs 蒙牛纯牛奶）同样不配拿 L1 的可信度
        if sub_brand_mismatch(s.title, rival.title):
            continue

        # 标题相似度下限，挡住同系列不同单品。
        # 这里刻意不用 token_set_ratio —— 它对「子集」返回满分，
        # 「蒙牛纯牛奶」是「蒙牛特仑苏纯牛奶」的子集会被判成 100 分。
        if fuzz.token_sort_ratio(s.norm_title, rival.norm_title) < _BRAND_SPEC_TITLE_FLOOR:
            continue

        out[s.uid] = MatchPair(
            self_product=s,
            rival_product=rival,
            score=96.0,
            level=MatchLevel.BRAND_SPEC,
            confidence=0.95,
            reason=f"品牌/品类/规格一致（{s.brand} · {s.category} · {s.spec.display()}）",
        )
    return out
