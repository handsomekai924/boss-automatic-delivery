"""抓取任务：启动 / 进度 / 取消 + 历史任务档案。

历史档案路由（`/tasks…`）必须注册在 `/{task_id}` 之前——动态段会把 `tasks`
当成 task_id 吃掉（与 `jobs.py` 里 `fetch-descriptions` 同款坑）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from boss_jobs.store import JobStore

from ..errors import NotFoundError, ValidationWebError
from ..services.crawl_task import crawl_tasks
from ..services.match_task import match_tasks

router = APIRouter()


class CrawlBody(BaseModel):
    """启动抓取任务。

    Attributes:
        use_search: 兼容字段。旧 false = 已移除的「推荐页」；现在一律走搜索流。
    """

    max_pages: int | None = Field(None, ge=1, le=200)
    interval: float | None = Field(None, ge=0.0, le=30.0)
    start_page: int | None = Field(None, ge=1)
    use_search: bool | None = None
    fetch_details: bool | None = None
    detail_interval: float | None = Field(None, ge=0.0, le=30.0)
    store_blacklisted: bool | None = None


class CrawlMatchBody(BaseModel):
    """对本批抓取职位做匹配。

    Attributes:
        resume_id: 确认弹窗里选的简历；必填。
    """

    resume_id: str


@router.get("/tasks")
def list_tasks(
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """历史任务分页列表。进行中任务置顶并附实时进度（内存快照优先）。"""
    with JobStore() as store:
        items = store.list_crawl_tasks(limit=limit, offset=offset)
        total = store.count_crawl_tasks()
        for item in items:
            item["job_count_existing"] = store.count_crawl_task_jobs(
                str(item.get("task_id") or ""), only_existing=True
            )

    live = crawl_tasks.current()
    live_id = live.task_id if live is not None else ""
    if live_id:
        snap = live.snapshot()
        for item in items:
            if str(item.get("task_id")) == live_id:
                item["status"] = snap.get("status") or item.get("status")
                item["progress"] = snap.get("progress") or {}
                item["phase"] = snap.get("phase") or ""
                item["live"] = True
                break
        else:
            items.insert(0, {
                "task_id": live_id,
                "source": "expect" if _is_expect(snap) else "search",
                "status": snap.get("status") or "running",
                "created_at": snap.get("created_at") or 0,
                "started_at": snap.get("created_at") or 0,
                "ended_at": snap.get("ended_at") or 0,
                "params": snap.get("params") or {},
                "stats": snap.get("progress") or {},
                "progress": snap.get("progress") or {},
                "phase": snap.get("phase") or "",
                "job_count": 0,
                "job_count_existing": 0,
                "inserted_count": 0,
                "updated_count": 0,
                "error": "",
                "stopped_reason": "",
                "live": True,
            })
            total += 1
    for item in items:
        item.setdefault("live", False)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/tasks/{task_id}")
def get_task(task_id: str) -> dict[str, Any]:
    """按 id 取历史抓取任务；进行中可从内存快照兜底。"""
    with JobStore() as store:
        item = store.get_crawl_task(task_id)
        if item is None:
            live = crawl_tasks.current()
            if live is not None and live.task_id == task_id:
                snap = live.snapshot()
                return {
                    "task_id": task_id,
                    "source": "expect" if _is_expect(snap) else "search",
                    "status": snap.get("status") or "running",
                    "created_at": snap.get("created_at") or 0,
                    "started_at": snap.get("created_at") or 0,
                    "ended_at": snap.get("ended_at") or 0,
                    "params": snap.get("params") or {},
                    "stats": snap.get("progress") or {},
                    "progress": snap.get("progress") or {},
                    "job_count": 0,
                    "inserted_count": 0,
                    "updated_count": 0,
                    "error": "",
                    "stopped_reason": "",
                    "live": True,
                }
            raise NotFoundError(f"抓取任务不存在：{task_id}")
        item["job_count_existing"] = store.count_crawl_task_jobs(task_id, only_existing=True)
        live = crawl_tasks.current()
        if live is not None and live.task_id == task_id:
            snap = live.snapshot()
            item["status"] = snap.get("status") or item.get("status")
            item["progress"] = snap.get("progress") or {}
            item["stats"] = snap.get("progress") or item.get("stats") or {}
            item["phase"] = snap.get("phase") or ""
            item["live"] = True
        else:
            item.setdefault("live", False)
    return item


@router.get("/tasks/{task_id}/jobs")
def list_task_jobs(
    task_id: str,
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """本批职位分页。`missing=true` 的行是已从库里删掉的归属记录。"""
    with JobStore() as store:
        if store.get_crawl_task(task_id) is None:
            live = crawl_tasks.current()
            if live is None or live.task_id != task_id:
                raise NotFoundError(f"抓取任务不存在：{task_id}")
        items = store.list_crawl_task_jobs(task_id, limit=limit, offset=offset)
        total = store.count_crawl_task_jobs(task_id, only_existing=False)
        existing = store.count_crawl_task_jobs(task_id, only_existing=True)
    return {
        "items": items,
        "total": total,
        "existing": existing,
        "limit": limit,
        "offset": offset,
    }


@router.post("/tasks/{task_id}/match")
def match_task_batch(task_id: str, body: CrawlMatchBody) -> dict[str, Any]:
    """糖：等价于 `analyze` + `crawl_task_id`（前端可少拼一层）。

    本批职位与库中仍存在的取交集；交集为空 → 400「本批职位已不在库中」。
    """
    if not str(body.resume_id or "").strip():
        raise ValidationWebError("请先选择一份简历")
    with JobStore() as store:
        if store.get_crawl_task(task_id) is None:
            live = crawl_tasks.current()
            if live is None or live.task_id != task_id:
                raise NotFoundError(f"抓取任务不存在：{task_id}")
        existing = store.count_crawl_task_jobs(task_id, only_existing=True)
    if existing <= 0:
        raise ValidationWebError("本批职位已不在库中")
    task = match_tasks.start(resume_id=body.resume_id, crawl_task_id=task_id)
    return task.snapshot()


@router.get("/status")
def status() -> dict[str, Any]:
    """当前进行中抓取任务快照。"""
    return crawl_tasks.snapshot()


@router.post("/start")
def start(body: CrawlBody) -> dict[str, Any]:
    """启动抓取任务。"""
    task = crawl_tasks.start(
        max_pages=body.max_pages,
        interval=body.interval,
        start_page=body.start_page,
        use_search=body.use_search,
        fetch_details=body.fetch_details,
        detail_interval=body.detail_interval,
        store_blacklisted=body.store_blacklisted,
    )
    return task.snapshot()


@router.get("/{task_id}")
def task_status(task_id: str) -> dict[str, Any]:
    """按 id 取抓取任务快照。"""
    return crawl_tasks.snapshot(task_id)


@router.post("/{task_id}/cancel")
def cancel(task_id: str) -> dict[str, Any]:
    """取消抓取任务。"""
    task = crawl_tasks.cancel(task_id)
    return task.snapshot()


def _is_expect(snap: dict[str, Any]) -> bool:
    """从任务快照判断来源：期望流 / 搜索流。"""
    params = snap.get("params") or {}
    cfg = params.get("config_snapshot") or {}
    collection = cfg.get("collection") or {}
    return bool(str(collection.get("expect_kind") or "").strip())
