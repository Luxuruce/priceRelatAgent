"""核心数据模型。

整条链路的数据形态只有三种：
    Product   —— 一个商品（我方或竞品），采集层产出，标准化后带规格与单位价
    MatchPair —— 一组匹配关系（我方商品 ↔ 竞品商品），匹配层产出
    CompareRow —— 一行比价结果，比价层产出，报告层消费
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Any


class Unit(str, Enum):
    """折算后的基准单位。"""

    WEIGHT = "weight"   # 归一到克
    VOLUME = "volume"   # 归一到毫升
    COUNT = "count"     # 归一到件（或内含量单位，见 Spec.content_unit）
    UNKNOWN = "unknown"


# 数量通常上百的内含量单位，单位价按每 100 折算（元/100抽），其余按每 1（元/卷）
PER_100_CONTENT_UNITS = ("抽", "张")


def same_measure(a: "Spec", b: "Spec") -> bool:
    """两个规格是否可按单位价比较：都已解析，且可比口径一致。"""
    return a.parsed and b.parsed and a.measure_key == b.measure_key


def measure_mismatch_note(a: "Spec", b: "Spec") -> str:
    """口径不一致的原因描述，供报告备注和匹配依据使用。"""
    if a.unit is not b.unit:
        return f"单位类别不同（{a.unit.value} vs {b.unit.value}）"
    return f"计数单位不同（{a.count_label} vs {b.count_label}）"


class MatchLevel(str, Enum):
    """匹配由哪一层产出，决定了可信度和是否需要人工复核。"""

    BARCODE = "L1-条码"
    BRAND_SPEC = "L1-品牌规格"
    FUZZY = "L2-模糊"
    LLM = "L3-AI判定"
    MANUAL = "人工确认"
    NONE = "未匹配"


class Action(str, Enum):
    """比价结论给出的建议动作。"""

    CUT_PRICE_URGENT = "建议降价(高优)"
    CUT_PRICE = "建议降价"
    WATCH = "持平/关注"
    RAISE_PRICE = "有提价空间"
    NO_DATA = "数据不足"


@dataclass
class Spec:
    """从商品标题解析出的规格。

    净含量统一折算到基准单位：重量→克，体积→毫升，计数→件。
    total_base = qty_base * count，即整个包装的总量。

    计数类再细分内含量单位（content_unit）：纸品的「150抽×24包」总量是 3600 抽，
    而不是 24 件。抽数和张数、和普通件数之间不能互相折算，比价前必须先判断
    measure_key 是否一致。
    """

    raw: str = ""                    # 原始规格文本，如 "500ml*6瓶"
    qty: float | None = None         # 单件净含量数值，如 500
    unit_text: str = ""              # 原始单位文本，如 "ml"
    unit: Unit = Unit.UNKNOWN        # 归一化单位类别
    qty_base: float | None = None    # 单件净含量折算到基准单位，如 500.0 (ml)
    count: int = 1                   # 件数/包数，如 6
    total_base: float | None = None  # 整包总量 = qty_base * count
    content_unit: str = ""           # 计数类的内含量单位：抽/张/片/粒/卷；普通件数为空

    @property
    def parsed(self) -> bool:
        return self.total_base is not None and self.total_base > 0

    @property
    def measure_key(self) -> tuple[Unit, str]:
        """可比口径。两个规格只有 measure_key 相同，单位价才有可比性。"""
        return (self.unit, self.content_unit if self.unit is Unit.COUNT else "")

    @property
    def count_label(self) -> str:
        """计数类的单位名，普通件数显示为「件」。"""
        return self.content_unit or "件"

    @property
    def price_base(self) -> float:
        """单位价的折算基数：重量/体积每 100g、100ml；抽/张每 100；其余每 1。"""
        if self.unit is Unit.COUNT:
            return 100.0 if self.content_unit in PER_100_CONTENT_UNITS else 1.0
        return 100.0

    def display(self) -> str:
        """人类可读的规格描述，一律用折算后的基准单位，避免 1000mg 显示成 1000g。"""
        if not self.parsed:
            return self.raw or "未解析"
        if self.unit is Unit.COUNT:
            label = self.count_label
            if self.content_unit and self.count > 1:
                return f"{self.qty_base:g}{label}×{self.count} (合计{self.total_base:g}{label})"
            return f"{self.total_base:g}{label}"
        suffix = {Unit.WEIGHT: "g", Unit.VOLUME: "ml"}[self.unit]
        if self.count > 1:
            return f"{self.qty_base:g}{suffix}×{self.count} (合计{self.total_base:g}{suffix})"
        return f"{self.total_base:g}{suffix}"


@dataclass
class Product:
    """一个商品。采集层的标准产出，也是匹配与比价的输入。"""

    # --- 采集层必须提供 ---
    platform: str                    # 平台 key，如 "sams" / "self"
    platform_name: str               # 平台显示名，如 "山姆" / "我方"
    sku_id: str                      # 平台内商品唯一标识
    title: str                       # 商品标题
    price: float | None = None       # 售价（元）

    # --- 采集层可选提供 ---
    brand: str = ""
    category: str = ""
    barcode: str = ""                # 条码/国际码，有则匹配准确率最高
    spec_text: str = ""              # 平台标注的规格字段（若有，优先于标题解析）
    origin_price: float | None = None  # 划线价/原价
    city: str = ""
    store: str = ""
    url: str = ""
    image_url: str = ""
    collected_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    extra: dict[str, Any] = field(default_factory=dict)

    # --- 标准化层填充 ---
    norm_title: str = ""             # 归一化标题，供模糊匹配使用
    spec: Spec = field(default_factory=Spec)

    @property
    def unit_price(self) -> float | None:
        """折算后的可比单位价。

        重量类 → 元/100g，体积类 → 元/100ml，计数类 → 元/件、元/100抽、元/卷 等。
        规格未解析出来时返回 None，调用方需降级为比标价。
        """
        if self.price is None or not self.spec.parsed:
            return None
        return self.price / self.spec.total_base * self.spec.price_base

    @property
    def unit_price_label(self) -> str:
        spec = self.spec
        if spec.unit is Unit.WEIGHT:
            return "元/100g"
        if spec.unit is Unit.VOLUME:
            return "元/100ml"
        if spec.unit is Unit.COUNT:
            base = "100" if spec.price_base == 100 else ""
            return f"元/{base}{spec.count_label}"
        return "—"

    @property
    def uid(self) -> str:
        return f"{self.platform}:{self.sku_id}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["spec"] = {**asdict(self.spec), "unit": self.spec.unit.value}
        d["unit_price"] = self.unit_price
        return d


@dataclass
class MatchPair:
    """一组匹配关系。"""

    self_product: Product
    rival_product: Product | None
    score: float = 0.0               # 0~100 相似度
    level: MatchLevel = MatchLevel.NONE
    confidence: float = 0.0          # 0~1，L3 给出；L1 固定 1.0
    reason: str = ""                 # 匹配依据，人工复核时看这个
    need_review: bool = False

    @property
    def matched(self) -> bool:
        return self.rival_product is not None


@dataclass
class CompareRow:
    """一行比价结果。报告与导出的最终形态。"""

    self_product: Product
    # 平台 key -> 该平台匹配到的竞品（未匹配则为 None）
    rivals: dict[str, MatchPair] = field(default_factory=dict)

    benchmark_platform: str = ""     # 跟价基准平台
    benchmark_price: float | None = None       # 基准标价
    benchmark_unit_price: float | None = None  # 基准单位价

    diff: float | None = None        # 单位价差额（我方 - 基准），正=我方贵
    diff_rate: float | None = None   # 价差率
    action: Action = Action.NO_DATA
    comparable: bool = False         # 是否基于单位价可比（否则仅比标价）
    notes: list[str] = field(default_factory=list)
