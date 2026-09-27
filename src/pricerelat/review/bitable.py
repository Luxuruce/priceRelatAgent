"""多维表格读写。

试点期用个人身份通过 lark-cli 写入（lark-cli 负责登录态，项目里不存凭证）；
推广前切换为自建应用，只需要换掉这一层的实现，同步逻辑不动。
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Protocol

logger = logging.getLogger(__name__)

_BATCH = 200  # 多维表格单次批量写入上限


class BitableError(RuntimeError):
    """多维表格不可访问：未登录、无权限、网络错误等。调用方据此降级到本地 CSV。"""


@dataclass
class Row:
    record_id: str
    fields: dict = field(default_factory=dict)

    def text(self, name: str) -> str:
        """取单元格的文本值。单选读出来是数组，人员是 [{id, name}]，统一摊成字符串。"""
        value = self.fields.get(name)
        if value is None:
            return ""
        if isinstance(value, list):
            parts = [v.get("name", v.get("id", "")) if isinstance(v, dict) else str(v) for v in value]
            return ",".join(p for p in parts if p)
        return str(value).strip()


class ReviewTable(Protocol):
    def list_rows(self) -> list[Row]: ...
    def create_rows(self, rows: list[dict]) -> None: ...
    def update_rows(self, updates: dict[str, dict]) -> None: ...


class LarkCliTable:
    """通过 lark-cli 读写一张多维表格。"""

    def __init__(self, base_token: str, table: str, identity: str = "user", timeout: int = 120):
        if not shutil.which("lark-cli"):
            raise BitableError("未找到 lark-cli，无法访问多维表格")
        self.base_token = base_token
        self.table = table
        self.identity = identity
        self.timeout = timeout

    def _run(self, shortcut: str, *args: str) -> dict:
        cmd = [
            "lark-cli", "base", shortcut, "--as", self.identity,
            "--base-token", self.base_token, "--table-id", self.table, *args,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired as e:
            raise BitableError(f"lark-cli {shortcut} 超时") from e
        try:
            out = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise BitableError(f"lark-cli {shortcut} 返回无法解析：{(proc.stderr or proc.stdout)[:200]}") from e
        if not out.get("ok"):
            err = out.get("error") or {}
            raise BitableError(f"lark-cli {shortcut} 失败：{err.get('message') or err}")
        return out.get("data") or {}

    def list_rows(self) -> list[Row]:
        rows: list[Row] = []
        offset = 0
        while True:
            data = self._run("+record-list", "--format", "json",
                             "--offset", str(offset), "--limit", str(_BATCH))
            names = data.get("fields", [])
            for rid, values in zip(data.get("record_id_list", []), data.get("data", [])):
                rows.append(Row(record_id=rid, fields=dict(zip(names, values))))
            if not data.get("has_more"):
                return rows
            offset += _BATCH

    def create_rows(self, rows: list[dict]) -> None:
        for i in range(0, len(rows), _BATCH):
            payload = {"create_records": rows[i : i + _BATCH]}
            self._run("+record-batch-create", "--json", json.dumps(payload, ensure_ascii=False))
            if i + _BATCH < len(rows):
                time.sleep(0.5)  # 连续写同一张表时串行并稍作间隔，避免并发写冲突

    def update_rows(self, updates: dict[str, dict]) -> None:
        items = list(updates.items())
        for i in range(0, len(items), _BATCH):
            payload = {"update_records": dict(items[i : i + _BATCH])}
            self._run("+record-batch-update", "--json", json.dumps(payload, ensure_ascii=False))
            if i + _BATCH < len(items):
                time.sleep(0.5)


def open_table(cfg: dict) -> LarkCliTable | None:
    """按配置打开复核表。未配置 base_token 时返回 None（不启用多维表格）。"""
    import os

    b_cfg = cfg.get("matching", {}).get("review", {}).get("bitable", {})
    if not b_cfg.get("enabled", True):
        return None
    token = b_cfg.get("base_token") or os.getenv("FEISHU_REVIEW_BASE_TOKEN", "")
    if not token:
        return None
    return LarkCliTable(
        base_token=token,
        table=b_cfg.get("table", "匹配复核"),
        identity=b_cfg.get("identity", "user"),
    )
