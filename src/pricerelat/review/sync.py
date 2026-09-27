"""复核表同步。

    push      比价结束后，把关系库中的待复核关系写入复核表
    writeback 比价开始前（或单独执行），把人工结论回写到关系库

规则：
    - 同一关系在表中还有未回写完成的行时，不重复写入
    - 程序不覆盖人工填写的字段；已有人工结论的行不删、不改
    - 改配的竞品 SKU 必须在最近一期采集结果中存在，否则该行回写失败
    - 同一关系被多行处理时，以最后修改时间最新的一行为准
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..models import MatchLevel, RelationStatus
from ..store.db import Relation, Store
from .bitable import ReviewTable, Row

logger = logging.getLogger(__name__)

SELF_PLATFORM = "self"

# 复核表字段名
F_KEY = "关系键"
F_CONCLUSION = "人工结论"
F_REASSIGN = "改配竞品SKU"
F_STATUS = "回写状态"
F_FAIL = "回写失败原因"
F_BY = "处理人"
F_AT = "处理时间"
F_MODIFIED_BY = "最后修改人"
F_MODIFIED_AT = "最后修改时间"

CONFIRM, REJECT, REASSIGN = "确认", "否决", "改配"
NOT_WRITTEN, WRITTEN, FAILED = "未回写", "已回写", "回写失败"


def relation_key(self_sku: str, platform: str, rival_sku: str) -> str:
    return f"{self_sku}|{platform}|{rival_sku}"


def _split_key(key: str) -> tuple[str, str, str]:
    parts = key.split("|")
    if len(parts) != 3 or not all(parts):
        raise ValueError(f"关系键格式不正确：{key!r}")
    return parts[0], parts[1], parts[2]


# ------------------------------------------------------------------
# push
# ------------------------------------------------------------------

def review_record(rel: Relation, period: str) -> dict:
    """待复核关系 → 复核表的一行（只含程序写入的字段）。"""
    return {
        "我方商品": rel.self_title,
        "比价期次": period,
        "我方SKU": rel.self_sku,
        "我方规格": rel.self_spec,
        "竞品平台": rel.platform,
        "竞品SKU": rel.rival_sku,
        "竞品商品": rel.rival_title,
        "竞品规格": rel.rival_spec,
        "匹配层级": rel.source,
        "相似度": round(rel.score, 1),
        "置信度": round(rel.confidence, 2),
        "判定依据": rel.reason,
        "进入复核原因": rel.review_reason or None,
        "变更说明": rel.change_note,
        F_STATUS: NOT_WRITTEN,
        F_KEY: relation_key(rel.self_sku, rel.platform, rel.rival_sku),
    }


def push(store: Store, table: ReviewTable, period: str) -> int:
    """把待复核关系追加写入复核表，返回新写入的行数。"""
    open_keys = {
        row.text(F_KEY) for row in table.list_rows() if row.text(F_STATUS) != WRITTEN
    }
    records = [
        review_record(rel, period)
        for rel in store.relations(status=RelationStatus.PENDING)
        if relation_key(rel.self_sku, rel.platform, rel.rival_sku) not in open_keys
    ]
    if records:
        table.create_rows(records)
    return len(records)


# ------------------------------------------------------------------
# writeback
# ------------------------------------------------------------------

@dataclass
class Decision:
    """一条人工结论，来源可以是多维表格的一行或本地 CSV 的一行。"""

    key: str
    conclusion: str
    reassign_sku: str = ""
    by: str = ""
    at: str = ""
    row_id: str = ""


@dataclass
class WritebackResult:
    applied: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)   # (关系键, 原因)
    superseded: int = 0

    def summary(self) -> str:
        parts = [f"回写 {self.applied} 条"]
        if self.failed:
            parts.append(f"失败 {len(self.failed)} 条")
        if self.superseded:
            parts.append(f"被较新处理覆盖 {self.superseded} 条")
        return "，".join(parts)


class WritebackError(ValueError):
    pass


def _snapshot_from_observations(store: Store, rel: Relation, period: str) -> None:
    """用最近一期采集快照刷新关系的确认基线，避免确认后下期又被判为信息变更。"""
    me = store.observation(period, SELF_PLATFORM, rel.self_sku)
    rival = store.observation(period, rel.platform, rel.rival_sku)
    if me is not None:
        rel.self_title, rel.self_spec, rel.self_fp = me["title"], me["spec"], me["fp"]
    if rival is not None:
        rel.rival_title, rel.rival_spec, rel.rival_fp = rival["title"], rival["spec"], rival["fp"]


def _confirm(store: Store, rel: Relation, d: Decision, period: str, source: str | None = None) -> None:
    rel.status = RelationStatus.CONFIRMED
    if source:
        rel.source = source
    rel.auto = False
    rel.confirmed_by, rel.confirmed_at = d.by, d.at
    rel.review_reason, rel.change_note = "", ""
    rel.missing_count = 0
    _snapshot_from_observations(store, rel, period)
    store.save_relation(rel)
    store.delete_attempt(rel.self_sku, rel.platform)

    # 一对一：同一「我方 SKU × 平台」只保留一条已确认关系
    for other in store.relations(platform=rel.platform, self_sku=rel.self_sku,
                                 status=RelationStatus.CONFIRMED):
        if other.rival_sku != rel.rival_sku:
            other.status = RelationStatus.INVALID
            other.change_note = f"已被人工确认的 {rel.rival_sku} 取代"
            store.save_relation(other)


def _reject(store: Store, rel: Relation, d: Decision) -> None:
    rel.status = RelationStatus.REJECTED
    rel.auto = False
    rel.confirmed_by, rel.confirmed_at = d.by, d.at
    store.save_relation(rel)
    store.delete_attempt(rel.self_sku, rel.platform)


def apply_decision(store: Store, d: Decision, period: str) -> None:
    """把一条人工结论写入关系库。不满足条件时抛出 WritebackError，关系库不变。"""
    try:
        self_sku, platform, rival_sku = _split_key(d.key)
    except ValueError as e:
        raise WritebackError(str(e)) from e

    rel = store.get_relation(self_sku, platform, rival_sku) or Relation(
        self_sku=self_sku, platform=platform, rival_sku=rival_sku,
        status=RelationStatus.PENDING, source=MatchLevel.MANUAL.value,
    )

    if d.conclusion == CONFIRM:
        if store.observation(period, platform, rival_sku) is None and not rel.rival_title:
            raise WritebackError(f"竞品 SKU {rival_sku} 不存在")
        _confirm(store, rel, d, period)
    elif d.conclusion == REJECT:
        _reject(store, rel, d)
    elif d.conclusion == REASSIGN:
        target = d.reassign_sku.strip()
        if not target:
            raise WritebackError("人工结论为改配，但未填写改配竞品SKU")
        if store.observation(period, platform, target) is None:
            raise WritebackError("改配 SKU 不存在")
        if target != rival_sku:
            _reject(store, rel, d)
        new_rel = store.get_relation(self_sku, platform, target) or Relation(
            self_sku=self_sku, platform=platform, rival_sku=target,
            status=RelationStatus.PENDING, source=MatchLevel.MANUAL.value,
            reason="人工改配",
        )
        _confirm(store, new_rel, d, period, source=MatchLevel.MANUAL.value)
    else:
        raise WritebackError(f"无法识别的人工结论：{d.conclusion!r}")


def _latest_per_key(decisions: list[Decision]) -> tuple[list[Decision], list[Decision]]:
    """同一关系多行处理时，以处理时间最新的一行为准。Returns: (生效, 被覆盖)"""
    latest: dict[str, Decision] = {}
    for d in decisions:
        if d.key not in latest or d.at > latest[d.key].at:
            latest[d.key] = d
    kept = list(latest.values())
    superseded = [d for d in decisions if latest[d.key] is not d]
    return kept, superseded


def _apply_all(store: Store, decisions: list[Decision], period: str) -> tuple[WritebackResult, dict[str, str], list[Decision]]:
    """Returns: (结果, {row_id: 失败原因}, 被覆盖的结论)"""
    result = WritebackResult()
    failures: dict[str, str] = {}
    kept, superseded = _latest_per_key(decisions)
    for d in kept:
        try:
            apply_decision(store, d, period)
            result.applied += 1
        except WritebackError as e:
            result.failed.append((d.key, str(e)))
            failures[d.row_id] = str(e)
    result.superseded = len(superseded)
    store.commit()
    return result, failures, superseded


def _format_time(value: str) -> str:
    """多维表格时间 2026-09-27T22:08:41.000+08:00 → 2026-09-27 22:08:41（写回日期字段的格式）。"""
    if not value:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return value.replace("T", " ")[:19]


def writeback(store: Store, table: ReviewTable, period: str | None = None) -> WritebackResult:
    """读取复核表中已填人工结论、尚未回写（或回写失败）的行，回写到关系库。"""
    period = period or store.latest_period()
    rows: list[Row] = [
        r for r in table.list_rows()
        if r.text(F_CONCLUSION) and r.text(F_STATUS) in ("", NOT_WRITTEN, FAILED)
    ]
    decisions = [
        Decision(
            key=r.text(F_KEY),
            conclusion=r.text(F_CONCLUSION),
            reassign_sku=r.text(F_REASSIGN),
            # 取人工填写结论时的修改人与时间；程序随后更新回写状态会刷新这两个系统字段
            by=r.text(F_MODIFIED_BY),
            at=_format_time(r.text(F_MODIFIED_AT)),
            row_id=r.record_id,
        )
        for r in rows
    ]
    result, failures, superseded = _apply_all(store, decisions, period)

    updates: dict[str, dict] = {}
    superseded_ids = {d.row_id for d in superseded}
    for d in decisions:
        if d.row_id in failures:
            updates[d.row_id] = {F_STATUS: FAILED, F_FAIL: failures[d.row_id]}
        elif d.row_id in superseded_ids:
            updates[d.row_id] = {F_STATUS: WRITTEN, F_FAIL: "同一关系有更新的处理，以最新一行为准",
                                 F_BY: d.by, F_AT: d.at}
        else:
            updates[d.row_id] = {F_STATUS: WRITTEN, F_FAIL: "", F_BY: d.by, F_AT: d.at}
    if updates:
        table.update_rows(updates)
    return result


def writeback_csv(store: Store, path: str | Path, period: str | None = None) -> WritebackResult:
    """从本地待复核 CSV 回写人工结论。多维表格不可用时的兜底通路。

    「人工确认」列填 确认 / 否决 / 改配；改配时在「改配竞品SKU」列填 SKU，
    也兼容直接写成「改配:SKU」。
    """
    path = Path(path)
    period = period or store.latest_period()
    decisions = []
    at = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    with path.open(encoding="utf-8-sig", newline="") as f:
        for i, r in enumerate(csv.DictReader(f)):
            raw = (r.get("人工确认") or "").strip()
            if not raw:
                continue
            conclusion, _, inline_sku = raw.replace("：", ":").partition(":")
            decisions.append(Decision(
                key=relation_key(r.get("我方SKU", "").strip(), r.get("竞品平台", "").strip(),
                                 r.get("竞品SKU", "").strip()),
                conclusion=conclusion.strip(),
                reassign_sku=(inline_sku or r.get("改配竞品SKU") or "").strip(),
                by="本地CSV",
                at=at,
                row_id=f"csv:{i}",
            ))
    result, _, _ = _apply_all(store, decisions, period)
    return result
