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
