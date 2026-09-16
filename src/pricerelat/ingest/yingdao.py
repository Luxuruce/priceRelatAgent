"""对接方式 C：影刀 RPA OpenAPI 主动触发。

流程：拿 token → 启动任务 → 轮询状态 → 任务完成后从 inbox 取数据。

重要：影刀 OpenAPI **没有主动回调**，任务完成不会推送给你，只能轮询查状态。
所以「拿到数据」这一步仍需机器人自己落盘（方式 A）或 POST 回来（方式 B）——
本模块负责的是「触发 + 等待完成」，不负责传输数据本身。

凭证优先从环境变量读，避免写进配置文件被提交：
    YINGDAO_ACCESS_KEY_ID / YINGDAO_ACCESS_KEY_SECRET
"""

from __future__ import annotations

import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.yingdao.com"


class YingdaoError(RuntimeError):
    pass


class YingdaoClient:
    def __init__(
        self,
        access_key_id: str = "",
        access_key_secret: str = "",
        base_url: str = DEFAULT_BASE_URL,
        timeout: int = 30,
    ):
        self.key_id = access_key_id or os.getenv("YINGDAO_ACCESS_KEY_ID", "")
        self.key_secret = access_key_secret or os.getenv("YINGDAO_ACCESS_KEY_SECRET", "")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._token: str | None = None

        if not (self.key_id and self.key_secret):
            raise YingdaoError(
                "缺少影刀凭证：请设置环境变量 YINGDAO_ACCESS_KEY_ID / "
                "YINGDAO_ACCESS_KEY_SECRET，或在 config.yaml 的 ingest.yingdao 下填写"
            )

    def _post(self, path: str, payload: dict, with_auth: bool = True) -> dict:
        headers = {"Content-Type": "application/json"}
        if with_auth:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            resp = requests.post(
                f"{self.base_url}{path}", json=payload, headers=headers, timeout=self.timeout
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            raise YingdaoError(f"影刀接口调用失败 {path}: {e}") from e
        except ValueError as e:
            raise YingdaoError(f"影刀接口返回非 JSON {path}: {e}") from e

        # 影刀成功响应约定 code == 0（与 lark-cli 的 ok 字段不同，勿混用）
        if data.get("code") not in (0, "0", None):
            raise YingdaoError(f"影刀接口报错 {path}: {data.get('msg') or data}")
        return data.get("data") or {}

    @property
    def token(self) -> str:
        """获取 accessToken。有效期内复用，过期自动重取。"""
        if self._token:
            return self._token
        data = self._post(
            "/oapi/token/v2/token/create",
            {"accessKeyId": self.key_id, "accessKeySecret": self.key_secret},
            with_auth=False,
        )
        token = data.get("accessToken")
        if not token:
            raise YingdaoError(f"未能获取 accessToken：{data}")
        self._token = token
        return token

    def start_task(
        self, robot_uuid: str, schedule_uuid: str = "", params: dict | None = None
    ) -> str:
        """启动机器人任务，返回 taskId。"""
        payload: dict = {"robotUuid": robot_uuid}
        if schedule_uuid:
            payload["scheduleUuid"] = schedule_uuid
        if params:
            payload["params"] = params

        data = self._post("/oapi/dispatch/v2/task/start", payload)
        task_id = data.get("taskId") or data.get("taskUuid")
        if not task_id:
            raise YingdaoError(f"启动任务未返回 taskId：{data}")
        logger.info("影刀任务已启动：taskId=%s", task_id)
        return task_id

    def query_task(self, task_id: str) -> dict:
        return self._post("/oapi/dispatch/v2/task/query", {"taskId": task_id})

    def wait_task(
        self, task_id: str, poll_interval: int = 10, timeout: int = 1800
    ) -> dict:
        """轮询直到任务结束。

        影刀没有回调，只能轮询 —— 这是接口本身的限制，不是实现取巧。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            info = self.query_task(task_id)
            status = str(info.get("status", "")).lower()
            logger.info("影刀任务 %s 状态：%s", task_id, status or "未知")

            if status in ("success", "finished", "completed", "2"):
                return info
            if status in ("failed", "error", "cancelled", "canceled", "3"):
                raise YingdaoError(f"影刀任务失败：{info}")

            time.sleep(poll_interval)

        raise YingdaoError(f"影刀任务 {task_id} 等待超时（{timeout}s）")

    def run(
        self,
        robot_uuid: str,
        schedule_uuid: str = "",
        params: dict | None = None,
        poll_interval: int = 10,
        timeout: int = 1800,
    ) -> dict:
        """触发任务并等待完成。"""
        task_id = self.start_task(robot_uuid, schedule_uuid, params)
        return self.wait_task(task_id, poll_interval, timeout)
