"""云采集适配器测试。

用假会话模拟各厂商的响应形态，不打真实接口。响应形态按厂商文档构造：
    Apify     —— 纯数组，offset/limit 分页
    八爪鱼     —— 密码模式换 token，服务端返回下一页 offset 与剩余条数
    Firecrawl —— POST /v2/scrape，结果在 data.json
"""

import json
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pricerelat.ingest.cloud_api import CloudSource, CloudSourceError, get_path
from pricerelat.ingest.file_collector import FileCollector


class FakeResponse:
    def __init__(self, payload=None, status=200, headers=None, text=None):
        self._payload = payload
        self.status_code = status
        self.headers = headers or {}
        self.text = text if text is not None else json.dumps(payload, ensure_ascii=False)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """按顺序返回预设响应，并记录每次请求。handler 可按请求内容动态生成响应。"""

    def __init__(self, responses=None, handler=None):
        self.responses = list(responses or [])
        self.handler = handler
        self.calls = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        call = {"method": method, "url": url, "params": params, "json": json, "headers": headers}
        self.calls.append(call)
        if self.handler:
            return self.handler(call)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_source(cfg, session, tmp_path):
    src = CloudSource({"name": "t", "platform": "sams", **cfg}, tmp_path, session=session)
    src._sleep = lambda _: None
    return src


def product(i, **extra):
    return {"sku_id": f"P{i}", "title": f"农夫山泉 饮用天然水 550ml*24瓶 #{i}", "price": 39.9, **extra}


# ----------------------------------------------------------------------
# 预设
# ----------------------------------------------------------------------
class TestApifyPreset:
    def test_offset_pagination_until_short_page(self, tmp_path, monkeypatch):
        monkeypatch.setenv("APIFY_TOKEN", "apify-secret-token")
        session = FakeSession([
            FakeResponse([product(1), product(2)]),
            FakeResponse([product(3), product(4)]),
            FakeResponse([product(5)]),
        ])
        src = make_source(
            {"preset": "apify", "vars": {"dataset_id": "ds123"}, "pagination": {"page_size": 2}},
            session, tmp_path,
        )
        records = src.fetch()

        assert [r["sku_id"] for r in records] == ["P1", "P2", "P3", "P4", "P5"]
        assert len(session.calls) == 3  # 第三页不满一页即停，不多发请求
        assert [c["params"]["offset"] for c in session.calls] == [0, 2, 4]
        assert session.calls[0]["params"]["limit"] == 2
        assert session.calls[0]["url"] == "https://api.apify.com/v2/datasets/ds123/items"
        assert session.calls[0]["headers"]["Authorization"] == "Bearer apify-secret-token"


class TestBazhuayuPreset:
    def _handler(self, pages):
        state = {"token_calls": 0, "data_calls": 0}

        def handle(call):
            if call["url"].endswith("/token"):
                state["token_calls"] += 1
                assert call["json"]["grant_type"] == "password"
                return FakeResponse({"data": {"access_token": f"tok{state['token_calls']}"}})
            page = pages[state["data_calls"]]
            state["data_calls"] += 1
            return FakeResponse(page)

        return handle, state

    def test_token_and_server_driven_offset(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BAZHUAYU_USERNAME", "user@example.com")
        monkeypatch.setenv("BAZHUAYU_PASSWORD", "pw-secret")
        pages = [
            {"data": {"offset": 107, "restTotal": 1, "dataList": [product(1), product(2)]}},
            {"data": {"offset": 108, "restTotal": 0, "dataList": [product(3)]}},
        ]
        handler, state = self._handler(pages)
        session = FakeSession(handler=handler)
        src = make_source({"preset": "bazhuayu", "vars": {"task_id": "task-9"}}, session, tmp_path)

        records = src.fetch()

        assert len(records) == 3
        assert state["token_calls"] == 1  # token 只取一次
        data_calls = [c for c in session.calls if not c["url"].endswith("/token")]
        assert data_calls[0]["params"]["offset"] == 0
        assert data_calls[1]["params"]["offset"] == 107  # 用服务端返回的 offset，而不是自己累加
        assert data_calls[0]["params"]["taskId"] == "task-9"
        assert data_calls[0]["params"]["size"] == 1000
        assert data_calls[0]["headers"]["Authorization"] == "Bearer tok1"

    def test_items_path_fallback(self, tmp_path, monkeypatch):
        """导出接口的数据列表字段未核实，预设允许在多个候选路径中择一。"""
        monkeypatch.setenv("BAZHUAYU_USERNAME", "u")
        monkeypatch.setenv("BAZHUAYU_PASSWORD", "p")
        pages = [{"data": {"offset": 1, "restTotal": 0, "data": [product(1)]}}]
        handler, _ = self._handler(pages)
        src = make_source({"preset": "bazhuayu", "vars": {"task_id": "x"}}, FakeSession(handler=handler), tmp_path)
        assert len(src.fetch()) == 1

    def test_expired_token_refreshed_once(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BAZHUAYU_USERNAME", "u")
        monkeypatch.setenv("BAZHUAYU_PASSWORD", "p")
        state = {"token": 0, "data": 0}

        def handle(call):
            if call["url"].endswith("/token"):
                state["token"] += 1
                return FakeResponse({"data": {"access_token": f"tok{state['token']}"}})
            state["data"] += 1
            if call["headers"]["Authorization"] == "Bearer tok1":
                return FakeResponse({"message": "expired"}, status=401)
            return FakeResponse({"data": {"offset": 1, "restTotal": 0, "dataList": [product(1)]}})

        src = make_source({"preset": "bazhuayu", "vars": {"task_id": "x"}}, FakeSession(handler=handle), tmp_path)
        assert len(src.fetch()) == 1
        assert state["token"] == 2


class TestFirecrawlPreset:
    def test_scrape_each_url_and_skip_failures(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-secret-key")
        session = FakeSession([
            FakeResponse({"success": True, "data": {"json": {"products": [
                {"title": "金龙鱼 食用调和油 5L", "price": 69.9, "url": "https://shop.example.com/p/1"},
                {"title": "海天 金标生抽 1.9L", "price": 29.9},
            ]}}}),
            FakeResponse({"success": False, "error": "page blocked"}),
        ])
        src = make_source(
            {"preset": "firecrawl", "urls": ["https://shop.example.com/list/1", "https://shop.example.com/list/2"]},
            session, tmp_path,
        )
        records = src.fetch()

        assert len(records) == 2
        body = session.calls[0]["json"]
        assert body["url"] == "https://shop.example.com/list/1"
        assert body["formats"][0]["type"] == "json"
        assert "products" in body["formats"][0]["schema"]["properties"]
        # 页面上没有商品编码：有链接按链接生成，否则按标题生成，且结果稳定
        assert all(r["sku_id"].startswith("auto-") for r in records)
        assert records[0]["sku_id"] != records[1]["sku_id"]
        assert records[1]["source_page"] == "https://shop.example.com/list/1"

    def test_all_pages_failed_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FIRECRAWL_API_KEY", "k")
        session = FakeSession([FakeResponse({"success": False, "error": "blocked"})])
        src = make_source({"preset": "firecrawl", "urls": ["https://a.example.com"]}, session, tmp_path)
        with pytest.raises(CloudSourceError, match="全部"):
            src.fetch()


# ----------------------------------------------------------------------
# 通用能力
# ----------------------------------------------------------------------
class TestGenericList:
    def test_cursor_pagination(self, tmp_path):
        session = FakeSession([
            FakeResponse({"items": [product(1)], "next": "c2"}),
            FakeResponse({"items": [product(2)], "next": None}),
        ])
        src = make_source({
            "url": "https://api.example.com/p",
            "items_path": "items",
            "pagination": {"type": "cursor", "cursor_param": "after", "cursor_path": "next"},
        }, session, tmp_path)

        assert len(src.fetch()) == 2
        assert "after" not in session.calls[0]["params"]
        assert session.calls[1]["params"]["after"] == "c2"

    def test_page_pagination_in_body(self, tmp_path):
        session = FakeSession([
            FakeResponse({"data": {"list": [product(1), product(2)]}}),
            FakeResponse({"data": {"list": []}}),
        ])
        src = make_source({
            "method": "POST",
            "url": "https://api.example.com/p",
            "body": {"city": "上海"},
            "items_path": "data.list",
            "pagination": {"type": "page", "page_param": "pageNo", "limit_param": "pageSize", "page_size": 2, "in": "body"},
        }, session, tmp_path)

        assert len(src.fetch()) == 2
        assert session.calls[0]["json"] == {"city": "上海", "pageNo": 1, "pageSize": 2}
        assert session.calls[1]["json"]["pageNo"] == 2

    def test_max_pages_guard(self, tmp_path):
        """接口不停返回满页数据时，靠 max_pages 兜底，不能死循环。"""
        session = FakeSession(handler=lambda call: FakeResponse([product(call["params"]["page"])]))
        src = make_source({
            "url": "https://api.example.com/p",
            "pagination": {"type": "page", "page_size": 1},
            "max_pages": 3,
        }, session, tmp_path)
        assert len(src.fetch()) == 3
        assert len(session.calls) == 3

    def test_offset_not_advancing_stops(self, tmp_path):
        session = FakeSession(handler=lambda call: FakeResponse({"offset": 0, "rows": [product(1)]}))
        src = make_source({
            "url": "https://api.example.com/p",
            "items_path": "rows",
            "pagination": {"type": "offset", "page_size": 1, "next_offset_path": "offset"},
        }, session, tmp_path)
        src.fetch()
        assert len(session.calls) == 1


class TestFieldMapping:
    def test_dotted_paths_and_duplicate_columns(self, tmp_path):
        raw = {
            "id": "X1",
            "name": "可口可乐 汽水 330ml*24罐",
            "商品名称": "旧字段，不应与映射后的 title 冲突",
            "pricing": {"current": 49.9},
            "images": [{"src": "https://img.example.com/1.jpg"}],
        }
        src = make_source({
            "url": "https://api.example.com/p",
            "field_map": {"sku_id": "id", "title": "name", "price": "pricing.current", "image_url": "images.0.src"},
        }, FakeSession([FakeResponse([raw])]), tmp_path)

        record = src.fetch()[0]
        assert record["sku_id"] == "X1"
        assert record["title"] == "可口可乐 汽水 330ml*24罐"
        assert record["price"] == 49.9
        assert record["image_url"] == "https://img.example.com/1.jpg"
        assert "商品名称" not in record
        assert "pricing" not in record  # 嵌套对象不透传

    def test_wrong_mapping_reported_with_source_fields(self, tmp_path):
        """数据拉回来了但一条都导入不了，必须报错并给出源字段，而不是静默产出空结果。"""
        src = make_source(
            {"url": "https://api.example.com/p"},
            FakeSession([FakeResponse([{"goodsNo": "1", "goodsName": "x"}])]),
            tmp_path,
        )
        with pytest.raises(CloudSourceError, match="goodsName"):
            src.fetch()


class TestRobustness:
    def test_retry_on_429_then_success(self, tmp_path):
        session = FakeSession([
            FakeResponse({"error": "rate limited"}, status=429, headers={"Retry-After": "2"}),
            requests.ConnectionError("reset"),
            FakeResponse([product(1)]),
        ])
        slept = []
        src = make_source({"url": "https://api.example.com/p"}, session, tmp_path)
        src._sleep = slept.append

        assert len(src.fetch()) == 1
        assert slept[0] == 2  # 遵守 Retry-After

    def test_client_error_not_retried(self, tmp_path):
        session = FakeSession([FakeResponse({"error": "bad"}, status=400)])
        src = make_source({"url": "https://api.example.com/p"}, session, tmp_path)
        with pytest.raises(CloudSourceError, match="HTTP 400"):
            src.fetch()
        assert len(session.calls) == 1

    def test_missing_variable_names_the_variable(self, tmp_path, monkeypatch):
        monkeypatch.delenv("APIFY_TOKEN", raising=False)
        src = make_source({"preset": "apify", "vars": {"dataset_id": "d"}}, FakeSession(), tmp_path)
        with pytest.raises(CloudSourceError, match="APIFY_TOKEN"):
            src.fetch()

    def test_secret_redacted_in_errors(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VENDOR_KEY", "sk-very-secret-value")
        session = FakeSession([FakeResponse(None, status=403, text="invalid key sk-very-secret-value")])
        src = make_source({
            "url": "https://api.example.com/p",
            "headers": {"X-Api-Key": "${VENDOR_KEY}"},
            "retries": 0,
        }, session, tmp_path)
        with pytest.raises(CloudSourceError) as exc:
            src.fetch()
        assert "sk-very-secret-value" not in str(exc.value)
        assert "***" in str(exc.value)

    def test_unsafe_name_rejected(self, tmp_path):
        with pytest.raises(CloudSourceError):
            CloudSource({"name": "../evil", "platform": "sams", "url": "https://x"}, tmp_path)

    def test_get_path(self):
        data = {"a": {"b": [{"c": 1}]}}
        assert get_path(data, "a.b.0.c") == 1
        assert get_path(data, "a.b.5.c") is None
        assert get_path(data, "") is data


def test_end_to_end_inbox_to_products(tmp_path, monkeypatch):
    """拉取落盘后，FileCollector 应能把它导入成带规格解析的 Product。"""
    monkeypatch.setenv("APIFY_TOKEN", "t0ken")
    session = FakeSession([FakeResponse([
        {"productId": "W1", "name": "金龙鱼 食用调和油 5L", "price": {"current": "¥65.90"}},
    ])])
    src = make_source({
        "preset": "apify",
        "vars": {"dataset_id": "d"},
        "field_map": {"sku_id": "productId", "title": "name", "price": "price.current"},
    }, session, tmp_path)

    path, count = src.fetch_to_inbox()
    assert count == 1 and path.name.startswith("sams_cloud-t_")

    products = FileCollector(tmp_path).collect("sams", "山姆")
    assert len(products) == 1
    assert products[0].price == pytest.approx(65.9)
    assert products[0].spec.total_base == pytest.approx(5000)
    assert products[0].brand == "金龙鱼"
