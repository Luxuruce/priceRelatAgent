"""对接方式 B：HTTP 回调接收。

影刀 / UiPath 的 OpenAPI 都没有主动回调机制 —— 任务跑完不会推给你。
所以实践上要在机器人流程的**最后一步**加一个「发送 HTTP 请求」指令，
把抓到的数据 POST 到这个服务。

用标准库 http.server，不引入 web 框架：这个服务只做一件事，
接收数据后落盘到 inbox，剩下的交给 FileCollector。
落盘而非直接入内存，是为了 RPA 和比价流程能各自独立重跑。

启动：python run.py serve
RPA 侧 POST 到：http://<host>:8770/api/ingest?platform=sams
Body 支持两种：{"items": [...]} 或直接 [...]
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 32 * 1024 * 1024  # 32MB，防止超大请求打爆内存
_SAFE_PLATFORM = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def _make_handler(inbox: Path, token: str):
    class IngestHandler(BaseHTTPRequestHandler):
        # 默认的 log_message 会往 stderr 打印，接到 logging 上统一管理
        def log_message(self, fmt: str, *args) -> None:
            logger.debug("%s - %s", self.address_string(), fmt % args)

        def _reply(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if urlparse(self.path).path == "/health":
                self._reply(200, {"ok": True})
            else:
                self._reply(404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != "/api/ingest":
                self._reply(404, {"ok": False, "error": "not found"})
                return

            # 鉴权：配置了 token 才校验，方便本地调试
            if token:
                supplied = self.headers.get("X-Auth-Token", "")
                if supplied != token:
                    self._reply(401, {"ok": False, "error": "unauthorized"})
                    return

            params = parse_qs(parsed.query)
            platform = (params.get("platform") or [""])[0].strip()
            # 平台名会拼进文件名，必须限制字符集防止路径穿越
            if not _SAFE_PLATFORM.match(platform):
                self._reply(
                    400,
                    {"ok": False, "error": "platform 缺失或非法（仅允许字母数字下划线连字符）"},
                )
                return

            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._reply(400, {"ok": False, "error": "invalid Content-Length"})
                return
            if length <= 0:
                self._reply(400, {"ok": False, "error": "empty body"})
                return
            if length > MAX_BODY_BYTES:
                self._reply(413, {"ok": False, "error": "body too large"})
                return

            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                self._reply(400, {"ok": False, "error": f"invalid JSON: {e}"})
                return

            items = payload.get("items") if isinstance(payload, dict) else payload
            if not isinstance(items, list):
                self._reply(400, {"ok": False, "error": "期望 {\"items\": [...]} 或 [...]"})
                return

            inbox.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            target = inbox / f"{platform}_{stamp}.json"
            target.write_text(
                json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            logger.info("[%s] 收到 %d 条 → %s", platform, len(items), target.name)
            self._reply(200, {"ok": True, "received": len(items), "file": target.name})

    return IngestHandler


def serve(inbox_dir: str | Path, host: str = "127.0.0.1", port: int = 8770, token: str = "") -> None:
    """启动接收服务，阻塞运行直到 Ctrl-C。"""
    inbox = Path(inbox_dir)
    server = ThreadingHTTPServer((host, port), _make_handler(inbox, token))
    logger.info("采集回调服务已启动：http://%s:%d/api/ingest?platform=<平台key>", host, port)
    if not token:
        logger.warning("未配置 token，接口无鉴权 —— 生产环境请在 config.yaml 设置 ingest.http.token")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在关闭…")
    finally:
        server.shutdown()
        server.server_close()
