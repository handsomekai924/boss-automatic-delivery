"""职位库：列表 / 详情 / 删除 / 统计 / 补抓描述。

`fetch-descriptions` 必须注册在 `/{encrypt_job_id}` 之前，免得被动态路由吃掉。
列表 / 统计 / 清空都按路径里的 `{site}` 切片（`jobs.site`），别家站点的职位
不串进来——这是跨站点职位库视图的底座。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from boss_jobs.store import JobStore

from ..errors import NotFoundError, ValidationWebError
from ..services.desc_task import desc_tasks

router = APIRouter()


class DeleteBody(BaseModel):
    """批量删除职位。"""

    ids: list[str] = Field(default_factory=list)


class ClearBody(BaseModel):
    """清空职位库。"""

    confirm: bool = False
    city: str | None = None
    keyword: str | None = None


class FetchDescBody(BaseModel):
    """补抓 JD 参数。

    Attributes:
        limit: 本次最多补几条；0 = 全部没描述的。
        interval: 条间隔（秒）。0/不传 = 服务端默认 `DETAIL_INTERVAL`（1s），防风控。
    """

    limit: int = Field(0, ge=0, le=5000)
    interval: float = Field(0.0, ge=0.0, le=30.0)


@router.get("")
def list_jobs(
    request: Request,
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    city: str | None = None,
    keyword: str | None = None,
    category: str | None = None,
    jd_status: str | None = None,
) -> dict[str, Any]:
    """分页列出职位库；只认路径里那个站点的职位（跨站点视图的切片口）。"""
    site = str(request.state.site)
    with JobStore() as store:
        items = store.list_jobs(
            limit=limit, offset=offset, city=city, keyword=keyword,
            category=category, jd_status=jd_status, site=site,
        )
        total = store.count_jobs_matching(
            city=city, keyword=keyword, category=category, jd_status=jd_status, site=site
        )
    return {
        "items": [j.to_dict() for j in items],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/stats")
def stats(request: Request) -> dict[str, Any]:
    """职位库汇总统计与最近翻页日志。"""
    site = str(request.state.site)
    with JobStore() as store:
        summary = store.summary(site=site)
        summary["missing_desc"] = store.count_jobs_missing_desc()
        pages_log = []
        for row in store.list_pages(limit=20, site=site):
            item = dict(row)
            item["inserted"] = item.get("inserted_count", 0)
            item["updated"] = item.get("updated_count", 0)
            pages_log.append(item)
        summary["pages_log"] = pages_log
    return summary


@router.post("/fetch-descriptions")
def fetch_descriptions(body: FetchDescBody | None = None) -> dict[str, Any]:
    """对还没抓到描述的职位批量补 JD（后台任务）。

    名单 = 一次没抓过的 + 抓过但 `jd_status = 'failed'` 的；回过空 JD 的不再撞。
    """
    limit = (body.limit if body else 0) or 0
    interval = (body.interval if body else 0.0) or None
    task = desc_tasks.start(limit=limit, interval=interval)
    return task.snapshot()


@router.get("/fetch-descriptions/status")
def fetch_descriptions_status() -> dict[str, Any]:
    """补抓 JD 任务快照。"""
    return desc_tasks.snapshot()


@router.post("/fetch-descriptions/cancel")
def fetch_descriptions_cancel() -> dict[str, Any]:
    """取消补抓 JD 任务。"""
    task = desc_tasks.cancel()
    return task.snapshot()


@router.get("/{encrypt_job_id}")
def get_job(encrypt_job_id: str) -> dict[str, Any]:
    """按 id 取单条职位。"""
    with JobStore() as store:
        job = store.get_job(encrypt_job_id)
    if job is None:
        raise NotFoundError(f"职位不存在：{encrypt_job_id}")
    return job.to_dict()


@router.delete("/{encrypt_job_id}")
def delete_job(encrypt_job_id: str) -> dict[str, Any]:
    """按 id 删单条职位。"""
    with JobStore() as store:
        ok = store.delete_job(encrypt_job_id)
    if not ok:
        raise NotFoundError(f"职位不存在：{encrypt_job_id}")
    return {"deleted": 1}


@router.post("/delete")
def delete_many(body: DeleteBody) -> dict[str, Any]:
    """批量删除职位。"""
    if not body.ids:
        raise ValidationWebError("没有选中任何职位")
    with JobStore() as store:
        deleted = store.delete_jobs(body.ids)
    return {"deleted": deleted}


@router.post("/clear")
def clear_jobs(request: Request, body: ClearBody) -> dict[str, Any]:
    """清空职位库（需 confirm）；只清路径里那个站点的职位。"""
    if not body.confirm:
        raise ValidationWebError("需要 confirm=true 才能清空")
    with JobStore() as store:
        deleted = store.clear_jobs(
            city=body.city, keyword=body.keyword, site=str(request.state.site)
        )
    return {"deleted": deleted}
