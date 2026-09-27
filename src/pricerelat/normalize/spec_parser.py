"""商品规格解析。

比价的前提是「可比」。500ml×6瓶 和 1.5L 标价没有可比性，
必须先把规格折算成统一基准单位，再算单位价。

解析优先级：平台规格字段 > 商品标题。
解析结果全部折算到：重量→克，体积→毫升，计数→件。
纸品日化的「150抽×24包」按内含量折算为 3600 抽，而不是 24 件。
"""

from __future__ import annotations

import re
import unicodedata

from ..models import Spec, Unit

# ------------------------------------------------------------------
# 单位表：单位文本 -> (类别, 折算到基准单位的系数)
# 按文本长度降序匹配，避免 "kg" 被 "g" 抢先命中
# ------------------------------------------------------------------
_MEASURE_UNITS: dict[str, tuple[Unit, float]] = {
    # 重量 → 克
    "mg": (Unit.WEIGHT, 0.001),
    "毫克": (Unit.WEIGHT, 0.001),
    "g": (Unit.WEIGHT, 1.0),
    "克": (Unit.WEIGHT, 1.0),
    "kg": (Unit.WEIGHT, 1000.0),
    "千克": (Unit.WEIGHT, 1000.0),
    "公斤": (Unit.WEIGHT, 1000.0),
    "斤": (Unit.WEIGHT, 500.0),
    "两": (Unit.WEIGHT, 50.0),
    "磅": (Unit.WEIGHT, 453.592),
    "lb": (Unit.WEIGHT, 453.592),
    "oz": (Unit.WEIGHT, 28.3495),
    "盎司": (Unit.WEIGHT, 28.3495),
    # 体积 → 毫升
    "ml": (Unit.VOLUME, 1.0),
    "mL": (Unit.VOLUME, 1.0),
    "毫升": (Unit.VOLUME, 1.0),
    "cl": (Unit.VOLUME, 10.0),
    "l": (Unit.VOLUME, 1000.0),
    "升": (Unit.VOLUME, 1000.0),
    "L": (Unit.VOLUME, 1000.0),
}

# 计数单位：只表示件数，不含净含量信息
_COUNT_UNITS = (
    "连包", "连杯", "件装", "只装", "个装", "片装", "袋装", "盒装", "包装",
    "瓶", "罐", "盒", "袋", "包", "支", "个", "只", "片", "条", "卷",
    "组", "入", "杯", "块", "枚", "把", "捆", "提", "听", "桶", "件", "双", "对",
)

# 内含量单位：纸品的抽/张、湿巾面膜的片、药品保健品的粒、卷纸的卷。
# 与普通件数（包/瓶）不可互相折算，解析后记在 Spec.content_unit
_CONTENT_UNITS = ("抽", "张", "片", "粒", "卷")
# 内含量后面跟的包数单位：「150抽×24包」「10卷×2提」
_PACK_UNITS = ("连包", "包", "提", "袋", "盒", "箱")

# 长度降序，保证最长匹配优先
_MEASURE_PATTERN = "|".join(
    re.escape(u) for u in sorted(_MEASURE_UNITS, key=len, reverse=True)
)
_COUNT_PATTERN = "|".join(
    re.escape(u) for u in sorted(_COUNT_UNITS, key=len, reverse=True)
)

_CONTENT_PATTERN = "|".join(map(re.escape, _CONTENT_UNITS))
_PACK_PATTERN = "|".join(
    re.escape(u) for u in sorted(_PACK_UNITS, key=len, reverse=True)
)

_NUM = r"\d+(?:\.\d+)?"

# 形如 500ml*24 / 500ml×24瓶 / 500g*6袋
_RE_MEASURE_MUL_COUNT = re.compile(
    rf"({_NUM})\s*({_MEASURE_PATTERN})\s*\*\s*(\d+)\s*(?:{_COUNT_PATTERN})?",
)
# 形如 24*500ml / 6瓶*500ml
_RE_COUNT_MUL_MEASURE = re.compile(
    rf"(\d+)\s*(?:{_COUNT_PATTERN})?\s*\*\s*({_NUM})\s*({_MEASURE_PATTERN})",
)
# 单独的净含量，形如 500g / 1.5L / 净含量：750克
_RE_MEASURE = re.compile(rf"({_NUM})\s*({_MEASURE_PATTERN})(?![a-zA-Z一-鿿])")
# 内含量 × 包数，形如 150抽*24包 / 80片*3 / 10卷*2提
_RE_CONTENT_MUL_PACK = re.compile(
    rf"(\d+)\s*({_CONTENT_PATTERN})\s*\*\s*(\d+)\s*(?:{_PACK_PATTERN})?",
)
# 包数 × 内含量，形如 24包*150抽
_RE_PACK_MUL_CONTENT = re.compile(
    rf"(\d+)\s*(?:{_PACK_PATTERN})\s*\*\s*(\d+)\s*({_CONTENT_PATTERN})",
)
# 单独的内含量，形如 150抽 / 10卷 / 5片装
_RE_CONTENT = re.compile(rf"(\d+)\s*({_CONTENT_PATTERN})(?:装)?")
# 单独的包数，与内含量分开出现时使用：「3层150抽 24包」
_RE_PACK = re.compile(rf"(\d+)\s*(?:{_PACK_PATTERN})")
# 单独的件数，形如 12盒 / 6连包 / 24瓶装
_RE_COUNT = re.compile(rf"(\d+)\s*({_COUNT_PATTERN})")

# 噪声：促销词里的数字容易误判成规格，解析前剔除
_NOISE = re.compile(
    r"(第二件\d*折|买\d+送\d+|满\d+减\d+|\d+折|直降\d+|立减\d+|券后|到手价)"
)


def normalize_text(text: str) -> str:
    """全角转半角、统一乘号、压缩空白。"""
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    s = s.replace("×", "*").replace("x", "*").replace("X", "*").replace("﹡", "*")
    s = s.replace("（", "(").replace("）", ")").replace("：", ":")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _lookup(unit_text: str) -> tuple[Unit, float]:
    """单位文本查表。先精确匹配，再忽略大小写匹配。"""
    if unit_text in _MEASURE_UNITS:
        return _MEASURE_UNITS[unit_text]
    lowered = unit_text.lower()
    for key, val in _MEASURE_UNITS.items():
        if key.lower() == lowered:
            return val
    return (Unit.UNKNOWN, 1.0)


def _build(qty: float, unit_text: str, count: int, raw: str) -> Spec:
    unit, factor = _lookup(unit_text)
    qty_base = qty * factor
    return Spec(
        raw=raw,
        qty=qty,
        unit_text=unit_text,
        unit=unit,
        qty_base=qty_base,
        count=max(count, 1),
        total_base=qty_base * max(count, 1),
    )


def _build_content(qty: int, content_unit: str, packs: int, raw: str) -> Spec:
    packs = max(packs, 1)
    return Spec(
        raw=raw,
        qty=float(qty),
        unit_text=content_unit,
        unit=Unit.COUNT,
        qty_base=float(qty),
        count=packs,
        total_base=float(qty * packs),
        content_unit=content_unit,
    )


def _parse_content(s: str) -> Spec | None:
    """解析内含量规格。只写「10卷」「5片装」时按每卷、每片计价。"""
    if m := _RE_CONTENT_MUL_PACK.search(s):
        return _build_content(int(m.group(1)), m.group(2), int(m.group(3)), m.group(0).strip())

    if m := _RE_PACK_MUL_CONTENT.search(s):
        return _build_content(int(m.group(2)), m.group(3), int(m.group(1)), m.group(0).strip())

    m = _RE_CONTENT.search(s)
    if not m or int(m.group(1)) <= 0:
        return None
    qty, content_unit = int(m.group(1)), m.group(2)
    packs, raw = 1, m.group(0).strip()
    rest = s[: m.start()] + " " + s[m.end() :]
    if m_pack := _RE_PACK.search(rest):
        n = int(m_pack.group(1))
        if 1 < n <= 200:
            packs = n
            raw = f"{raw}*{m_pack.group(0).strip()}"
    return _build_content(qty, content_unit, packs, raw)


def parse_spec(title: str, spec_text: str = "") -> Spec:
    """解析商品规格。

    Args:
        title: 商品标题
        spec_text: 平台标注的规格字段，有则优先

    Returns:
        Spec；解析失败时 Spec.parsed 为 False，调用方需降级为比标价。
    """
    for source in (spec_text, title):
        if not source:
            continue
        spec = _parse_one(source)
        if spec.parsed:
            return spec
    return Spec(raw=normalize_text(spec_text or title))


def _parse_one(text: str) -> Spec:
    s = normalize_text(text)
    s = _NOISE.sub(" ", s)
    if not s:
        return Spec()

    # 1) 净含量 × 件数：500ml*24瓶
    if m := _RE_MEASURE_MUL_COUNT.search(s):
        return _build(float(m.group(1)), m.group(2), int(m.group(3)), m.group(0).strip())

    # 2) 件数 × 净含量：24*500ml
    if m := _RE_COUNT_MUL_MEASURE.search(s):
        return _build(float(m.group(2)), m.group(3), int(m.group(1)), m.group(0).strip())

    # 3) 净含量 + 另行出现的件数：250ml 12盒装
    m_measure = _RE_MEASURE.search(s)
    if m_measure:
        qty, unit_text = float(m_measure.group(1)), m_measure.group(2)
        count = 1
        raw = m_measure.group(0).strip()
        # 只在净含量之外的片段里找件数，避免把 "500g" 的 500 当件数
        rest = s[: m_measure.start()] + " " + s[m_measure.end() :]
        if m_count := _RE_COUNT.search(rest):
            n = int(m_count.group(1))
            if 1 < n <= 200:  # 过滤 "1袋" 与异常大的数字
                count = n
                raw = f"{raw}*{m_count.group(0).strip()}"
        return _build(qty, unit_text, count, raw)

    # 4) 内含量：150抽*24包 → 3600 抽；10卷 → 按每卷计价
    if spec := _parse_content(s):
        return spec

    # 5) 只有件数，没有净含量：苹果 5个装 → 按件计价
    if m := _RE_COUNT.search(s):
        n = int(m.group(1))
        if 0 < n <= 500:
            return Spec(
                raw=m.group(0).strip(),
                qty=1.0,
                unit_text=m.group(2),
                unit=Unit.COUNT,
                qty_base=1.0,
                count=n,
                total_base=float(n),
            )

    return Spec(raw=s)
