"""比价结果写入多维表格（仪表盘数据源）测试。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pricerelat.compare.engine import build_row
from pricerelat.ingest.base import enrich
from pricerelat.models import MatchLevel, MatchPair, Product
from pricerelat.report.feishu import platform_records, push_results, result_records
from pricerelat.review.bitable import Row

COMPETITORS = [{"key": "sams", "name": "山姆"}, {"key": "rtmart", "name": "大润发"}]
CFG = {"compare": {"benchmark": "min", "thresholds": {"critical_high": 0.15, "warn_high": 0.05}}}


class MemTable:
    def __init__(self, rows=None):
        self.rows = [Row(f"old{i}", r) for i, r in enumerate(rows or [])]

    def list_rows(self):
        return list(self.rows)

    def create_rows(self, rows):
        self.rows += [Row(f"new{i}", r) for i, r in enumerate(rows)]

    def delete_rows(self, ids):
        self.rows = [r for r in self.rows if r.record_id not in ids]

    def update_rows(self, updates):
        raise AssertionError("结果表只做整表替换")


def make(platform, sku, title, price):
    return enrich(Product(platform=platform, platform_name=platform, sku_id=sku, title=title, price=price))


def rows():
    me = make("self", "S1", "伊利 纯牛奶 250ml*16盒", 60.0)
    confirmed = make("rtmart", "R2", "伊利 纯牛奶 250ml*16盒", 50.0)
    pending = make("sams", "R1", "伊利 纯牛奶 250ml*16盒", 48.0)
    return [build_row(me, {
        "rtmart": MatchPair(self_product=me, rival_product=confirmed, level=MatchLevel.BARCODE),
        "sams": MatchPair(self_product=me, rival_product=pending, level=MatchLevel.FUZZY, need_review=True),
    }, CFG)]


def test_result_records():
    rec = result_records(rows(), COMPETITORS, "2026-W40")[0]
    assert rec["基准平台"] == "大润发"   # 只用已确认的竞品
    assert rec["价差率"] == pytest.approx(0.2)
    assert rec["建议动作"] == "建议降价(高优)" and rec["建议类别"] == "正式"
    assert rec["对比方式"] == "单位价" and rec["比价期次"] == "2026-W40"


def test_platform_records():
    recs = {r["竞品平台"]: r for r in platform_records(rows(), COMPETITORS, "2026-W40")}
    assert recs["大润发"]["匹配状态"] == "已确认"
    assert recs["山姆"]["匹配状态"] == "待复核"
    assert recs["山姆"]["价差率"] == pytest.approx(0.25)


def test_push_replaces_previous_period():
    results, platforms = MemTable([{"我方SKU": "OLD"}]), MemTable([{"我方SKU": "OLD"}])
    assert push_results(results, platforms, rows(), COMPETITORS, "2026-W40") == (1, 2)
    assert [r.fields["我方SKU"] for r in results.rows] == ["S1"]
    assert len(platforms.rows) == 2
