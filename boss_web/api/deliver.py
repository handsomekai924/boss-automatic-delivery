"""投递：一键投递的评分阈值 + 打招呼发送任务。

阈值落状态库 ``doc('deliver_config')``（见 :mod:`boss_web.services.deliver_config_store`），
刷新页面 / 换机器都跟着走。

发送三种粒度（一键 / 多条 / 单条）**共用一个入口** ``POST /start``，区别只在
``encrypt_job_ids`` 的长短（见 todo.md C2）；确认弹窗在前端，后端收下已确认的清单。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..errors import ValidationWebError
from ..services.deliver_config_store import load_config, update_config
from ..services.deliver_task import deliver_tasks

router = APIRouter()


class ConfigBody(BaseModel):
    #: 0-100 分制；越界由 FastAPI 直接回 422
    min_score: int = Field(..., ge=0, le=100)


class DeliverBody(BaseModel):
    analysis_id: str
    #: 要发送的岗位：单条 = 1 个，多条 = 勾选的 N 个，一键 = 全部目标
    encrypt_job_ids: list[str] = Field(default_factory=list)


@router.get("/config")
def get_config() -> dict[str, Any]:
    return load_config().to_dict()


@router.put("/config")
def put_config(body: ConfigBody) -> dict[str, Any]:
    return update_config(body.model_dump()).to_dict()


@router.post("/start")
def start(body: DeliverBody) -> dict[str, Any]:
    if not body.encrypt_job_ids:
        raise ValidationWebError("没有要发送的岗位")
    task = deliver_tasks.start(
        analysis_id=body.analysis_id, job_ids=body.encrypt_job_ids
    )
    return task.snapshot()


@router.get("/status")
def status() -> dict[str, Any]:
    return deliver_tasks.snapshot()


@router.post("/{task_id}/cancel")
def cancel(task_id: str) -> dict[str, Any]:
    task = deliver_tasks.cancel(task_id)
    return task.snapshot()
