"""R5 评测集与准确率报告测试。"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pricerelat.evaluate import EvalSetError, evaluate, format_report, load_set, write_outputs

ROOT = Path(__file__).resolve().parents[1]
HEADER = "我方SKU,我方标题,我方品牌,我方规格,竞品平台,竞品SKU,竞品标题,竞品品牌,竞品规格,是否同款,备注\n"

CFG = {
    "matching": {
        "rules": {"enable_barcode": True, "enable_brand_spec": True},
        "fuzzy": {"top_k": 5, "auto_accept": 88, "reject_below": 55, "spec_mismatch_penalty": 12},
        "llm": {"enabled": False},
    }
}


def write_set(tmp_path, body):
    path = tmp_path / "set.csv"
    path.write_text(HEADER + body, encoding="utf-8")
    return path


def test_metrics_and_error_details(tmp_path):
    path = write_set(tmp_path, (
        "S1,农夫山泉 饮用天然水 550ml*24瓶,农夫山泉,,sams,R1,农夫山泉 天然水 550ml×24瓶,农夫山泉,,是,\n"
        "S2,康师傅 红烧牛肉面 103g*5包,康师傅,,sams,R2,康师傅 红烧牛肉面 103g*5包,康师傅,,否,故意标错\n"
        "S3,某牌 手工皂 100g,,,sams,R3,另一牌 沐浴露 1L,,,否,\n"
    ))
    result = evaluate(load_set(path), CFG)

    assert result.pair_count == 3
    assert result.positives == 1
    # S1 正确匹配；S2 被匹配但标注为无同款 → 错配；S3 未匹配且无同款 → 不计错误
    assert result.predicted == 2 and result.correct == 1
    assert result.precision() == pytest.approx(0.5)
    assert result.recall() == pytest.approx(1.0)
    assert sum(result.layer_recall(layer) for layer in result.by_layer) == pytest.approx(result.recall())
    assert [e["类型"] for e in result.errors] == ["错配"]
    assert result.errors[0]["我方SKU"] == "S2"
    assert not result.l3_enabled


def test_missed_match_reported(tmp_path):
    path = write_set(tmp_path, "S1,某牌 手工皂 100g,,,sams,R1,完全不同的标题 沐浴露 1L,,,是,\n")
    result = evaluate(load_set(path), CFG)
    assert result.recall() == 0
    assert result.errors[0]["类型"] == "漏配"


def test_outputs_written(tmp_path):
    path = write_set(tmp_path, "S1,农夫山泉 饮用天然水 550ml*24瓶,农夫山泉,,sams,R1,农夫山泉 天然水 550ml×24瓶,农夫山泉,,是,\n")
    result = evaluate(load_set(path), CFG)
    summary, errors = write_outputs(result, tmp_path / "out")
    data = json.loads(summary.read_text(encoding="utf-8"))
    assert data["总体"]["准确率"] == 1.0
    assert "未启用" in data["L3"]
    assert errors.exists()
    assert "未启用" in format_report(result)


def test_invalid_label_rejected(tmp_path):
    path = write_set(tmp_path, "S1,a,,,sams,R1,b,,,也许,\n")
    with pytest.raises(EvalSetError, match="是/否"):
        load_set(path)


def test_two_positives_on_same_platform_rejected(tmp_path):
    path = write_set(tmp_path, "S1,a,,,sams,R1,b,,,是,\nS1,a,,,sams,R2,c,,,是,\n")
    with pytest.raises(EvalSetError, match="只能有一个同款"):
        load_set(path)


def test_missing_columns_rejected(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("我方SKU,我方标题\nS1,a\n", encoding="utf-8")
    with pytest.raises(EvalSetError, match="缺少列"):
        load_set(path)


def test_seed_set_loads():
    """仓库内置的种子评测集可以正常加载与评测。"""
    data = load_set(ROOT / "data/eval/评测集.csv")
    assert data.pair_count >= 30
    result = evaluate(data, CFG)
    # 自动确认的匹配不应出错 —— 错配风险的底线
    assert result.auto_confirmed.precision() == 1.0
