"""采集层契约。

本项目不写爬虫。采集交给成熟 RPA 工具（影刀 / UiPath / Coze / 八爪鱼），
这里只定义「RPA 产出什么格式的数据」，以及把原始数据标准化成 Product。

只要 RPA 侧按此契约产出，换任何工具下游都不用改。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..models import Product
from ..normalize.spec_parser import parse_spec
from ..normalize.text import clean_title, extract_brand, extract_category

# 标准字段名 -> 可接受的别名。RPA 导出的表头往往不统一，这里做一次收敛。
FIELD_ALIASES: dict[str, list[str]] = {
    "sku_id": ["sku_id", "sku", "商品id", "商品ID", "商品编码", "货号", "item_id"],
    "title": ["title", "商品标题", "商品名称", "品名", "名称", "product_name"],
    "price": ["price", "售价", "价格", "现价", "成交价", "到手价", "sale_price"],
    "origin_price": ["origin_price", "原价", "划线价", "标价", "list_price"],
    "brand": ["brand", "品牌", "品牌名"],
    "category": ["category", "品类", "类目", "分类"],
    "barcode": ["barcode", "条码", "国际条码", "条形码", "ean", "upc"],
    "spec_text": ["spec", "spec_text", "规格", "包装规格", "净含量"],
    "city": ["city", "城市", "所在城市"],
    "store": ["store", "门店", "门店名称", "店铺"],
    "url": ["url", "链接", "商品链接", "商品url"],
    "image_url": ["image_url", "图片", "图片链接", "主图", "商品图片", "image"],
    "collected_at": ["collected_at", "采集时间", "抓取时间", "更新时间"],
}

# 反查表：别名（小写去空格）-> 标准字段名
_ALIAS_TO_FIELD = {
    alias.lower().replace(" ", ""): field
    for field, aliases in FIELD_ALIASES.items()
    for alias in aliases
}

REQUIRED_FIELDS = ("sku_id", "title")


def map_headers(headers: list[str]) -> dict[str, str]:
    """把 RPA 导出的表头映射到标准字段名。返回 {原表头: 标准字段名}。"""
    out = {}
    for h in headers:
        key = str(h).strip().lower().replace(" ", "")
        if key in _ALIAS_TO_FIELD:
            out[h] = _ALIAS_TO_FIELD[key]
    return out


# pandas 读 CSV 空单元格得到 float('nan')，str() 之后变成字符串 "nan"。
# 不拦住的话，所有「空条码」会彼此相等，被 L1 条码规则判成最高可信度的匹配。
_NULLISH = {"", "nan", "none", "null", "na", "n/a", "-", "—"}


def _to_str(value: Any) -> str:
    """把任意单元格值转成干净字符串，空值一律归一为空串。"""
    if value is None:
        return ""
    s = str(value).strip()
    return "" if s.lower() in _NULLISH else s


def _to_float(value: Any) -> float | None:
    """价格字段容错：'¥12.90'、'12.9元'、'' 都能正确处理。"""
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() in ("nan", "none", "-", "—"):
        return None
    s = s.replace("¥", "").replace("￥", "").replace("元", "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def to_product(
    record: dict[str, Any], platform: str, platform_name: str
) -> Product | None:
    """把一条原始记录标准化成 Product。缺必填字段返回 None。"""
    sku_id = _to_str(record.get("sku_id"))
    title = _to_str(record.get("title"))
    if not sku_id or not title:
        return None

    known = set(FIELD_ALIASES)
    product = Product(
        platform=platform,
        platform_name=platform_name,
        sku_id=sku_id,
        title=title,
        price=_to_float(record.get("price")),
        origin_price=_to_float(record.get("origin_price")),
        brand=_to_str(record.get("brand")),
        category=_to_str(record.get("category")),
        barcode=_to_str(record.get("barcode")),
        spec_text=_to_str(record.get("spec_text")),
        city=_to_str(record.get("city")),
        store=_to_str(record.get("store")),
        url=_to_str(record.get("url")),
        image_url=_to_str(record.get("image_url")),
        # 未识别的字段原样保留，方便排查和后续扩展
        extra={k: v for k, v in record.items() if k not in known},
    )
    if raw_time := _to_str(record.get("collected_at")):
        product.collected_at = raw_time

    enrich(product)
    return product


def enrich(product: Product) -> Product:
    """标准化：解析规格、归一化标题、补全品牌与品类。"""
    product.spec = parse_spec(product.title, product.spec_text)
    product.norm_title = clean_title(product.title)
    product.brand = extract_brand(product.title, product.brand)
    product.category = extract_category(product.title, product.category)
    return product


class Collector(ABC):
    """采集器接口。三种 RPA 对接方式各实现一个。"""

    @abstractmethod
    def collect(self, platform: str, platform_name: str) -> list[Product]:
        """拉取指定平台的商品数据。"""
        raise NotImplementedError
