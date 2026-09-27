"""R2 多维表格复核与回写测试。用内存表替代多维表格。"""

import csv
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pricerelat import cli
from pricerelat.ingest.base import enrich
from pricerelat.matching.pipeline import match_platform
from pricerelat.models import Product, RelationStatus
from pricerelat.review import bitable
from pricerelat.review.bitable import BitableError, LarkCliTable, Row
from pricerelat.review.sync import push, relation_key, writeback, writeback_csv
from pricerelat.store import Store

ROOT = Path(__file__).resolve().parents[1]
PERIOD = "2026-W40"
CFG = {
    "matching": {
        "rules": {"enable_barcode": True, "enable_brand_spec": True},
        "fuzzy": {"top_k": 5, "auto_accept": 88, "reject_below": 55, "spec_mismatch_penalty": 12},
        "llm": {"enabled": False},
    },
}


class FakeTable:
    """内存版复核表。create 追加行；update 只改传入的字段，模拟多维表格的行为。"""

    def __init__(self):
        self.rows: list[Row] = []
        self.clock = 0

    def list_rows(self):
        return [Row(r.record_id, dict(r.fields)) for r in self.rows]

    def create_rows(self, rows):
        for fields in rows:
            self.rows.append(Row(f"rec{len(self.rows)}", dict(fields)))

    def update_rows(self, updates):
        for r in self.rows:
            if r.record_id in updates:
                r.fields.update(updates[r.record_id])

    def human_fill(self, key, conclusion, reassign="", who="专员A"):
        """模拟人工在最新一行填结论。"""
        row = [r for r in self.rows if r.fields["关系键"] == key][-1]
        self.clock += 1
        row.fields.update({
            "人工结论": [conclusion], "改配竞品SKU": reassign,
            "最后修改人": [{"id": "ou_x", "name": who}],
            "最后修改时间": f"2026-09-28T10:00:{self.clock:02d}.000+08:00",
        })
        return row


class BrokenTable:
    def list_rows(self):
        raise BitableError("无权限")

    create_rows = update_rows = list_rows


def make(platform, sku, title, price=10.0):
    return enrich(Product(platform=platform, platform_name=platform, sku_id=sku, title=title, price=price))


SELFS = [
    make("self", "S1", "蒙牛 特仑苏 纯牛奶 250ml*16盒", 80),
    make("self", "S2", "康师傅 红烧牛肉面 103g*5包", 15),
    make("self", "S3", "维达 蓝色经典 抽纸 150抽*24包", 60),
]
RIVALS = [
    make("sams", "R1", "蒙牛 纯牛奶 250ml×16盒", 60),        # 产品线仅一方标注 → 灰区
    make("sams", "R2", "康师傅 经典红烧牛肉面 103g×5袋 促销装", 14),
    make("sams", "R3", "维达 蓝色经典 软抽 150抽*24", 55),  # 灰区
]
# 改配目标：本期采集到，但不参与匹配
REASSIGN_TARGET = make("sams", "R9", "蒙牛 特仑苏 纯牛奶 250ml*16盒 礼盒", 85)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "t.db") as s:
        s.record_observations(PERIOD, "self", SELFS)
        s.record_observations(PERIOD, "sams", RIVALS + [REASSIGN_TARGET])
        match_platform(SELFS, RIVALS, CFG, store=s, period=PERIOD)
        s.commit()
        yield s


def pending_keys(store):
    return {relation_key(r.self_sku, r.platform, r.rival_sku)
            for r in store.relations(status=RelationStatus.PENDING)}


def test_push_writes_pending_once(store):
    """运行后复核表出现全部待复核项，重复运行不产生重复行。"""
    table = FakeTable()
    keys = pending_keys(store)
    assert keys, "测试数据应产生待复核项"

    assert push(store, table, PERIOD) == len(keys)
    assert {r.fields["关系键"] for r in table.rows} == keys
    row = table.rows[0].fields
    assert row["比价期次"] == PERIOD and row["回写状态"] == "未回写"
    assert row["进入复核原因"] and row["我方商品"] and row["竞品商品"]

    assert push(store, table, PERIOD) == 0
    assert len(table.rows) == len(keys)


def test_writeback_confirm_reject_reassign(store):
    """确认 / 否决 / 改配各一条，关系库状态分别更新，三行回写状态为已回写。"""
    table = FakeTable()
    push(store, table, PERIOD)
    keys = sorted(pending_keys(store))
    # 额外构造一条待复核关系，保证三种结论各有一条
    s2 = store.get_relation("S2", "sams", "R2")
    s2.status = RelationStatus.PENDING
    store.save_relation(s2)
    push(store, table, PERIOD)
    keys = sorted(pending_keys(store))
    assert len(keys) == 3
    k_confirm, k_reject, k_reassign = keys[0], keys[1], keys[2]

    table.human_fill(k_confirm, "确认")
    table.human_fill(k_reject, "否决")
    table.human_fill(k_reassign, "改配", reassign="R9")

    result = writeback(store, table, PERIOD)
    assert result.applied == 3 and not result.failed

    def rel(key):
        return store.get_relation(*key.split("|"))

    assert rel(k_confirm).status is RelationStatus.CONFIRMED
    assert rel(k_confirm).confirmed_by == "专员A"
    assert rel(k_reject).status is RelationStatus.REJECTED
    self_sku = k_reassign.split("|")[0]
    new = store.get_relation(self_sku, "sams", "R9")
    assert new.status is RelationStatus.CONFIRMED and new.source == "人工确认"
    assert rel(k_reassign).status is RelationStatus.REJECTED

    done = [r.fields for r in table.rows if r.fields.get("人工结论")]
    assert all(f["回写状态"] == "已回写" for f in done)
    assert all(f["处理人"] == "专员A" and f["处理时间"].startswith("2026-09-28 10:00") for f in done)


def test_reassign_to_missing_sku_fails(store):
    table = FakeTable()
    push(store, table, PERIOD)
    key = sorted(pending_keys(store))[0]
    before = store.get_relation(*key.split("|")).status
    table.human_fill(key, "改配", reassign="NOT_EXIST")

    result = writeback(store, table, PERIOD)
    assert result.failed == [(key, "改配 SKU 不存在")]
    row = [r for r in table.rows if r.fields["关系键"] == key][0].fields
    assert row["回写状态"] == "回写失败" and row["回写失败原因"] == "改配 SKU 不存在"
    assert store.get_relation(*key.split("|")).status is before
    assert store.get_relation(key.split("|")[0], "sams", "NOT_EXIST") is None


def test_reassign_without_sku_fails(store):
    table = FakeTable()
    push(store, table, PERIOD)
    key = sorted(pending_keys(store))[0]
    table.human_fill(key, "改配")
    result = writeback(store, table, PERIOD)
    assert "未填写改配竞品SKU" in result.failed[0][1]


def test_latest_row_wins(store):
    """同一关系被多行处理，以最后修改时间最新的一行为准。"""
    table = FakeTable()
    push(store, table, PERIOD)
    key = sorted(pending_keys(store))[0]
    first = table.human_fill(key, "确认")
    # 同一关系再出现一行（例如另一位专员在复制的行里处理）
    table.create_rows([{**first.fields, "人工结论": None}])
    table.human_fill(key, "否决", who="专员B")

    result = writeback(store, table, PERIOD)
    assert result.applied == 1 and result.superseded == 1
    assert store.get_relation(*key.split("|")).status is RelationStatus.REJECTED
    assert all(r.fields["回写状态"] == "已回写" for r in table.rows if r.fields["关系键"] == key)


def test_push_never_touches_human_rows(store):
    """同周重跑：已有人工结论或处理状态的行不删、不改（C-7）。"""
    table = FakeTable()
    push(store, table, PERIOD)
    key = sorted(pending_keys(store))[0]
    table.human_fill(key, "确认")
    snapshot = [dict(r.fields) for r in table.rows]
    push(store, table, PERIOD)
    assert [dict(r.fields) for r in table.rows][: len(snapshot)] == snapshot


def test_confirmed_relation_reused_next_run(store):
    table = FakeTable()
    push(store, table, PERIOD)
    key = relation_key("S1", "sams", "R1")
    assert key in pending_keys(store)
    table.human_fill(key, "确认")
    writeback(store, table, PERIOD)

    pairs, stats = match_platform(SELFS, RIVALS, CFG, store=store, period=PERIOD)
    assert pairs["self:S1"].confirmed
    assert stats.layered == 0


def test_writeback_from_csv(store, tmp_path):
    key = sorted(pending_keys(store))[0]
    self_sku, platform, rival_sku = key.split("|")
    path = tmp_path / "review.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["我方SKU", "竞品平台", "竞品SKU", "人工确认", "改配竞品SKU"])
        w.writeheader()
        w.writerow({"我方SKU": self_sku, "竞品平台": platform, "竞品SKU": rival_sku, "人工确认": "否决"})
    result = writeback_csv(store, path, PERIOD)
    assert result.applied == 1
    assert store.get_relation(self_sku, platform, rival_sku).status is RelationStatus.REJECTED


def test_compare_completes_when_bitable_unavailable(tmp_path, monkeypatch, capsys):
    """多维表格不可访问：比价照常完成，生成本地 CSV，摘要含同步失败提示。"""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for f in (ROOT / "data/samples").glob("*.csv"):
        shutil.copy(f, inbox / f.name)
    cfg = yaml.safe_load((ROOT / "config/config.yaml").read_text(encoding="utf-8"))
    cfg["ingest"]["inbox_dir"] = str(inbox)
    cfg["ingest"]["archive_dir"] = str(tmp_path / "raw")
    cfg["store"]["path"] = str(tmp_path / "state.db")
    cfg["matching"]["llm"]["enabled"] = False
    cfg["matching"]["review"]["export_path"] = str(tmp_path / "review.csv")
    cfg["report"]["output_dir"] = str(tmp_path / "out")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")

    monkeypatch.setattr(bitable, "open_table", lambda cfg: BrokenTable())
    assert cli.main(["-c", str(cfg_path), "compare", "--period", PERIOD]) == 0

    out = capsys.readouterr().out
    assert "复核表同步失败" in out
    assert (tmp_path / "review.csv").exists()
    assert (tmp_path / "out" / "比价报告.html").exists()


def test_lark_cli_table_parses_rows(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/lark-cli")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        data = {"fields": ["关系键", "回写状态"], "record_id_list": ["rec1"],
                "data": [["S|p|R", ["未回写"]]], "has_more": False}
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"ok": True, "data": data}), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    rows = LarkCliTable("tok", "匹配复核").list_rows()
    assert rows[0].record_id == "rec1" and rows[0].text("回写状态") == "未回写"
    assert calls[0][:4] == ["lark-cli", "base", "+record-list", "--as"]


def test_lark_cli_error_raises(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/lark-cli")
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 1, stdout=json.dumps({"ok": False, "error": {"message": "permission denied"}}), stderr=""))
    with pytest.raises(BitableError, match="permission denied"):
        LarkCliTable("tok", "匹配复核").list_rows()
