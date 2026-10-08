"""抓取任务：启动 / 进度 / 取消。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..services.crawl_task import crawl_tasks

router = APIRouter()


class CrawlBody(BaseModel):
    max_pages: int = Field(5, ge=1, le=200)
    interval: float = Field(1.0, ge=0.0, le=30.0)
    start_page: int = Field(1, ge=1)
    use_search: bool = True
    #: 每页入库后顺带补 JD（默认开；每页约 +4.5s）
    fetch_details: bool = True


@router.get("/status")
def status() -> dict[str, Any]:
    return crawl_tasks.snapshot()


@router.post("/start")
def start(body: CrawlBody) -> dict[str, Any]:
    task = crawl_tasks.start(
        max_pages=body.max_pages,
        interval=body.interval,
        start_page=body.start_page,
        use_search=body.use_search,
        fetch_details=body.fetch_details,
    )
    return task.snapshot()


@router.get("/{task_id}")
def task_status(task_id: str) -> dict[str, Any]:
    return crawl_tasks.snapshot(task_id)


@router.post("/{task_id}/cancel")
def cancel(task_id: str) -> dict[str, Any]:
    task = crawl_tasks.cancel(task_id)
    return task.snapshot()
