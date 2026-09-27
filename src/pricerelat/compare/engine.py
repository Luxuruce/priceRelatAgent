"""比价计算。

核心是「可比性」：只有双方规格都解析成功且可比口径相同，才用单位价对比；
否则降级为标价对比并在报告中标注，避免给出误导性的价差结论。

其次是「可信度」：跟价基准只取已确认的匹配，产出正式建议。只有排除待复核
匹配后没有基准时，才带上它们计算，产出待确认建议 —— 不进高优清单，
避免因错配而错误降价。
"""

from __future__ import annotations

from ..models import (
    Action,
    CompareRow,
    MatchPair,
    Product,
    SuggestionTier,
    measure_mismatch_note,
    same_measure,
)


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
        if p.unit_price is not None and same_measure(p.spec, self_product.spec)
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


def pair_diff_rate(self_product: Product, rival: Product) -> float | None:
    """我方相对单个竞品的价差率：口径一致按单位价，否则按标价。缺价返回 None。"""
    if rival.price is None or self_product.price is None:
        return None
    if (
        self_product.unit_price is not None
        and rival.unit_price is not None
        and same_measure(self_product.spec, rival.spec)
    ):
        return (self_product.unit_price - rival.unit_price) / rival.unit_price
    return (self_product.price - rival.price) / rival.price if rival.price else None


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


def _price_against(
    row: CompareRow, rivals: dict[str, MatchPair], mode: str, thresholds: dict
) -> bool:
    """用给定的竞品集合计算基准与价差，写入 row。没有可用基准返回 False。"""
    self_product = row.self_product
    bench_key, bench = _pick_benchmark(rivals, self_product, mode)
    if bench is None:
        return False

    row.benchmark_platform = bench_key
    row.benchmark_price = bench.price
    row.benchmark_unit_price = bench.unit_price

    # avg 模式：基准价取参与竞品的均值，bench 仅作为代表平台展示
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
            and same_measure(p.rival_product.spec, self_product.spec)
        ]
        row.benchmark_unit_price = (
            sum(unit_prices) / len(unit_prices) if unit_prices else None
        )

    self_unit = self_product.unit_price
    bench_unit = row.benchmark_unit_price

    # 单位价可比的条件：双方都解析出规格，且可比口径一致（单位类别 + 计数单位）
    if (
        self_unit is not None
        and bench_unit is not None
        and same_measure(self_product.spec, bench.spec)
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
            row.notes.append(f"{measure_mismatch_note(self_product.spec, bench.spec)}，按标价对比")

    row.action = _decide_action(row.diff_rate, thresholds)
    return True


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

    confirmed = {k: p for k, p in rivals.items() if p.confirmed}
    pending = {k: p for k, p in rivals.items() if p.matched and p.need_review}

    # 正式建议：只用已确认的竞品
    if _price_against(row, confirmed, mode, thresholds):
        if pending:
            row.notes.append(f"{len(pending)} 个待复核竞品未参与基准")
        return row

    # 待确认建议：带上待复核竞品才有基准
    if pending and _price_against(row, {**confirmed, **pending}, mode, thresholds):
        row.tier = SuggestionTier.TENTATIVE
        row.depends_on = _dependencies(row, pending, mode)
        row.notes.append("依赖待复核匹配，确认后转为正式建议")
        return row

    row.notes.append("无有效竞品匹配")
    return row


def _dependencies(row: CompareRow, pending: dict[str, MatchPair], mode: str) -> list[str]:
    """待确认建议依赖哪些待复核匹配：min/指定平台只依赖基准平台，avg 依赖全部参与者。"""
    if mode == "avg":
        return [k for k, p in pending.items() if p.rival_product.price is not None]
    return [row.benchmark_platform] if row.benchmark_platform in pending else []


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
