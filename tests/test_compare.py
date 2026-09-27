"""R3 建议可信度分级测试。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pricerelat.compare.engine import build_row, build_rows
from pricerelat.ingest.base import enrich
from pricerelat.matching.pipeline import match_platform
from pricerelat.models import Action, MatchLevel, MatchPair, Product, RelationStatus, SuggestionTier
from pricerelat.report import html as report_html
from pricerelat.store import Store

CFG = {
    "matching": {
        "rules": {"enable_barcode": True, "enable_brand_spec": True},
        "fuzzy": {"top_k": 5, "auto_accept": 88, "reject_below": 55, "spec_mismatch_penalty": 12},
        "llm": {"enabled": False},
    },
    "compare": {
        "benchmark": "min",
        "thresholds": {"critical_high": 0.15, "warn_high": 0.05, "warn_low": -0.10},
    },
    "competitors": [{"key": "sams", "name": "山姆"}, {"key": "rtmart", "name": "大润发"}],
    "report": {"top_n": 20},
}


def make(platform, sku, title, price):
    return enrich(Product(platform=platform, platform_name=platform, sku_id=sku, title=title, price=price))


def pair(me, rival, need_review=False):
    return MatchPair(
        self_product=me, rival_product=rival, score=90, confidence=0.9,
        level=MatchLevel.FUZZY, need_review=need_review,
    )


ME = make("self", "S1", "伊利 纯牛奶 250ml*16盒", 60.0)


def test_only_cheap_rival_pending_gives_tentative():
    """唯一低价竞品为待复核匹配：不进高优，进待确认组并显示依赖。"""
    cheap = make("sams", "R1", "伊利 纯牛奶 250ml*16盒", 45.0)
    row = build_row(ME, {"sams": pair(ME, cheap, need_review=True)}, CFG)

    assert row.tier is SuggestionTier.TENTATIVE
    assert row.action is Action.CUT_PRICE_URGENT  # 按阈值的动作照算，但不进高优清单
    assert row.depends_on == ["sams"]
    assert any("依赖待复核匹配" in n for n in row.notes)


def test_benchmark_uses_confirmed_only():
    """同时有已确认与待复核竞品：基准只取已确认的，即使待复核的更便宜。"""
    confirmed = make("rtmart", "R2", "伊利 纯牛奶 250ml*16盒", 57.0)
    cheap_pending = make("sams", "R1", "伊利 纯牛奶 250ml*16盒", 40.0)
    row = build_row(
        ME,
        {"sams": pair(ME, cheap_pending, need_review=True), "rtmart": pair(ME, confirmed)},
        CFG,
    )
    assert row.tier is SuggestionTier.FORMAL
    assert row.benchmark_platform == "rtmart"
    assert row.action is Action.CUT_PRICE  # 60 vs 57 = +5.3%
    assert any("1 个待复核竞品未参与基准" in n for n in row.notes)


def test_avg_mode_tentative_depends_on_all_pending():
    cfg = {**CFG, "compare": {**CFG["compare"], "benchmark": "avg"}}
    a = make("sams", "R1", "伊利 纯牛奶 250ml*16盒", 50.0)
    b = make("rtmart", "R2", "伊利 纯牛奶 250ml*16盒", 54.0)
    row = build_row(
        ME, {"sams": pair(ME, a, need_review=True), "rtmart": pair(ME, b, need_review=True)}, cfg
    )
    assert row.tier is SuggestionTier.TENTATIVE
    assert sorted(row.depends_on) == ["rtmart", "sams"]


def test_no_match_at_all():
    row = build_row(ME, {"sams": MatchPair(self_product=ME, rival_product=None)}, CFG)
    assert row.tier is SuggestionTier.FORMAL
    assert row.action is Action.NO_DATA


def test_confirming_match_promotes_to_formal(tmp_path):
    """确认依赖的匹配后再运行，建议转为正式并按阈值归入对应动作。"""
    me = make("self", "S1", "蒙牛 特仑苏 纯牛奶 250ml*16盒", 80.0)
    # 仅一方标注产品线 → L2 扣分落入灰区，进入待复核
    rival = make("sams", "R1", "蒙牛 纯牛奶 250ml×16盒", 60.0)

    with Store(tmp_path / "t.db") as store:
        pairs, _ = match_platform([me], [rival], CFG, store=store, period="2026-W40")
        row = build_rows([me], {"sams": pairs}, CFG)[0]
        assert pairs[me.uid].need_review
        assert row.tier is SuggestionTier.TENTATIVE

        rel = store.get_relation("S1", "sams", "R1")
        rel.status = RelationStatus.CONFIRMED
        store.save_relation(rel)

        pairs, _ = match_platform([me], [rival], CFG, store=store, period="2026-W40")
        row = build_rows([me], {"sams": pairs}, CFG)[0]
        assert row.tier is SuggestionTier.FORMAL
        assert row.action is Action.CUT_PRICE_URGENT  # 80 vs 60 = +33%


def test_report_top_n_excludes_tentative(tmp_path):
    cheap = make("sams", "R1", "伊利 纯牛奶 250ml*16盒", 45.0)
    other = make("self", "S2", "金龙鱼 调和油 5L", 80.0)
    oil = make("rtmart", "R2", "金龙鱼 调和油 5L", 60.0)
    rows = [
        build_row(ME, {"sams": pair(ME, cheap, need_review=True)}, CFG),
        build_row(other, {"rtmart": pair(other, oil)}, CFG),
    ]
    out = report_html.render(rows, CFG, tmp_path / "r.html")
    html = out.read_text(encoding="utf-8")

    top = html.split("最该调价的商品")[1].split("待确认建议")[0]
    assert "金龙鱼" in top and "伊利" not in top
    tentative = html.split("待确认建议 · 1 个")[1].split("全部比价明细")[0]
    assert "伊利" in tentative and "山姆：伊利 纯牛奶" in tentative
