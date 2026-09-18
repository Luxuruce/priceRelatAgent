"""对接方式 D：云采集 API。

面向免安装的在线采集工具（八爪鱼云采集 / Apify / Firecrawl）和数据服务商接口。
和影刀不同，这类工具本身就跑在云上，我方只需要调 API 把结果拉回来。

不把任何厂商接口写死：URL、鉴权、分页、字段映射全部由配置驱动，
常见工具做成预设（preset），换厂商或对接自建接口只改 config.yaml。

两种模式：
    list   —— 拉取一个已经跑完的结果集，支持 offset / page / cursor 分页
              适用：八爪鱼云采集导出、Apify Dataset、数据服务商接口
    scrape —— 逐个 URL 让云端抓取并按 schema 抽取结构化数据
              适用：Firecrawl

拉回的数据落盘到 inbox，再由 FileCollector 统一导入 —— 与 HTTP 回调保持同一条路径，
采集和比价可以各自独立重跑，原始数据也会被归档留痕。

配置中的 ${NAME} 会先从该数据源的 vars 取值，取不到再读环境变量。
密钥一律走环境变量，不要明文写进配置文件。
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import requests

from .base import _to_str, map_headers

logger = logging.getLogger(__name__)

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Firecrawl 抽取用的默认 schema。字段名直接用标准字段，省掉一层映射。
_DEFAULT_PRODUCT_SCHEMA = {
    "type": "object",
    "properties": {
        "products": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "sku_id": {"type": "string", "description": "商品ID或商品编码，页面上没有则留空"},
                    "title": {"type": "string", "description": "完整商品标题，包含规格，不要截断"},
                    "price": {"type": "number", "description": "当前实际售价（元），不是划线价"},
                    "origin_price": {"type": "number", "description": "划线价或原价，没有则留空"},
                    "brand": {"type": "string"},
                    "spec_text": {"type": "string", "description": "规格或净含量，如 550ml*24瓶"},
                    "barcode": {"type": "string"},
                    "url": {"type": "string", "description": "商品详情页链接"},
                    "image_url": {"type": "string"},
                },
                "required": ["title"],
            },
        }
    },
    "required": ["products"],
}

_DEFAULT_PRODUCT_PROMPT = (
    "提取页面上所有商品的信息。title 保留完整标题（含规格）；"
    "price 取当前实际售价，不要取划线价；页面上没有的字段留空，不要编造。"
)

# ------------------------------------------------------------------
# 预设。用户配置会深度覆盖预设，所以预设里的任何一项都可以在 config.yaml 里改。
# ------------------------------------------------------------------
PRESETS: dict[str, dict[str, Any]] = {
    # Apify：GET /v2/datasets/{id}/items 返回纯数组，offset/limit 分页
    "apify": {
        "mode": "list",
        "method": "GET",
        "url": "https://api.apify.com/v2/datasets/${dataset_id}/items",
        "headers": {"Authorization": "Bearer ${APIFY_TOKEN}"},
        "params": {"format": "json", "clean": "true"},
        "items_path": "",
        "pagination": {
            "type": "offset",
            "offset_param": "offset",
            "limit_param": "limit",
            "page_size": 1000,
        },
    },
    # 八爪鱼 OpenAPI：密码模式换 token；单次最多 1000 条，服务端返回下一页 offset 与剩余条数。
    # 注意：token 接口已核实；数据导出的 URL 路径与数据列表所在字段未能从公开文档核实，
    # 使用前请在 https://openapi.bazhuayu.com/zh-CN/ 核对 url 与 items_path，不一致在 config.yaml 覆盖即可。
    "bazhuayu": {
        "mode": "list",
        "method": "GET",
        "url": "https://openapi.bazhuayu.com/data/all",
        "auth": {
            "type": "password_grant",
            "token_url": "https://openapi.bazhuayu.com/token",
            "username": "${BAZHUAYU_USERNAME}",
            "password": "${BAZHUAYU_PASSWORD}",
            "token_path": "data.access_token",
        },
        "params": {"taskId": "${task_id}"},
        "items_path": ["data.dataList", "data.data"],
        "pagination": {
            "type": "offset",
            "offset_param": "offset",
            "limit_param": "size",
            "page_size": 1000,
            "next_offset_path": "data.offset",
            "remaining_path": "data.restTotal",
        },
    },
    # Firecrawl v2：POST /v2/scrape，formats=[{type:json, schema, prompt}]，结果在 data.json
    "firecrawl": {
        "mode": "scrape",
        "method": "POST",
        "url": "https://api.firecrawl.dev/v2/scrape",
        "headers": {"Authorization": "Bearer ${FIRECRAWL_API_KEY}"},
        "items_path": "data.json.products",
        "schema": _DEFAULT_PRODUCT_SCHEMA,
        "prompt": _DEFAULT_PRODUCT_PROMPT,
        "timeout_ms": 60000,
        # 网页上经常拿不到商品编码，按商品链接或标题生成稳定 ID
        "derive_sku_id": True,
    },
}


class CloudSourceError(RuntimeError):
    pass


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def get_path(data: Any, path: str) -> Any:
    """按点路径取值：'data.items'、'images.0.url'。空路径返回原对象，取不到返回 None。"""
    if not path:
        return data
    current = data
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            idx = int(part)
            current = current[idx] if idx < len(current) else None
        else:
            return None
        if current is None:
            return None
    return current


class CloudSource:
    """一个云采集数据源。每个实例对应 config.yaml 里 ingest.cloud 的一项。"""

    def __init__(
        self,
        source_cfg: dict[str, Any],
        inbox_dir: str | Path,
        session: requests.Session | None = None,
    ):
        preset_name = source_cfg.get("preset", "")
        if preset_name and preset_name not in PRESETS:
            raise CloudSourceError(
                f"未知预设 {preset_name!r}，可选：{', '.join(PRESETS)}"
            )
        self.cfg = _deep_merge(PRESETS.get(preset_name, {}), source_cfg)

        self.name = str(self.cfg.get("name", ""))
        self.platform = str(self.cfg.get("platform", ""))
        # 两者都会拼进落盘文件名，必须限制字符集
        for label, value in (("name", self.name), ("platform", self.platform)):
            if not _SAFE_NAME.match(value):
                raise CloudSourceError(
                    f"数据源 {label}={value!r} 缺失或非法（仅允许字母数字下划线连字符）"
                )

        self.mode = self.cfg.get("mode", "list")
        if self.mode not in ("list", "scrape"):
            raise CloudSourceError(f"[{self.name}] 未知 mode {self.mode!r}，可选 list / scrape")
        if not self.cfg.get("url"):
            raise CloudSourceError(f"[{self.name}] 缺少 url")

        self.inbox = Path(inbox_dir)
        self.session = session or requests.Session()
        self.timeout = float(self.cfg.get("timeout_sec", 60))
        self.retries = int(self.cfg.get("retries", 3))
        self.max_pages = int(self.cfg.get("max_pages", 200))
        self._sleep = time.sleep  # 测试时替换
        self._token: str | None = None
        self._secrets: set[str] = set()

    # ------------------------------------------------------------------
    # 变量替换与脱敏
    # ------------------------------------------------------------------
    def _expand(self, value: Any) -> Any:
        """递归替换 ${NAME}：先查 vars，再查环境变量。环境变量的值记为密钥，用于日志脱敏。"""
        if isinstance(value, dict):
            return {k: self._expand(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._expand(v) for v in value]
        if not isinstance(value, str):
            return value

        variables = self.cfg.get("vars") or {}

        def replace(match: re.Match) -> str:
            key = match.group(1)
            if key in variables and _to_str(variables[key]):
                return str(variables[key])
            env_value = os.environ.get(key)
            if env_value:
                if len(env_value) >= 4:
                    self._secrets.add(env_value)
                return env_value
            raise CloudSourceError(
                f"[{self.name}] 缺少变量 {key}：请在该数据源的 vars 中填写，或设置同名环境变量"
            )

        return _VAR.sub(replace, value)

    def _redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------
    def _auth_header(self) -> dict[str, str]:
        auth = self.cfg.get("auth") or {}
        if auth.get("type") != "password_grant":
            return {}
        if self._token is None:
            self._token = self._fetch_token(auth)
        return {"Authorization": f"Bearer {self._token}"}

    def _fetch_token(self, auth: dict) -> str:
        payload = {
            "username": self._expand(auth.get("username", "")),
            "password": self._expand(auth.get("password", "")),
            "grant_type": "password",
        }
        data = self._send("POST", self._expand(auth["token_url"]), json_body=payload, with_auth=False)
        token = get_path(data, auth.get("token_path", "access_token"))
        if not token:
            raise CloudSourceError(f"[{self.name}] 获取 token 失败：返回中没有 {auth.get('token_path')}")
        self._secrets.add(str(token))
        return str(token)

    def _send(
        self,
        method: str,
        url: str,
        params: dict | None = None,
        json_body: Any = None,
        with_auth: bool = True,
        timeout: float | None = None,
    ) -> Any:
        """发请求并解析 JSON。429/5xx/网络错误指数退避重试；token 过期自动重取一次。"""
        refreshed = False
        attempt = 0
        while True:
            headers = {"Accept": "application/json", **self._expand(self.cfg.get("headers") or {})}
            if with_auth:
                headers.update(self._auth_header())

            try:
                resp = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=headers,
                    timeout=timeout or self.timeout,
                )
            except requests.RequestException as e:
                if attempt < self.retries:
                    attempt += 1
                    self._backoff(attempt, None)
                    continue
                raise CloudSourceError(self._redact(f"[{self.name}] 网络错误：{e}")) from None

            status = resp.status_code
            if status == 401 and with_auth and self._token is not None and not refreshed:
                # 八爪鱼 token 有效期一天，长任务中途过期是常态
                self._token = None
                refreshed = True
                continue
            if (status == 429 or status >= 500) and attempt < self.retries:
                attempt += 1
                self._backoff(attempt, resp.headers.get("Retry-After"))
                continue
            if status >= 400:
                raise CloudSourceError(
                    self._redact(f"[{self.name}] HTTP {status}：{resp.text[:300]}")
                )

            try:
                return resp.json()
            except ValueError:
                raise CloudSourceError(
                    self._redact(f"[{self.name}] 返回不是 JSON：{resp.text[:200]}")
                ) from None

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        delay = min(2 ** attempt, 30)
        if retry_after and retry_after.isdigit():
            delay = min(int(retry_after), 60)
        logger.warning("[%s] 请求受限或失败，%ds 后第 %d 次重试", self.name, delay, attempt)
        self._sleep(delay)

    def _items(self, data: Any) -> list:
        """按 items_path 取数据列表。允许配置多个候选路径，取第一个是列表的。"""
        paths = self.cfg.get("items_path", "")
        for path in paths if isinstance(paths, list) else [paths]:
            items = get_path(data, path)
            if isinstance(items, list):
                return items
        return []

    # ------------------------------------------------------------------
    # list 模式
    # ------------------------------------------------------------------
    def _iter_list(self) -> Iterator[dict]:
        method = self.cfg.get("method", "GET").upper()
        url = self._expand(self.cfg["url"])
        base_params = self._expand(self.cfg.get("params") or {})
        base_body = self._expand(self.cfg.get("body")) if self.cfg.get("body") else None

        pg = self.cfg.get("pagination") or {}
        ptype = pg.get("type", "none")
        page_size = int(pg.get("page_size", 100))
        in_body = pg.get("in") == "body"
        remaining_path = pg.get("remaining_path", "")

        offset = int(pg.get("start", 0))
        page = int(pg.get("start", 1))
        cursor: Any = None

        for _ in range(self.max_pages):
            paging: dict[str, Any] = {}
            if ptype == "offset":
                paging[pg.get("offset_param", "offset")] = offset
            elif ptype == "page":
                paging[pg.get("page_param", "page")] = page
            elif ptype == "cursor" and cursor is not None:
                paging[pg.get("cursor_param", "cursor")] = cursor
            if ptype != "none" and pg.get("limit_param"):
                paging[pg["limit_param"]] = page_size

            if in_body:
                params, body = base_params, {**(base_body or {}), **paging}
            else:
                params, body = {**base_params, **paging}, base_body

            data = self._send(method, url, params=params, json_body=body)
            items = self._items(data)
            yield from items

            if not items or ptype == "none":
                return

            # 服务端明确告知剩余条数时以它为准，否则按「不满一页即最后一页」判断
            if remaining_path:
                remaining = get_path(data, remaining_path)
                if remaining is not None and float(remaining) <= 0:
                    return
            elif ptype in ("offset", "page") and len(items) < page_size:
                return

            if ptype == "offset":
                next_offset = get_path(data, pg["next_offset_path"]) if pg.get("next_offset_path") else None
                if next_offset is None:
                    offset += len(items)
                elif int(next_offset) == offset:
                    return  # 服务端没有推进 offset，继续请求会死循环
                else:
                    offset = int(next_offset)
            elif ptype == "page":
                page += 1
            elif ptype == "cursor":
                cursor = get_path(data, pg.get("cursor_path", "next_cursor"))
                if not cursor:
                    return

        logger.warning(
            "[%s] 已达到 max_pages=%d 上限，数据可能不完整。确有需要请调大 max_pages",
            self.name, self.max_pages,
        )

    # ------------------------------------------------------------------
    # scrape 模式
    # ------------------------------------------------------------------
    def _iter_scrape(self) -> Iterator[dict]:
        urls = self.cfg.get("urls") or []
        if not urls:
            raise CloudSourceError(f"[{self.name}] scrape 模式需要配置 urls")

        endpoint = self._expand(self.cfg["url"])
        timeout_ms = int(self.cfg.get("timeout_ms", 60000))
        failed = 0

        for page_url in urls:
            body = {
                "url": page_url,
                "formats": [
                    {
                        "type": "json",
                        "schema": self.cfg.get("schema") or _DEFAULT_PRODUCT_SCHEMA,
                        "prompt": self.cfg.get("prompt") or _DEFAULT_PRODUCT_PROMPT,
                    }
                ],
                "timeout": timeout_ms,
            }
            try:
                # HTTP 超时要比云端抓取超时更长，否则会在对方还在渲染页面时提前断开
                data = self._send("POST", endpoint, json_body=body, timeout=timeout_ms / 1000 + 30)
            except CloudSourceError as e:
                failed += 1
                logger.error("%s（页面 %s）", e, page_url)
                continue

            if isinstance(data, dict) and data.get("success") is False:
                failed += 1
                logger.error("[%s] 抓取失败 %s：%s", self.name, page_url, self._redact(str(data.get("error", ""))[:200]))
                continue

            items = self._items(data)
            logger.info("[%s] %s → %d 条", self.name, page_url, len(items))
            for item in items:
                if isinstance(item, dict):
                    item.setdefault("source_page", page_url)
                    yield item

        if failed == len(urls):
            raise CloudSourceError(f"[{self.name}] 全部 {failed} 个页面抓取失败")

    # ------------------------------------------------------------------
    # 字段映射与落盘
    # ------------------------------------------------------------------
    def _map_record(self, raw: Any) -> dict | None:
        if not isinstance(raw, dict):
            return None

        field_map: dict[str, str] = self.cfg.get("field_map") or {}
        # 已被 field_map 显式映射的标准字段，源数据里同义的原始列要丢掉，
        # 否则导入时两列会被识别成同一个标准字段，互相覆盖
        mapped_fields = set(field_map)

        record: dict[str, Any] = {}
        for key, value in raw.items():
            if isinstance(value, (dict, list)):
                continue
            standard = map_headers([key]).get(key)
            if standard in mapped_fields and key != standard:
                continue
            record[key] = value

        for standard, path in field_map.items():
            value = get_path(raw, path)
            if value is not None and not isinstance(value, (dict, list)):
                record[standard] = value

        if self.cfg.get("derive_sku_id") and not _to_str(record.get("sku_id")):
            basis = _to_str(record.get("url")) or _to_str(record.get("title"))
            if basis:
                record["sku_id"] = "auto-" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]

        return record

    def fetch(self) -> list[dict]:
        """拉取并映射全部记录。"""
        raw_iter = self._iter_scrape() if self.mode == "scrape" else self._iter_list()
        records = [r for r in (self._map_record(raw) for raw in raw_iter) if r is not None]

        # 映射配错是最常见的问题：数据拉回来了，但一条都导入不了。这里提前报出来。
        usable = sum(
            1 for r in records
            if {"sku_id", "title"} <= set(map_headers([k for k in r if _to_str(r[k])]).values())
        )
        if records and usable == 0:
            sample = sorted(records[0].keys())
            raise CloudSourceError(
                f"[{self.name}] 拉取到 {len(records)} 条，但没有一条同时含商品ID与标题。"
                f"请检查 field_map，源数据字段：{sample}"
            )
        if usable < len(records):
            logger.warning(
                "[%s] %d 条缺少商品ID或标题，导入时将被跳过", self.name, len(records) - usable
            )
        return records

    def fetch_to_inbox(self) -> tuple[Path | None, int]:
        """拉取并落盘到 inbox，返回 (文件路径, 条数)。无数据时不落盘。"""
        records = self.fetch()
        if not records:
            logger.warning("[%s] 未拉取到任何数据", self.name)
            return None, 0

        self.inbox.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        target = self.inbox / f"{self.platform}_cloud-{self.name}_{stamp}.json"
        target.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("[%s] 拉取 %d 条 → %s", self.name, len(records), target.name)
        return target, len(records)
