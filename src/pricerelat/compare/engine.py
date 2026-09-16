"""比价计算。

核心是「可比性」：只有双方规格都解析成功且单位类别相同，才用单位价对比；
否则降级为标价对比并在报告中标注，避免给出误导性的价差结论。
"""

from __future__ import annotations

from ..models import Action, CompareRow, MatchPair, Product


def _pick_benchmark(
    rivals: dict[str, MatchPair], self_product: Product, mode: str
) -> tuple[str, Product | None]:
    """选跟价基准。

    min = 最低单位价的竞品（最激进，保证不被任何竞品压价）
    avg = 竞品均价（稳健，不被单个异常低价带偏）
    其他 = 指定平台 key
    """
    valid = [
        (key, pair.rival_product)
        for key, pair in rivals.items()
        if pair.matched and pair.rival_product.price is not None
    ]
    if not valid:
        return "", None

    if mode not in ("min", "avg"):
        for key, prod in valid:
            if key == mode:
                return key, prod
        return "", None

    # 优先用单位价可比的竞品；都不可比时退回标价
    comparable = [
        (k, p)
        for k, p in valid
        if p.unit_price is not None and p.spec.unit is self_product.spec.unit
    ]
    pool = comparable or valid
    key_fn = (
        (lambda kp: kp[1].unit_price) if comparable else (lambda kp: kp[1].price)
    )

    if mode == "min":
        return min(pool, key=key_fn)

    # avg 模式返回「最接近均价」的那个竞品作为代表，价格另行按均值计算
    avg = sum(key_fn(kp) for kp in pool) / len(pool)
    return min(pool, key=lambda kp: abs(key_fn(kp) - avg))


def _decide_action(diff_rate: float | None, thresholds: dict) -> Action:
    if diff_rate is None:
        return Action.NO_DATA
    if diff_rate >= thresholds.get("critical_high", 0.15):
        return Action.CUT_PRICE_URGENT
    if diff_rate >= thresholds.get("warn_high", 0.05):
        return Action.CUT_PRICE
    if diff_rate <= thresholds.get("warn_low", -0.10):
        return Action.RAISE_PRICE
    return Action.WATCH


def build_row(
    self_product: Product,
    rivals: dict[str, MatchPair],
    cfg: dict,
) -> CompareRow:
    """为一个我方商品生成比价行。"""
    c_cfg = cfg.get("compare", {})
    mode = c_cfg.get("benchmark", "min")
    thresholds = c_cfg.get("thresholds", {})

    row = CompareRow(self_product=self_product, rivals=rivals)

    if self_product.price is None:
        row.notes.append("我方缺少价格")
        return row

    bench_key, bench = _pick_benchmark(rivals, self_product, mode)
    if bench is None:
        row.notes.append("无有效竞品匹配")
        return row

    row.benchmark_platform = bench_key
    row.benchmark_price = bench.price
    row.benchmark_unit_price = bench.unit_price

    # avg 模式：基准价取所有匹配竞品的均值，bench 仅作为代表平台展示
    if mode == "avg":
        prices = [
            p.rival_product.price
            for p in rivals.values()
            if p.matched and p.rival_product.price is not None
        ]
        row.benchmark_price = sum(prices) / len(prices)
        unit_prices = [
            p.rival_product.unit_price
            for p in rivals.values()
            if p.matched
            and p.rival_product.unit_price is not None
            and p.rival_product.spec.unit is self_product.spec.unit
        ]
        row.benchmark_unit_price = (
            sum(unit_prices) / len(unit_prices) if unit_prices else None
        )

    self_unit = self_product.unit_price
    bench_unit = row.benchmark_unit_price

    # 单位价可比的条件：双方都解析出规格，且单位类别一致
    if (
        self_unit is not None
        and bench_unit is not None
        and self_product.spec.unit is bench.spec.unit
    ):
        row.comparable = True
        row.diff = self_unit - bench_unit
        row.diff_rate = row.diff / bench_unit if bench_unit else None
    else:
        # 降级：只比标价。规格不同的情况下这个结论仅供参考
        row.comparable = False
        row.diff = self_product.price - row.benchmark_price
        row.diff_rate = (
            row.diff / row.benchmark_price if row.benchmark_price else None
        )
        if not self_product.spec.parsed:
            row.notes.append("我方规格未解析，按标价对比")
        elif not bench.spec.parsed:
            row.notes.append("竞品规格未解析，按标价对比")
        else:
            row.notes.append("单位类别不同，按标价对比")

    row.action = _decide_action(row.diff_rate, thresholds)

    # 低置信匹配的比价结论不可全信，提醒复核
    if any(p.need_review for p in rivals.values() if p.matched):
        row.notes.append("存在待复核的匹配关系")

    return row


def build_rows(
    self_products: list[Product],
    matches: dict[str, dict[str, MatchPair]],
    cfg: dict,
) -> list[CompareRow]:
    """批量生成比价行。

    Args:
        matches: {平台key: {我方商品uid: MatchPair}}
    """
    rows = []
    for p in self_products:
        rivals = {
            platform: pairs[p.uid]
            for platform, pairs in matches.items()
            if p.uid in pairs
        }
        rows.append(build_row(p, rivals, cfg))
    return rows
