"""R1 匹配关系库测试。"""

import csv
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pricerelat.ingest.base import enrich
from pricerelat.matching.pipeline import classify, match_platform
from pricerelat.models import MatchLevel, MatchPair, Product, RelationStatus, ReviewReason
from pricerelat.store import Store, current_period

CFG = {
    "matching": {
        "rules": {"enable_barcode": True, "enable_brand_spec": True},
        "fuzzy": {"top_k": 5, "auto_accept": 88, "reject_below": 55, "spec_mismatch_penalty": 12},
        "llm": {"enabled": False},
    },
    "store": {"invalid_after_periods": 3},
}


def make(platform, sku, title, price=10.0, barcode=""):
    return enrich(
        Product(platform=platform, platform_name=platform, sku_id=sku, title=title,
                price=price, barcode=barcode)
    )


def self_items():
    return [
        make("self", "S1", "农夫山泉 饮用天然水 550ml*24瓶", 30, barcode="690001"),
        make("self", "S2", "伊利 纯牛奶 250ml*16盒", 50),
        make("self", "S3", "某小众品牌 手工皂 100g", 20),
    ]


def rival_items(**overrides):
    items = {
        "R1": make("sams", "R1", "农夫山泉 饮用天然水 550ml*24瓶", 28, barcode="690001"),
        "R2": make("sams", "R2", "伊利 纯牛奶 250ml*16盒", 48),
        "R3": make("sams", "R3", "伊利 纯牛奶 250ml*16盒 礼盒装", 55),
        "R4": make("sams", "R4", "蓝月亮 洗衣液 3kg", 60),
    }
    items.update(overrides)
    return [v for v in items.values() if v is not None]


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "test.db") as s:
        yield s


def run(store, rivals=None, period="2026-W40", selfs=None):
    result, stats = match_platform(selfs or self_items(), rivals or rival_items(), CFG,
                                   store=store, period=period)
    store.commit()
    return result, stats


def test_period_is_iso_week():
    assert current_period(date(2026, 9, 28)) == "2026-W40"
    assert current_period(date(2027, 1, 1)) == "2026-W53"


def test_second_run_skips_all_layers(store):
    """同一份输入连续运行两次，第二次进入 L1-L3 的商品数为 0。"""
    first, stats1 = run(store)
    assert stats1.layered == 3
    second, stats2 = run(store)
    assert stats2.layered == 0
    assert stats2.reused == 3
    # 复用结果与首次一致
    for uid, pair in first.items():
        assert (pair.rival_product and pair.rival_product.sku_id) == (
            second[uid].rival_product and second[uid].rival_product.sku_id
        )


def test_auto_confirmed_levels_written_as_confirmed(store):
    run(store)
    rel = store.get_relation("S1", "sams", "R1")
    assert rel.status is RelationStatus.CONFIRMED
    assert rel.source == MatchLevel.BARCODE.value
    assert rel.auto and rel.confirmed_by == "系统自动"
    assert rel.rival_title == "农夫山泉 饮用天然水 550ml*24瓶"


def test_rejected_rival_never_recommended_again(store):
    """人工否决 A↔B 后，下次运行 A 的候选中不出现 B。"""
    first, _ = run(store)
    matched = first["self:S2"].rival_product.sku_id
    rel = store.get_relation("S2", "sams", matched)
    rel.status = RelationStatus.REJECTED
    store.save_relation(rel)

    second, stats = run(store)
    assert stats.layered == 1
    pair = second["self:S2"]
    assert not (pair.rival_product and pair.rival_product.sku_id == matched)
    assert not (pair.candidate and pair.candidate.sku_id == matched)


def test_title_change_triggers_review(store):
    """修改竞品标题后运行，关系变为待复核，备注「商品信息变更」。"""
    run(store)
    changed = make("sams", "R1", "农夫山泉 饮用天然水 380ml*24瓶", 28, barcode="690001")
    result, stats = run(store, rival_items(R1=changed))

    rel = store.get_relation("S1", "sams", "R1")
    assert rel.status is RelationStatus.PENDING
    assert rel.review_reason == ReviewReason.INFO_CHANGED.value
    assert "550ml×24" in rel.change_note and "380ml×24" in rel.change_note

    pair = result["self:S1"]
    assert pair.need_review and pair.review_reason == ReviewReason.INFO_CHANGED.value
    # S1 复用关系库；竞品池变了，只有上次未匹配的 S3 重跑
    assert stats.layered == 1


def test_marketing_words_do_not_trigger_review(store):
    """只加营销词不算商品信息变更（C-5）。"""
    run(store)
    promo = make("sams", "R1", "【限时特价】农夫山泉 饮用天然水 550ml*24瓶", 26, barcode="690001")
    run(store, rival_items(R1=promo))
    assert store.get_relation("S1", "sams", "R1").status is RelationStatus.CONFIRMED


def test_missing_rival_invalid_after_n_periods(store):
    run(store, period="2026-W40")
    without_r1 = rival_items(R1=None)

    for i, period in enumerate(["2026-W41", "2026-W42"], start=1):
        result, stats = run(store, without_r1, period=period)
        rel = store.get_relation("S1", "sams", "R1")
        assert rel.status is RelationStatus.CONFIRMED
        assert rel.missing_count == i
        assert not result["self:S1"].matched
        assert "本期未采集到" in result["self:S1"].reason

    # 同一期重跑不重复计数
    run(store, without_r1, period="2026-W42")
    assert store.get_relation("S1", "sams", "R1").missing_count == 2

    run(store, without_r1, period="2026-W43")
    assert store.get_relation("S1", "sams", "R1").status is RelationStatus.INVALID


def test_missing_count_resets_when_rival_returns(store):
    run(store, period="2026-W40")
    run(store, rival_items(R1=None), period="2026-W41")
    run(store, period="2026-W42")
    assert store.get_relation("S1", "sams", "R1").missing_count == 0


def test_unmatched_rerun_when_pool_changes(store):
    _, stats = run(store)
    assert stats.layered == 3
    new_rival = make("sams", "R9", "某小众品牌 手工皂 100g", 18)
    _, stats = run(store, rival_items(R9=new_rival))
    # 只有上次未匹配的 S3 需要重跑
    assert stats.layered == 1


def test_export_relations_without_compare(store, tmp_path):
    run(store)
    out = tmp_path / "rel.csv"
    n = store.export_relations(out)
    rows = list(csv.DictReader(out.open(encoding="utf-8-sig")))
    assert n == len(rows) > 0
    assert {"self_sku", "rival_sku", "status", "confirmed_by"} <= set(rows[0])

    n_confirmed = store.export_relations(out, status=RelationStatus.CONFIRMED)
    assert n_confirmed <= n


def _pair(level, need_review=False, matched=True):
    p = make("self", "S", "x")
    return MatchPair(self_product=p, rival_product=p if matched else None, level=level,
                     need_review=need_review)


@pytest.mark.parametrize(
    "pair,status,reason",
    [
        (_pair(MatchLevel.BARCODE), RelationStatus.CONFIRMED, ""),
        (_pair(MatchLevel.BRAND_SPEC), RelationStatus.CONFIRMED, ""),
        (_pair(MatchLevel.FUZZY), RelationStatus.CONFIRMED, ""),
        # C-4：L3 高置信也要人工确认
        (_pair(MatchLevel.LLM), RelationStatus.PENDING, ReviewReason.AI_UNCONFIRMED.value),
        (_pair(MatchLevel.LLM, need_review=True), RelationStatus.PENDING, ReviewReason.AI_LOW_CONFIDENCE.value),
        (_pair(MatchLevel.FUZZY, need_review=True), RelationStatus.PENDING, ReviewReason.GRAY_ZONE.value),
    ],
)
def test_classify(pair, status, reason):
    assert classify(pair) == (status, reason)


def test_without_store_behaves_like_v1():
    result, stats = match_platform(self_items(), rival_items(), CFG)
    assert stats.layered == 3
    assert all(p.status is None for p in result.values())
