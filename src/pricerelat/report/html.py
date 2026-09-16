"""HTML 比价报告生成。

报告要回答采购最关心的三个问题：
    1. 我方有多少商品比竞品贵？贵多少？
    2. 哪些商品最该立刻调价？
    3. 匹配关系可信吗？哪些需要人工看一眼？
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..models import Action, CompareRow

_TEMPLATE_DIR = Path(__file__).parent


def _summarize(rows: list[CompareRow], competitors: list[dict]) -> dict:
    """汇总统计。报告顶部的 KPI 和图表都吃这份数据。"""
    total = len(rows)
    priced = [r for r in rows if r.diff_rate is not None]
    matched_any = [r for r in rows if any(p.matched for p in r.rivals.values())]

    higher = [r for r in priced if r.diff_rate > 0]
    lower = [r for r in priced if r.diff_rate < 0]
    need_review = [r for r in rows if "存在待复核的匹配关系" in r.notes]

    by_action: dict[str, int] = {}
    for r in rows:
        by_action[r.action.value] = by_action.get(r.action.value, 0) + 1

    # 分平台：匹配数、平均价差率
    per_platform = []
    for c in competitors:
        key, name = c["key"], c["name"]
        pairs = [r.rivals[key] for r in rows if key in r.rivals]
        matched = [p for p in pairs if p.matched]
        rates = []
        for r in rows:
            pair = r.rivals.get(key)
            if not (pair and pair.matched):
                continue
            sp, rp = r.self_product, pair.rival_product
            if rp.price is None or sp.price is None:
                continue
            if (
                sp.unit_price is not None
                and rp.unit_price is not None
                and sp.spec.unit is rp.spec.unit
            ):
                rates.append((sp.unit_price - rp.unit_price) / rp.unit_price)
            else:
                rates.append((sp.price - rp.price) / rp.price)
        per_platform.append(
            {
                "key": key,
                "name": name,
                "matched": len(matched),
                "total": len(pairs),
                "match_rate": len(matched) / len(pairs) if pairs else 0.0,
                "avg_diff_rate": sum(rates) / len(rates) if rates else None,
            }
        )

    # 价差率分布直方图：以 0 为中心的发散分箱
    edges = [-0.30, -0.15, -0.05, 0.05, 0.15, 0.30]
    labels = ["低 30%+", "低 15~30%", "低 5~15%", "持平 ±5%", "高 5~15%", "高 15~30%", "高 30%+"]
    bins = [0] * len(labels)
    for r in priced:
        rate = r.diff_rate
        idx = len(edges)
        for i, edge in enumerate(edges):
            if rate < edge:
                idx = i
                break
        bins[idx] += 1

    return {
        "total": total,
        "matched": len(matched_any),
        "match_rate": len(matched_any) / total if total else 0.0,
        "priced": len(priced),
        "higher": len(higher),
        "lower": len(lower),
        "need_review": len(need_review),
        "avg_diff_rate": (
            sum(r.diff_rate for r in priced) / len(priced) if priced else None
        ),
        "by_action": by_action,
        "per_platform": per_platform,
        # 键名避开 dict.values —— Jinja 的属性查找会优先命中方法
        "histogram": {"labels": labels, "counts": bins},
    }


def _row_payload(row: CompareRow, competitors: list[dict]) -> dict:
    """把 CompareRow 摊平成模板和前端筛选都好用的扁平结构。"""
    sp = row.self_product
    rivals = []
    for c in competitors:
        pair = row.rivals.get(c["key"])
        if pair and pair.matched:
            rp = pair.rival_product
            rivals.append(
                {
                    "name": c["name"],
                    "matched": True,
                    "title": rp.title,
                    "price": rp.price,
                    "unit_price": rp.unit_price,
                    "spec": rp.spec.display(),
                    "level": pair.level.value,
                    "score": round(pair.score, 1),
                    "confidence": round(pair.confidence, 2),
                    "reason": pair.reason,
                    "need_review": pair.need_review,
                    "url": rp.url,
                }
            )
        else:
            rivals.append(
                {
                    "name": c["name"],
                    "matched": False,
                    "reason": pair.reason if pair else "未参与匹配",
                }
            )

    return {
        "sku_id": sp.sku_id,
        "title": sp.title,
        "brand": sp.brand,
        "category": sp.category or "未分类",
        "spec": sp.spec.display(),
        "price": sp.price,
        "unit_price": sp.unit_price,
        "unit_label": sp.unit_price_label,
        "benchmark_platform": row.benchmark_platform,
        "benchmark_price": row.benchmark_price,
        "benchmark_unit_price": row.benchmark_unit_price,
        "diff": row.diff,
        "diff_rate": row.diff_rate,
        "action": row.action.value,
        "action_kind": _action_kind(row.action),
        "comparable": row.comparable,
        "notes": row.notes,
        "rivals": rivals,
    }


def _action_kind(action: Action) -> str:
    """映射到状态色 role，供模板选样式类。"""
    return {
        Action.CUT_PRICE_URGENT: "critical",
        Action.CUT_PRICE: "serious",
        Action.WATCH: "neutral",
        Action.RAISE_PRICE: "good",
        Action.NO_DATA: "muted",
    }[action]


def render(
    rows: list[CompareRow],
    cfg: dict,
    output_path: str | Path | None = None,
) -> Path:
    """生成 HTML 报告，返回文件路径。"""
    competitors = cfg.get("competitors", [])
    r_cfg = cfg.get("report", {})

    payload = [_row_payload(r, competitors) for r in rows]
    summary = _summarize(rows, competitors)

    # 价差 TOP N：按「我方贵出多少」降序，这是最该调价的一批
    top_n = r_cfg.get("top_n", 20)
    top_rows = sorted(
        [p for p in payload if p["diff_rate"] is not None],
        key=lambda p: p["diff_rate"],
        reverse=True,
    )[:top_n]

    env = Environment(
        loader=FileSystemLoader(_TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("template.html")

    html = template.render(
        project=cfg.get("project", {}),
        competitors=competitors,
        summary=summary,
        rows=payload,
        top_rows=top_rows,
        rows_json=json.dumps(payload, ensure_ascii=False),
        summary_json=json.dumps(summary, ensure_ascii=False),
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        thresholds=cfg.get("compare", {}).get("thresholds", {}),
    )

    if output_path is None:
        out_dir = Path(r_cfg.get("output_dir", "data/output"))
        out_dir.mkdir(parents=True, exist_ok=True)
        output_path = out_dir / r_cfg.get("html_name", "比价报告.html")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    return output_path
