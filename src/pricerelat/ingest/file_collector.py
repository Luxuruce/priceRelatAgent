"""对接方式 A：文件落盘。

RPA 机器人跑完把结果导出成 CSV / Excel 丢到 inbox 目录，程序扫描导入。
最稳的一种 —— 不依赖网络连通性，RPA 挂了也不会丢数据，出问题能直接翻原始文件。

文件命名约定：<平台key>_<任意后缀>.csv，如 sams_20260916.csv、self_商品池.xlsx
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime
from pathlib import Path

import pandas as pd

from ..models import Product
from .base import Collector, map_headers, to_product

logger = logging.getLogger(__name__)


class FileCollector(Collector):
    def __init__(
        self,
        inbox_dir: str | Path,
        archive_dir: str | Path | None = None,
        encoding: str = "utf-8-sig",
    ):
        self.inbox = Path(inbox_dir)
        self.archive = Path(archive_dir) if archive_dir else None
        self.encoding = encoding

    def _find_files(self, platform: str) -> list[Path]:
        """按 <平台key>_*.csv/xlsx 约定查找文件。"""
        if not self.inbox.exists():
            return []
        files = [
            f
            for ext in ("csv", "xlsx", "xls", "json")
            for f in self.inbox.glob(f"{platform}_*.{ext}")
        ]
        return sorted(files)

    def _read(self, path: Path) -> pd.DataFrame:
        suffix = path.suffix.lower()
        if suffix == ".csv":
            return pd.read_csv(path, encoding=self.encoding, dtype=str)
        if suffix == ".json":
            # HTTP 回调落盘的格式：一个对象数组
            records = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(records, dict):
                records = records.get("items", [])
            return pd.DataFrame(records, dtype=str)
        return pd.read_excel(path, dtype=str)

    def collect(self, platform: str, platform_name: str) -> list[Product]:
        files = self._find_files(platform)
        if not files:
            logger.warning("[%s] 在 %s 下未找到数据文件", platform_name, self.inbox)
            return []

        products: list[Product] = []
        seen: set[str] = set()

        for path in files:
            try:
                df = self._read(path)
            except Exception as e:
                logger.error("[%s] 读取 %s 失败：%s", platform_name, path.name, e)
                continue

            mapping = map_headers(list(df.columns))
            missing = {"sku_id", "title"} - set(mapping.values())
            if missing:
                logger.error(
                    "[%s] %s 缺少必要列 %s（当前列：%s）",
                    platform_name, path.name, missing, list(df.columns),
                )
                continue

            df = df.rename(columns=mapping)
            n_before = len(products)
            for record in df.to_dict(orient="records"):
                product = to_product(record, platform, platform_name)
                if product is None:
                    continue
                # 同一平台内 SKU 去重，多城市/多门店取先出现的那条
                if product.sku_id in seen:
                    continue
                seen.add(product.sku_id)
                products.append(product)

            logger.info(
                "[%s] %s → %d 条", platform_name, path.name, len(products) - n_before
            )
            self._archive(path)

        return products

    def _archive(self, path: Path) -> None:
        """导入后归档原始文件，保留追溯能力。"""
        if not self.archive:
            return
        self.archive.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = self.archive / f"{path.stem}__{stamp}{path.suffix}"
        try:
            shutil.move(str(path), str(target))
        except OSError as e:
            logger.warning("归档 %s 失败：%s", path.name, e)
