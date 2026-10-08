"""职位库：列表 / 详情 / 删除 / 统计。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from boss_jobs.store import JobStore

from ..errors import NotFoundError, ValidationWebError

router = APIRouter()


class DeleteBody(BaseModel):
    ids: list[str] = Field(default_factory=list)


class ClearBody(BaseModel):
    confirm: bool = False
    city: str | None = None
    keyword: str | None = None


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
    with JobStore() as store:
        summary = store.summary()
        # list_pages 回的是表原始列名（inserted_count），流水事件用的是 inserted
        # ——这里统一成事件的键名，前端一套字段吃两种来源。
        pages_log = []
        for row in store.list_pages(limit=20):
            item = dict(row)
            item["inserted"] = item.get("inserted_count", 0)
            item["updated"] = item.get("updated_count", 0)
            pages_log.append(item)
        summary["pages_log"] = pages_log
    return summary


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
