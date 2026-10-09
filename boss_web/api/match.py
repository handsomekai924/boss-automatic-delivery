"""匹配舱：启动/轮询匹配任务、改招呼语、看历史分析。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..errors import NotFoundError, ValidationWebError
from ..services.match_task import match_tasks, regenerate_greeting
from ..services.resume_store import (
    delete_analysis,
    list_analyses,
    load_analysis,
    update_greeting,
)

router = APIRouter()


class MatchBody(BaseModel):
    resume_id: str
    #: 空 = 全库全部岗位；非空 = 只对勾选的 job_id 跑
    job_ids: list[str] = Field(default_factory=list)


class GreetingBody(BaseModel):
    encrypt_job_id: str
    greeting: str = Field("", max_length=500)


class RegenGreetingBody(BaseModel):
    encrypt_job_id: str


@router.post("/analyze")
def start_match(body: MatchBody) -> dict[str, Any]:
    task = match_tasks.start(resume_id=body.resume_id, job_ids=body.job_ids)
    return task.snapshot()


@router.get("/analyze/status")
def match_status() -> dict[str, Any]:
    return match_tasks.snapshot()


@router.get("/analyze/{task_id}")
def match_task(task_id: str) -> dict[str, Any]:
    return match_tasks.snapshot(task_id)


@router.post("/analyze/{task_id}/cancel")
def cancel_match(task_id: str) -> dict[str, Any]:
    task = match_tasks.cancel(task_id)
    return task.snapshot()


@router.patch("/{analysis_id}/greeting")
def patch_greeting(analysis_id: str, body: GreetingBody) -> dict[str, Any]:
    """只改 payload 里那一条 match 的招呼语。"""
    try:
        item = update_greeting(analysis_id, body.encrypt_job_id, body.greeting)
    except FileNotFoundError as exc:
        raise NotFoundError(f"分析结果不存在：{analysis_id}") from exc
    except KeyError as exc:
        raise NotFoundError(f"分析里没有这个职位：{body.encrypt_job_id}") from exc
    return {"ok": True, "item": item}


@router.post("/{analysis_id}/greeting/regenerate")
def regen_greeting(analysis_id: str, body: RegenGreetingBody) -> dict[str, Any]:
    """重打一条招呼语草稿：**只回新文案，不落库**（用户可能反复生成再挑一条保存）。"""
    try:
        greeting = regenerate_greeting(
            analysis_id=analysis_id, encrypt_job_id=body.encrypt_job_id
        )
    except FileNotFoundError as exc:
        raise NotFoundError(f"分析结果不存在：{analysis_id}") from exc
    except KeyError as exc:
        raise NotFoundError(f"分析里没有这个职位：{body.encrypt_job_id}") from exc
    return {"ok": True, "greeting": greeting}


@router.get("/analyses")
def analyses() -> dict[str, Any]:
    return {"items": list_analyses()}


@router.get("/analyses/{analysis_id}")
def get_analysis(analysis_id: str) -> dict[str, Any]:
    try:
        return load_analysis(analysis_id)
    except FileNotFoundError as exc:
        raise NotFoundError(f"分析结果不存在：{analysis_id}") from exc


@router.delete("/analyses/{analysis_id}")
def remove_analysis(analysis_id: str) -> dict[str, Any]:
    if not delete_analysis(analysis_id):
        raise NotFoundError(f"分析结果不存在：{analysis_id}")
    return {"deleted": True}
