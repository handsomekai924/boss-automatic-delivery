"""职位库：列表 / 详情 / 删除 / 统计 / 补抓描述。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from boss_jobs.store import JobStore

from ..errors import NotFoundError, ValidationWebError
from ..services.desc_task import desc_tasks

router = APIRouter()


class DeleteBody(BaseModel):
    ids: list[str] = Field(default_factory=list)


class ClearBody(BaseModel):
    confirm: bool = False
    city: str | None = None
    keyword: str | None = None


class FetchDescBody(BaseModel):
    #: 最多补几条；0 = 全部没描述的
    limit: int = Field(0, ge=0, le=5000)
    #: 条间隔（秒）；0/不传 = 默认 ``DETAIL_INTERVAL``，防风控
    interval: float = Field(0.0, ge=0.0, le=30.0)


@router.get("")
def list_jobs(
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    city: str | None = None,
    keyword: str | None = None,
) -> dict[str, Any]:
    with JobStore() as store:
        items = store.list_jobs(limit=limit, offset=offset, city=city, keyword=keyword)
        total = store.count_jobs_matching(city=city, keyword=keyword)
    return {
        "items": [j.to_dict() for j in items],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/stats")
def stats() -> dict[str, Any]:
    """库摘要 + 最近翻页流水。

    ``list_pages`` 回表原始列名（``inserted_count``），流水事件用 ``inserted``；
    这里统一成事件键名，前端一套字段吃两种来源。
    """
    with JobStore() as store:
        summary = store.summary()
        summary["missing_desc"] = store.count_jobs_missing_desc()
        pages_log = []
        for row in store.list_pages(limit=20):
            item = dict(row)
            item["inserted"] = item.get("inserted_count", 0)
            item["updated"] = item.get("updated_count", 0)
            pages_log.append(item)
        summary["pages_log"] = pages_log
    return summary


@router.post("/fetch-descriptions")
def fetch_descriptions(body: FetchDescBody | None = None) -> dict[str, Any]:
    """``POST /fetch-descriptions``：对 ``detail_fetched_at = ''`` 的职位批量补 JD（后台任务）。

    注册顺序要排在 ``/{encrypt_job_id}`` 之前，否则被动态路由吃掉。
    """
    limit = (body.limit if body else 0) or 0
    interval = (body.interval if body else 0.0) or None
    task = desc_tasks.start(limit=limit, interval=interval)
    return task.snapshot()


@router.get("/fetch-descriptions/status")
def fetch_descriptions_status() -> dict[str, Any]:
    return desc_tasks.snapshot()


@router.post("/fetch-descriptions/cancel")
def fetch_descriptions_cancel() -> dict[str, Any]:
    task = desc_tasks.cancel()
    return task.snapshot()


@router.get("/{encrypt_job_id}")
def get_job(encrypt_job_id: str) -> dict[str, Any]:
    with JobStore() as store:
        job = store.get_job(encrypt_job_id)
    if job is None:
        raise NotFoundError(f"职位不存在：{encrypt_job_id}")
    return job.to_dict()


@router.delete("/{encrypt_job_id}")
def delete_job(encrypt_job_id: str) -> dict[str, Any]:
    with JobStore() as store:
        ok = store.delete_job(encrypt_job_id)
    if not ok:
        raise NotFoundError(f"职位不存在：{encrypt_job_id}")
    return {"deleted": 1}


@router.post("/delete")
def delete_many(body: DeleteBody) -> dict[str, Any]:
    if not body.ids:
        raise ValidationWebError("没有选中任何职位")
    with JobStore() as store:
        deleted = store.delete_jobs(body.ids)
    return {"deleted": deleted}


@router.post("/clear")
def clear_jobs(body: ClearBody) -> dict[str, Any]:
    if not body.confirm:
        raise ValidationWebError("需要 confirm=true 才能清空")
    with JobStore() as store:
        deleted = store.clear_jobs(city=body.city, keyword=body.keyword)
    return {"deleted": deleted}
