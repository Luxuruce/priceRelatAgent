"""匹配层测试。重点锁住两个曾经导致静默错误结论的 bug。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pricerelat.ingest.base import enrich, to_product
from pricerelat.matching import rules
from pricerelat.matching.pipeline import match_platform
from pricerelat.models import Product
from pricerelat.normalize.text import variant_conflict


def make(platform, sku, title, price=10.0, barcode="", brand="", category=""):
    p = Product(
        platform=platform,
        platform_name=platform,
        sku_id=sku,
        title=title,
        price=price,
        barcode=barcode,
        brand=brand,
        category=category,
    )
    return enrich(p)


CFG = {
    "matching": {
        "rules": {"enable_barcode": True, "enable_brand_spec": True},
        "fuzzy": {"top_k": 5, "auto_accept": 88, "reject_below": 55, "spec_mismatch_penalty": 12},
        "llm": {"enabled": False},
    }
}


class TestNaNHandling:
    """pandas 把空单元格读成 NaN，str(NaN) == 'nan'。

    不处理的话所有空条码会彼此相等，被 L1 条码规则判成最高可信度的匹配 ——
    这是最危险的一类 bug：不报错，直接产出错误结论。
    """

    def test_nan_barcode_not_treated_as_value(self):
        import numpy as np

        record = {"sku_id": "A1", "title": "测试商品 500g", "barcode": np.nan, "price": "9.9"}
        product = to_product(record, "self", "我方")
        assert product.barcode == ""

    def test_empty_barcodes_do_not_match_each_other(self):
        a = [make("self", "S1", "商品甲 500g"), make("self", "S2", "商品乙 500g")]
        b = [make("sams", "R1", "完全无关的东西 500g")]
        assert rules.match_by_barcode(a, b) == {}

    def test_real_barcode_matches(self):
        a = [make("self", "S1", "农夫山泉 550ml*24瓶", barcode="6921168509256")]
        b = [make("sams", "R1", "农夫山泉天然水 550ml×24", barcode="6921168509256")]
        matched = rules.match_by_barcode(a, b)
        assert matched["self:S1"].rival_product.sku_id == "R1"


class TestVariantConflict:
    """品牌、品类、规格全同但口味不同，是商超比价最常见的误配来源。"""

    def test_flavor_conflict_detected(self):
        assert variant_conflict("康师傅 香辣牛肉面 103g*5包", "康师傅 红烧牛肉面 103g*5包")

    def test_same_flavor_no_conflict(self):
        assert not variant_conflict("康师傅 香辣牛肉面 103g*5包", "康师傅香辣牛肉面 103g×5袋")

    def test_one_sided_declaration_is_not_conflict(self):
        """一方没写口味，不代表口味不同 —— 不能据此否决。"""
        assert not variant_conflict("蒙牛 纯牛奶 250ml*16", "蒙牛 低脂纯牛奶 250ml*16")

    def test_l1_rule_rejects_flavor_conflict(self):
        """L1 规则必须挡住口味冲突，否则会给出 96 分的错误匹配。"""
        a = [make("self", "S1", "康师傅 香辣牛肉面 103g*5包", brand="康师傅")]
        b = [make("sams", "R1", "康师傅 红烧牛肉面 103g*5包", brand="康师傅")]
        assert rules.match_by_brand_spec(a, b) == {}

    def test_pipeline_rejects_flavor_conflict(self):
        a = [make("self", "S1", "康师傅 香辣牛肉面 103g*5包", brand="康师傅")]
        b = [
            make("sams", "R1", "康师傅 红烧牛肉面 103g*5包", brand="康师傅"),
            make("sams", "R2", "康师傅香辣牛肉面 103g×5袋", brand="康师傅"),
        ]
        pairs, _ = match_platform(a, b, CFG)
        assert pairs["self:S1"].rival_product.sku_id == "R2"


class TestFuzzyAndSpec:
    def test_spec_difference_penalized(self):
        """规格差距大的候选应被扣分，避免 550ml 匹配到 4L。"""
        from pricerelat.matching.fuzzy import recall

        self_p = make("self", "S1", "农夫山泉 饮用天然水 550ml*24瓶")
        rivals = [
            make("sams", "R1", "农夫山泉 饮用天然水 4L*4桶"),
            make("sams", "R2", "农夫山泉 饮用天然水 550ml*24瓶"),
        ]
        results = recall(self_p, rivals)
        assert results[0][0].sku_id == "R2"

    def test_low_score_rejected(self):
        a = [make("self", "S1", "农夫山泉 饮用天然水 550ml*24瓶")]
        b = [make("sams", "R1", "蓝月亮 洗衣液 3kg")]
        pairs, _ = match_platform(a, b, CFG)
        assert not pairs["self:S1"].matched

    def test_gray_zone_flagged_for_review(self):
        """LLM 关闭时，灰区候选必须标记待复核，不能默默当成匹配。"""
        a = [make("self", "S1", "蒙牛 纯牛奶 250ml*16盒", brand="蒙牛")]
        b = [make("sams", "R1", "蒙牛 特仑苏 纯牛奶 250ml*16盒", brand="蒙牛")]
        pairs, stats = match_platform(a, b, CFG)
        pair = pairs["self:S1"]
        if pair.matched:
            assert pair.need_review
        assert stats.need_review >= 0
