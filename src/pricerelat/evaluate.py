"""匹配评测。

用一份人工标注的商品对，跑当前配置下的匹配流程，输出总体与分层的准确率、
召回率，以及错配明细。阈值、规则、模型的每次调整都先过一遍评测，
避免改一处坏一处。

评测集格式（CSV）：
    我方SKU, 我方标题, 我方品牌, 我方规格, 竞品平台, 竞品SKU, 竞品标题, 竞品品牌, 竞品规格, 是否同款, 备注
只含标题、品牌、规格与标注结果，不含价格与门店。

口径：
    - 每个平台单独评测。我方候选池 = 该平台标注中出现的我方商品，竞品池同理。
    - 一个我方商品在一个平台至多一个同款（与关系库一对一一致）；没有「是」标注的，
      正确答案为「无同款」。
    - 准确率 = 预测正确的匹配 / 全部预测出的匹配（按层级分别统计）
    - 召回率 = 预测正确的匹配 / 有同款的我方商品数；各层召回率相加等于总体召回率
    - 自动确认 = 系统直接写成「已确认」、不经人工的匹配，它的准确率决定错配风险
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .ingest.base import enrich
from .matching import llm
from .matching.pipeline import classify, match_platform
from .models import MatchPair, Product, RelationStatus

REQUIRED_COLUMNS = (
    "我方SKU", "我方标题", "竞品平台", "竞品SKU", "竞品标题", "是否同款",
)
_YES = {"是", "y", "yes", "1", "true", "同款"}
_NO = {"否", "n", "no", "0", "false", "不同款"}


class EvalSetError(ValueError):
    pass


@dataclass
class LabeledSet:
    """按平台组织的评测数据。"""

    self_products: dict[str, dict[str, Product]] = field(default_factory=dict)   # 平台 → sku → 商品
    rival_products: dict[str, dict[str, Product]] = field(default_factory=dict)
    positives: dict[str, dict[str, str]] = field(default_factory=dict)          # 平台 → 我方 sku → 同款竞品 sku
    pair_count: int = 0


def _product(platform: str, sku: str, title: str, brand: str, spec: str) -> Product:
    return enrich(Product(platform=platform, platform_name=platform, sku_id=sku, title=title,
                          brand=brand, spec_text=spec))


def load_set(path: str | Path) -> LabeledSet:
    path = Path(path)
    if not path.exists():
        raise EvalSetError(f"评测集不存在：{path}")
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise EvalSetError(f"评测集缺少列：{'、'.join(missing)}")
        rows = list(reader)

    data = LabeledSet()
    for lineno, r in enumerate(rows, start=2):
        platform = r["竞品平台"].strip()
        self_sku, rival_sku = r["我方SKU"].strip(), r["竞品SKU"].strip()
        label = r["是否同款"].strip().lower()
        if label not in _YES | _NO:
            raise EvalSetError(f"第 {lineno} 行「是否同款」只能填 是/否，实际为 {r['是否同款']!r}")

        data.self_products.setdefault(platform, {}).setdefault(
            self_sku, _product("self", self_sku, r["我方标题"], r.get("我方品牌", ""), r.get("我方规格", ""))
        )
        data.rival_products.setdefault(platform, {}).setdefault(
            rival_sku, _product(platform, rival_sku, r["竞品标题"], r.get("竞品品牌", ""), r.get("竞品规格", ""))
        )
        data.pair_count += 1

        if label in _YES:
            known = data.positives.setdefault(platform, {})
            if known.get(self_sku, rival_sku) != rival_sku:
                raise EvalSetError(
                    f"第 {lineno} 行：{self_sku} 在 {platform} 已标注同款 {known[self_sku]}，"
                    f"一个平台只能有一个同款"
                )
            known[self_sku] = rival_sku
    return data


@dataclass
class LayerStat:
    predicted: int = 0
    correct: int = 0

    def precision(self) -> float | None:
        return self.correct / self.predicted if self.predicted else None


@dataclass
class EvalResult:
    pair_count: int = 0
    positives: int = 0
    predicted: int = 0
    correct: int = 0
    by_layer: dict[str, LayerStat] = field(default_factory=dict)
    auto_confirmed: LayerStat = field(default_factory=LayerStat)
    pending: LayerStat = field(default_factory=LayerStat)
    errors: list[dict] = field(default_factory=list)
    l3_enabled: bool = False

    def precision(self) -> float | None:
        return self.correct / self.predicted if self.predicted else None

    def recall(self) -> float | None:
        return self.correct / self.positives if self.positives else None

    def layer_recall(self, layer: str) -> float | None:
        return self.by_layer[layer].correct / self.positives if self.positives else None

    def to_dict(self) -> dict:
        def pct(x):
            return None if x is None else round(x, 4)

        return {
            "标注商品对": self.pair_count,
            "有同款的我方商品": self.positives,
            "L3": "已启用" if self.l3_enabled else "未启用（灰区候选按 L2 结果计入待复核）",
            "总体": {"预测": self.predicted, "正确": self.correct,
                     "准确率": pct(self.precision()), "召回率": pct(self.recall())},
            "分层": {
                layer: {"预测": s.predicted, "正确": s.correct,
                        "准确率": pct(s.precision()), "召回率": pct(self.layer_recall(layer))}
                for layer, s in self.by_layer.items()
            },
            "自动确认": {**asdict(self.auto_confirmed), "准确率": pct(self.auto_confirmed.precision())},
            "待复核": {**asdict(self.pending), "准确率": pct(self.pending.precision())},
            "错误数": len(self.errors),
        }


def evaluate(data: LabeledSet, cfg: dict) -> EvalResult:
    result = EvalResult(pair_count=data.pair_count)
    result.l3_enabled = bool(cfg.get("matching", {}).get("llm", {}).get("enabled", True)) and llm.is_available()
    by_layer: dict[str, LayerStat] = defaultdict(LayerStat)

    for platform, selfs in data.self_products.items():
        rivals = list(data.rival_products[platform].values())
        truth = data.positives.get(platform, {})
        result.positives += len(truth)
        pairs, _ = match_platform(list(selfs.values()), rivals, cfg)

        for self_sku, product in selfs.items():
            pair = pairs[product.uid]
            expected = truth.get(self_sku, "")
            got = pair.rival_product.sku_id if pair.matched else ""

            if pair.matched:
                ok = got == expected
                result.predicted += 1
                result.correct += ok
                layer = by_layer[pair.level.value]
                layer.predicted += 1
                layer.correct += ok
                status, _ = classify(pair)
                bucket = result.auto_confirmed if status is RelationStatus.CONFIRMED else result.pending
                bucket.predicted += 1
                bucket.correct += ok
                if not ok:
                    result.errors.append(_error("错配", platform, pair, expected, data, status))
            elif expected:
                result.errors.append(_error("漏配", platform, pair, expected, data, None))

    result.by_layer = dict(sorted(by_layer.items()))
    return result


ERROR_FIELDS = [
    "类型", "平台", "我方SKU", "我方标题", "预测竞品SKU", "预测竞品标题",
    "应为竞品SKU", "应为竞品标题", "匹配层级", "写入状态", "相似度", "判定依据",
]


def _error(kind: str, platform: str, pair: MatchPair, expected: str, data: LabeledSet,
           status: RelationStatus | None) -> dict:
    rivals = data.rival_products[platform]
    got = pair.rival_product
    return {
        "类型": kind,
        "平台": platform,
        "我方SKU": pair.self_product.sku_id,
        "我方标题": pair.self_product.title,
        "预测竞品SKU": got.sku_id if got else "",
        "预测竞品标题": got.title if got else "",
        "应为竞品SKU": expected,
        "应为竞品标题": rivals[expected].title if expected else "（无同款）",
        "匹配层级": pair.level.value,
        "写入状态": status.value if status else "",
        "相似度": round(pair.score, 1),
        "判定依据": pair.reason,
    }


def write_outputs(result: EvalResult, out_dir: str | Path) -> tuple[Path, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_path = out_dir / "评测结果.json"
    summary_path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    errors_path = out_dir / "评测错配明细.csv"
    with errors_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=ERROR_FIELDS)
        writer.writeheader()
        writer.writerows(result.errors)
    return summary_path, errors_path


def format_report(result: EvalResult) -> str:
    def pct(x):
        return "—" if x is None else f"{x * 100:.1f}%"

    lines = [
        f"标注商品对 {result.pair_count} 组，有同款的我方商品 {result.positives} 个",
        f"L3：{'已启用' if result.l3_enabled else '未启用（未配置密钥或已关闭），灰区候选按 L2 结果计入待复核'}",
        "",
        f"{'层级':<12}{'预测':>6}{'正确':>6}{'准确率':>10}{'召回率':>10}",
    ]
    for layer, s in result.by_layer.items():
        lines.append(f"{layer:<12}{s.predicted:>6}{s.correct:>6}{pct(s.precision()):>10}{pct(result.layer_recall(layer)):>10}")
    lines.append(f"{'总体':<12}{result.predicted:>6}{result.correct:>6}{pct(result.precision()):>10}{pct(result.recall()):>10}")
    lines += [
        "",
        f"自动确认：{result.auto_confirmed.predicted} 个，准确率 {pct(result.auto_confirmed.precision())}",
        f"待复核：  {result.pending.predicted} 个，准确率 {pct(result.pending.precision())}",
    ]
    if result.errors:
        lines += ["", f"错误 {len(result.errors)} 个："]
        for e in result.errors:
            lines.append(
                f"  [{e['类型']}] {e['平台']} {e['我方标题']} → "
                f"{e['预测竞品标题'] or '（未匹配）'}，应为 {e['应为竞品标题']}（{e['匹配层级']}）"
            )
    return "\n".join(lines)
