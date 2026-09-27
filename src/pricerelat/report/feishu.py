"""把本期比价结果写入飞书多维表格，供仪表盘展示。

两张表都只放最新一期：每次运行整表替换。历史各期保存在本地关系库的
observations 表中，不在多维表格里累积，仪表盘的统计口径因此始终是「本期」。

    比价结果   每个我方商品一行：基准、价差率、建议动作、建议类别
    平台价差   每个「我方商品 × 竞品平台」匹配一行：用于分平台对比
"""

from __future__ import annotations

from ..compare.engine import pair_diff_rate
from ..models import CompareRow
from ..review.bitable import ReviewTable


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


def result_records(rows: list[CompareRow], competitors: list[dict], period: str) -> list[dict]:
    names = {c["key"]: c["name"] for c in competitors}
    out = []
    for r in rows:
        sp = r.self_product
        if r.diff_rate is None:
            method = "无"
        else:
            method = "单位价" if r.comparable else "标价"
        out.append({
            "我方商品": sp.title,
            "比价期次": period,
            "我方SKU": sp.sku_id,
            "品牌": sp.brand,
            "品类": sp.category or "未分类",
            "规格": sp.spec.display(),
            "我方价": sp.price,
            "我方单位价": _round(sp.unit_price),
            "单位": sp.unit_price_label,
            "基准平台": names.get(r.benchmark_platform, r.benchmark_platform),
            "基准单位价": _round(r.benchmark_unit_price),
            "价差率": _round(r.diff_rate),
            "建议动作": r.action.value,
            "建议类别": r.tier.value,
            "对比方式": method,
            "备注": "；".join(r.notes),
        })
    return out


def platform_records(rows: list[CompareRow], competitors: list[dict], period: str) -> list[dict]:
    names = {c["key"]: c["name"] for c in competitors}
    out = []
    for r in rows:
        sp = r.self_product
        for key, pair in r.rivals.items():
            if not pair.matched:
                continue
            rp = pair.rival_product
            out.append({
                "我方商品": sp.title,
                "比价期次": period,
                "我方SKU": sp.sku_id,
                "品类": sp.category or "未分类",
                "竞品平台": names.get(key, key),
                "竞品SKU": rp.sku_id,
                "竞品商品": rp.title,
                "竞品价": rp.price,
                "价差率": _round(pair_diff_rate(sp, rp)),
                "匹配状态": "已确认" if pair.confirmed else "待复核",
                "匹配层级": pair.level.value,
            })
    return out


def _replace(table: ReviewTable, records: list[dict]) -> None:
    old = [row.record_id for row in table.list_rows()]
    if old:
        table.delete_rows(old)
    if records:
        table.create_rows(records)


def push_results(
    results: ReviewTable,
    platforms: ReviewTable,
    rows: list[CompareRow],
    competitors: list[dict],
    period: str,
) -> tuple[int, int]:
    """整表替换为本期结果，返回 (比价结果行数, 平台价差行数)。"""
    res = result_records(rows, competitors, period)
    plat = platform_records(rows, competitors, period)
    _replace(results, res)
    _replace(platforms, plat)
    return len(res), len(plat)
