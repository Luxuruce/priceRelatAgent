"""规格解析测试。比价的正确性完全建立在这一层之上。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pricerelat.models import Unit
from pricerelat.normalize.spec_parser import parse_spec


@pytest.mark.parametrize(
    "title,unit,total,count",
    [
        ("农夫山泉 饮用天然水 550ml*24瓶", Unit.VOLUME, 13200, 24),
        ("可口可乐 汽水 330ml×6罐", Unit.VOLUME, 1980, 6),
        ("金龙鱼 食用调和油 5L", Unit.VOLUME, 5000, 1),
        ("伊利 纯牛奶 250ml 12盒装", Unit.VOLUME, 3000, 12),
        ("三只松鼠 每日坚果 750g/盒", Unit.WEIGHT, 750, 1),
        ("净含量：500g", Unit.WEIGHT, 500, 1),
        ("五常大米 2斤", Unit.WEIGHT, 1000, 1),
        ("雀巢咖啡 1kg", Unit.WEIGHT, 1000, 1),
        ("24*500ml 整箱", Unit.VOLUME, 12000, 24),
        ("红富士苹果 5个装", Unit.COUNT, 5, 5),
        ("抽纸 6连包", Unit.COUNT, 6, 6),
    ],
)
def test_parse(title, unit, total, count):
    spec = parse_spec(title)
    assert spec.parsed, f"未能解析：{title}"
    assert spec.unit is unit
    assert spec.total_base == pytest.approx(total)
    assert spec.count == count


def test_mg_converts_to_grams():
    """1000mg 应折算为 1g，而不是 1000g。"""
    spec = parse_spec("维生素C 1000mg*60片")
    assert spec.unit is Unit.WEIGHT
    assert spec.qty_base == pytest.approx(1.0)
    assert spec.total_base == pytest.approx(60.0)
    assert "1g×60" in spec.display()


def test_promo_noise_ignored():
    """促销文案里的数字不能被当成规格。"""
    spec = parse_spec("第二件5折 康师傅红烧牛肉面 103g*5包")
    assert spec.total_base == pytest.approx(515.0)
    assert spec.count == 5


def test_unparsable_returns_false():
    assert not parse_spec("999感冒灵颗粒").parsed
    assert not parse_spec("乐事 薯片 原味 大包装").parsed


def test_spec_text_takes_priority():
    """平台规格字段比标题更可信。"""
    spec = parse_spec("某商品 促销中", spec_text="500ml*6瓶")
    assert spec.total_base == pytest.approx(3000.0)


# ------------------------------------------------------------------
# R4 计数类内含量折算
# ------------------------------------------------------------------

from pricerelat.compare.engine import build_row
from pricerelat.ingest.base import enrich
from pricerelat.models import MatchLevel, MatchPair, Product


@pytest.mark.parametrize(
    "title,content_unit,total,count",
    [
        ("清风 抽纸 150抽×24包", "抽", 3600, 24),
        ("维达 3层130抽 24包", "抽", 3120, 24),
        ("24包*150抽", "抽", 3600, 24),
        ("心相印 手帕纸 10张*18包", "张", 180, 18),
        ("湿巾 80片*3包", "片", 240, 3),
        ("卷纸 10卷×2提", "卷", 20, 2),
        ("卷纸 10卷", "卷", 10, 1),
        ("面膜 5片装", "片", 5, 1),
        ("鱼油 100粒", "粒", 100, 1),
    ],
)
def test_parse_content_unit(title, content_unit, total, count):
    spec = parse_spec(title)
    assert spec.unit is Unit.COUNT
    assert spec.content_unit == content_unit
    assert spec.total_base == pytest.approx(total)
    assert spec.count == count


def test_measure_beats_content_unit():
    """净含量优先：「140g×10卷」按重量比，不按卷。"""
    spec = parse_spec("卷纸 140g×10卷")
    assert spec.unit is Unit.WEIGHT
    assert spec.content_unit == ""


def _product(platform, title, price):
    return enrich(Product(platform=platform, platform_name=platform, sku_id=title, title=title, price=price))


def _row(self_title, self_price, rival_title, rival_price):
    me = _product("self", self_title, self_price)
    rival = _product("sams", rival_title, rival_price)
    pair = MatchPair(self_product=me, rival_product=rival, score=100, level=MatchLevel.BARCODE, confidence=1.0)
    return me, build_row(me, {"sams": pair}, {"compare": {"benchmark": "min"}})


def test_tissue_unit_price_per_100_draws():
    p = _product("self", "清风 抽纸 150抽×24包", 72.0)
    assert p.unit_price == pytest.approx(2.0)
    assert p.unit_price_label == "元/100抽"


def test_roll_and_piece_priced_per_unit():
    assert _product("self", "卷纸 10卷", 30.0).unit_price_label == "元/卷"
    assert _product("self", "面膜 5片装", 50.0).unit_price == pytest.approx(10.0)
    assert _product("self", "面膜 5片装", 50.0).unit_price_label == "元/片"
    assert _product("self", "抽纸 6连包", 30.0).unit_price_label == "元/件"


def test_different_draw_counts_compared_per_100():
    """130抽×24包 与 150抽×24包 按每 100 抽比较。"""
    _, row = _row("维达 130抽×24包", 62.4, "清风 150抽×24包", 72.0)
    assert row.comparable
    # 我方 62.4/3120*100 = 2.0，竞品 72/3600*100 = 2.0
    assert row.diff_rate == pytest.approx(0.0)


def test_draws_vs_packs_not_comparable():
    """「150抽×24包」与「24包」计数口径不同，降级为标价对比并写明原因。"""
    _, row = _row("清风 抽纸 150抽×24包", 72.0, "清风 抽纸 24包", 60.0)
    assert not row.comparable
    assert any("计数单位不同（抽 vs 件）" in n for n in row.notes)


def test_draws_vs_sheets_not_comparable():
    _, row = _row("抽纸 150抽×24包", 72.0, "抽纸 150张×24包", 60.0)
    assert not row.comparable
    assert any("抽 vs 张" in n for n in row.notes)
