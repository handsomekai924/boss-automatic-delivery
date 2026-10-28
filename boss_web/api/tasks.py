"""任务中心 API：统一列表 / 取消 / 重试 / 删除 + 统一 WebSocket 推送。

见 `docs/task-center-and-unified-ws.md`。所有任务进度、登录喂料、会话变化、
自更新进度都从 `/api/ws` 一条连接走；HTTP status 端点保留作降级兜底。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from ..services import task_runtime as TR
from ..services.task_runtime import hub

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/tasks")
def list_tasks(
    task_type: str = Query("", description="按类型过滤"),
    status: str = Query("", description="running / finished / 具体状态；空=全部"),
) -> dict[str, Any]:
    """任务中心列表。"""
    return hub.list_snapshots(task_type=task_type, status=status)


@router.get("/tasks/{task_id}")
def get_task(task_id: str) -> dict[str, Any]:
    """单任务规范化快照。"""
    return hub.snapshot(task_id)


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: str) -> dict[str, Any]:
    """请求取消一个任务。"""
    return hub.cancel(task_id)


@router.post("/tasks/{task_id}/retry")
def retry_task(task_id: str) -> dict[str, Any]:
    """按原参数重跑一条已结束任务。"""
    return hub.retry(task_id)


@router.delete("/tasks/{task_id}")
def delete_task(task_id: str) -> dict[str, Any]:
    """删除一条已结束任务记录。"""
    return hub.delete(task_id)


@router.post("/tasks/cancel-all")
def cancel_all() -> dict[str, Any]:
    """取消所有未结束任务。"""
    n = hub.cancel_all()
    return {"cancelled": n}


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    """统一推送通道：连接给一帧 hello 快照，之后只出不进。

    客户端可发 `{"type":"ping"}`（回 `pong`）与 `{"type":"resync"}`（重推快照）。
    """
    await ws.accept()
    loop = asyncio.get_running_loop()
    queue = hub.subscribe(loop)
    consumer: asyncio.Task | None = None
    end = "未知原因"
    try:
        await _send(ws, {
            "type": "hello",
            "ts": _now(),
            "task_type": "",
            "task_id": "",
            "data": {
                "tasks": hub.hello_payload(),
                "session": _session_payload(),
                "update": _update_payload(),
                "version": _version_payload(),
            },
        })
        consumer = asyncio.create_task(_consume(ws))
        while True:
            get = asyncio.ensure_future(queue.get())
            done, _ = await asyncio.wait({get, consumer}, return_when=asyncio.FIRST_COMPLETED)
            if consumer in done:
                get.cancel()
                end = "收消息协程结束"
                break
            msg = get.result()
            await _send(ws, msg)
    except WebSocketDisconnect:
        end = "客户端断开"
    except Exception:  # noqa: BLE001 - 连接断得难看也不能把服务带崩
        end = "服务端异常"
        logger.warning("统一 WS 异常退出", exc_info=True)
    finally:
        if consumer is not None:
            consumer.cancel()
            with contextlib.suppress(BaseException):
                await consumer
        hub.unsubscribe(queue)
        logger.info("统一 WS 连接结束（%s）", end)


async def _consume(ws: WebSocket) -> None:
    """收客户端消息：ping / resync；连接断了就正常收摊。"""
    while True:
        try:
            raw = await ws.receive_text()
        except WebSocketDisconnect:
            return
        except Exception:  # noqa: BLE001 - 收消息出错只影响本连接，不许掀桌
            logger.warning("统一 WS 收消息失败，本连接收摊", exc_info=True)
            return
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            continue
        kind = str(data.get("type") or "")
        if kind == "ping":
            await _send(ws, {"type": "pong", "ts": _now(), "task_type": "", "task_id": "", "data": {}})
        elif kind == "resync":
            await _send(ws, {
                "type": "task.snapshot",
                "ts": _now(),
                "task_type": "",
                "task_id": "",
                "data": {"tasks": hub.hello_payload()},
            })


async def _send(ws: WebSocket, payload: dict[str, Any]) -> None:
    """推一帧 JSON。

    `default=str` 兜底：业务快照里混进日期/字节等怪值时降级成字符串，
    绝不能让一帧坏载荷把整条连接炸掉、逼客户端反复重连。
    """
    await ws.send_text(json.dumps(payload, ensure_ascii=False, default=str))


def _now() -> float:
    """当前 Unix 秒。"""
    import time

    return time.time()


def _session_payload() -> dict[str, Any]:
    """登录态摘要（hello 帧附带）：与 `GET /api/auth/status` 同形状。

    前端拿到就能画信号球 / 首页会话卡，不用再回头打一次 HTTP。
    """
    try:
        from ..services.login_task import login_tasks

        return login_tasks.status_payload()
    except Exception:  # noqa: BLE001
        return {"logged_in": False, "phone_masked": "", "user": {}, "saved_at": None, "task": None}


def _update_payload() -> dict[str, Any]:
    """自更新进度（hello 帧附带）。"""
    try:
        from ..services import updater

        return updater.peek_progress()
    except Exception:  # noqa: BLE001
        return {}


def _version_payload() -> dict[str, Any]:
    """版本检测结论（hello 帧附带）：只读缓存，绝不打网络。

    断线重连靠它把「有新版」的状态补回来；真测由服务端周期线程负责，
    测到就推 `update.available`。
    """
    try:
        from ..services import update_check

        return update_check.cached_snapshot()
    except Exception:  # noqa: BLE001
        return {}
