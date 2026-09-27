"""SQLite 存储。

选 SQLite 是因为试点规模（每期几千 SKU × 几个平台）远用不到数据库服务，
一个文件随项目走，备份就是复制文件。

三张表：
    relations     匹配关系库（我方 SKU ↔ 竞品平台 ↔ 竞品 SKU）
    attempts      未匹配商品的上次结果，输入不变时不重复跑 L1-L3
    observations  每期采集到的商品快照，供改配校验与价格历史使用
"""

from __future__ import annotations

import csv
import hashlib
import sqlite3
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime
from pathlib import Path

from ..models import Product, RelationStatus

_SCHEMA = """
CREATE TABLE IF NOT EXISTS relations (
    self_sku        TEXT NOT NULL,
    platform        TEXT NOT NULL,
    rival_sku       TEXT NOT NULL,
    status          TEXT NOT NULL,
    source          TEXT NOT NULL,
    auto            INTEGER NOT NULL DEFAULT 0,
    score           REAL NOT NULL DEFAULT 0,
    confidence      REAL NOT NULL DEFAULT 0,
    reason          TEXT NOT NULL DEFAULT '',
    review_reason   TEXT NOT NULL DEFAULT '',
    change_note     TEXT NOT NULL DEFAULT '',
    confirmed_by    TEXT NOT NULL DEFAULT '',
    confirmed_at    TEXT NOT NULL DEFAULT '',
    self_title      TEXT NOT NULL DEFAULT '',
    self_spec       TEXT NOT NULL DEFAULT '',
    self_fp         TEXT NOT NULL DEFAULT '',
    rival_title     TEXT NOT NULL DEFAULT '',
    rival_spec      TEXT NOT NULL DEFAULT '',
    rival_fp        TEXT NOT NULL DEFAULT '',
    last_seen_period TEXT NOT NULL DEFAULT '',
    missing_count   INTEGER NOT NULL DEFAULT 0,
    missing_period  TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (self_sku, platform, rival_sku)
);

CREATE TABLE IF NOT EXISTS attempts (
    self_sku    TEXT NOT NULL,
    platform    TEXT NOT NULL,
    self_fp     TEXT NOT NULL,
    pool_fp     TEXT NOT NULL,
    score       REAL NOT NULL DEFAULT 0,
    reason      TEXT NOT NULL DEFAULT '',
    period      TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (self_sku, platform)
);

CREATE TABLE IF NOT EXISTS observations (
    period      TEXT NOT NULL,
    platform    TEXT NOT NULL,
    sku_id      TEXT NOT NULL,
    title       TEXT NOT NULL,
    spec        TEXT NOT NULL DEFAULT '',
    fp          TEXT NOT NULL DEFAULT '',
    price       REAL,
    unit_price  REAL,
    unit_label  TEXT NOT NULL DEFAULT '',
    city        TEXT NOT NULL DEFAULT '',
    store       TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (period, platform, sku_id)
);
"""


def current_period(today: date | None = None) -> str:
    """比价期次 = 运行日期所在的 ISO 周，如 2026-W40。同周重跑属于同一期。"""
    d = today or date.today()
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def fingerprint(p: Product) -> str:
    """商品指纹：清洗后的标题 + 解析后的规格。

    不用原始标题 —— 竞品加个「【限时】」就会触发重新复核，复核量永远降不下来。
    """
    if p.spec.parsed:
        unit, content_unit = p.spec.measure_key
        spec = f"{unit.value}{content_unit}:{round(p.spec.total_base, 3)}"
    else:
        spec = "未解析"
    return f"{p.norm_title}|{spec}"


def pool_fingerprint(products: list[Product]) -> str:
    """候选池指纹。竞品池有任何商品增减或变更，未匹配的商品就要重新匹配。"""
    digest = hashlib.sha1()
    for key in sorted(f"{p.sku_id}\t{fingerprint(p)}" for p in products):
        digest.update(key.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


@dataclass
class Relation:
    """一条匹配关系。字段与 relations 表一一对应。"""

    self_sku: str
    platform: str
    rival_sku: str
    status: RelationStatus
    source: str
    auto: bool = False
    score: float = 0.0
    confidence: float = 0.0
    reason: str = ""
    review_reason: str = ""
    change_note: str = ""
    confirmed_by: str = ""
    confirmed_at: str = ""
    self_title: str = ""
    self_spec: str = ""
    self_fp: str = ""
    rival_title: str = ""
    rival_spec: str = ""
    rival_fp: str = ""
    last_seen_period: str = ""
    missing_count: int = 0
    missing_period: str = ""
    created_at: str = ""
    updated_at: str = ""

    def snapshot(self, self_product: Product, rival: Product) -> None:
        """记录双方当前的标题、规格与指纹，作为后续变更检测的基线。"""
        self.self_title = self_product.title
        self.self_spec = self_product.spec.display()
        self.self_fp = fingerprint(self_product)
        self.rival_title = rival.title
        self.rival_spec = rival.spec.display()
        self.rival_fp = fingerprint(rival)


_RELATION_COLUMNS = [f.name for f in fields(Relation)]


@dataclass
class Attempt:
    self_sku: str
    platform: str
    self_fp: str
    pool_fp: str
    score: float
    reason: str
    period: str


class Store:
    """关系库。用 with 语句打开，退出时提交并关闭。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, exc_type, *_) -> None:
        if exc_type is None:
            self.conn.commit()
        self.close()

    def close(self) -> None:
        self.conn.close()

    def commit(self) -> None:
        self.conn.commit()

    # ------------------------------------------------------------------
    # relations
    # ------------------------------------------------------------------

    @staticmethod
    def _to_relation(row: sqlite3.Row) -> Relation:
        data = dict(row)
        data["status"] = RelationStatus(data["status"])
        data["auto"] = bool(data["auto"])
        return Relation(**data)

    def relations(
        self,
        platform: str | None = None,
        self_sku: str | None = None,
        status: RelationStatus | None = None,
    ) -> list[Relation]:
        sql, args = "SELECT * FROM relations WHERE 1=1", []
        for col, val in (("platform", platform), ("self_sku", self_sku)):
            if val is not None:
                sql += f" AND {col} = ?"
                args.append(val)
        if status is not None:
            sql += " AND status = ?"
            args.append(status.value)
        sql += " ORDER BY platform, self_sku, rival_sku"
        return [self._to_relation(r) for r in self.conn.execute(sql, args)]

    def relations_by_self(self, platform: str) -> dict[str, list[Relation]]:
        """按我方 SKU 分组，匹配前一次性载入一个平台的全部关系。"""
        out: dict[str, list[Relation]] = {}
        for rel in self.relations(platform=platform):
            out.setdefault(rel.self_sku, []).append(rel)
        return out

    def get_relation(self, self_sku: str, platform: str, rival_sku: str) -> Relation | None:
        row = self.conn.execute(
            "SELECT * FROM relations WHERE self_sku = ? AND platform = ? AND rival_sku = ?",
            (self_sku, platform, rival_sku),
        ).fetchone()
        return self._to_relation(row) if row else None

    def save_relation(self, rel: Relation) -> None:
        now = _now()
        rel.created_at = rel.created_at or now
        rel.updated_at = now
        data = asdict(rel)
        data["status"] = rel.status.value
        data["auto"] = int(rel.auto)
        cols = ", ".join(_RELATION_COLUMNS)
        marks = ", ".join("?" for _ in _RELATION_COLUMNS)
        self.conn.execute(
            f"INSERT OR REPLACE INTO relations ({cols}) VALUES ({marks})",
            [data[c] for c in _RELATION_COLUMNS],
        )

    def delete_relation(self, rel: Relation) -> None:
        self.conn.execute(
            "DELETE FROM relations WHERE self_sku = ? AND platform = ? AND rival_sku = ?",
            (rel.self_sku, rel.platform, rel.rival_sku),
        )

    def export_relations(self, path: str | Path, status: RelationStatus | None = None) -> int:
        """导出关系库为 CSV，供排查。不依赖比价运行。"""
        rows = self.relations(status=status)
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(_RELATION_COLUMNS)
            for rel in rows:
                data = asdict(rel)
                data["status"] = rel.status.value
                writer.writerow([data[c] for c in _RELATION_COLUMNS])
        return len(rows)

    # ------------------------------------------------------------------
    # attempts
    # ------------------------------------------------------------------

    def get_attempt(self, self_sku: str, platform: str) -> Attempt | None:
        row = self.conn.execute(
            "SELECT self_sku, platform, self_fp, pool_fp, score, reason, period "
            "FROM attempts WHERE self_sku = ? AND platform = ?",
            (self_sku, platform),
        ).fetchone()
        return Attempt(**dict(row)) if row else None

    def save_attempt(self, attempt: Attempt) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO attempts "
            "(self_sku, platform, self_fp, pool_fp, score, reason, period, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                attempt.self_sku, attempt.platform, attempt.self_fp, attempt.pool_fp,
                attempt.score, attempt.reason, attempt.period, _now(),
            ),
        )

    def delete_attempt(self, self_sku: str, platform: str) -> None:
        self.conn.execute(
            "DELETE FROM attempts WHERE self_sku = ? AND platform = ?", (self_sku, platform)
        )

    # ------------------------------------------------------------------
    # observations
    # ------------------------------------------------------------------

    def record_observations(self, period: str, platform: str, products: list[Product]) -> None:
        """保存本期某平台的采集快照。同周重跑覆盖本期数据。"""
        self.conn.execute(
            "DELETE FROM observations WHERE period = ? AND platform = ?", (period, platform)
        )
        self.conn.executemany(
            "INSERT OR REPLACE INTO observations "
            "(period, platform, sku_id, title, spec, fp, price, unit_price, unit_label, city, store) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    period, platform, p.sku_id, p.title, p.spec.display(), fingerprint(p),
                    p.price, p.unit_price, p.unit_price_label, p.city, p.store,
                )
                for p in products
            ],
        )

    def latest_period(self) -> str:
        row = self.conn.execute("SELECT MAX(period) FROM observations").fetchone()
        return row[0] or ""

    def observation(self, period: str, platform: str, sku_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM observations WHERE period = ? AND platform = ? AND sku_id = ?",
            (period, platform, sku_id),
        ).fetchone()
