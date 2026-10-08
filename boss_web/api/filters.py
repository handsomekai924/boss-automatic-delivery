"""筛选条件：选项表 + 库里的搜索条件读写。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from boss_filter import (
    get_filter_conditions,
    load_search_filter,
    save_search_filter,
)
from boss_filter.search import JobSearchFilter

router = APIRouter()


class SearchFilterBody(BaseModel):
    """与库里 ``doc('search_filter')`` 同款字段（空 = 不限）。"""

    query: str = ""
    city: str = ""
    jobType: str = Field("", alias="jobType")
    salary: str = ""
    experience: list[str] = Field(default_factory=list)
    degree: list[str] = Field(default_factory=list)
    industry: list[str] = Field(default_factory=list)
    scale: list[str] = Field(default_factory=list)
    payType: list[str] = Field(default_factory=list)
    partTime: list[str] = Field(default_factory=list)
    stage: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


@router.get("/conditions")
def conditions() -> dict[str, Any]:
    """选项表；``source``/``degraded`` 供前端提示「接口/离线兜底」。"""
    cond = get_filter_conditions()
    data = cond.to_dict()
    data["source"] = getattr(cond, "source", "")
    data["degraded"] = getattr(cond, "degraded", {})
    return data


@router.get("/search")
def get_search() -> dict[str, Any]:
    f = load_search_filter()
    return _filter_payload(f)


@router.put("/search")
def put_search(body: SearchFilterBody) -> dict[str, Any]:
    f = JobSearchFilter(
        query=body.query,
        city=body.city,
        job_type=body.jobType,
        salary=body.salary,
        experience=tuple(body.experience),
        degree=tuple(body.degree),
        industry=tuple(body.industry),
        scale=tuple(body.scale),
        pay_type=tuple(body.payType),
        part_time=tuple(body.partTime),
        stage=tuple(body.stage),
    )
    path = save_search_filter(f)
    return {**_filter_payload(f), "path": str(path)}


@router.post("/search/reset")
def reset_search() -> dict[str, Any]:
    f = JobSearchFilter()
    path = save_search_filter(f)
    return {**_filter_payload(f), "path": str(path)}


def _filter_payload(f: JobSearchFilter) -> dict[str, Any]:
    return {
        "query": f.query,
        "city": f.city,
        "jobType": f.job_type,
        "salary": f.salary,
        "experience": list(f.experience),
        "degree": list(f.degree),
        "industry": list(f.industry),
        "scale": list(f.scale),
        "payType": list(f.pay_type),
        "partTime": list(f.part_time),
        "stage": list(f.stage),
        "is_blank": f.is_blank,
        "summary": _summary(f),
    }


def _summary(f: JobSearchFilter) -> str:
    parts = []
    if f.query:
        parts.append(f"关键词「{f.query}」")
    if f.city:
        parts.append(f"城市 {f.city}")
    if f.salary:
        parts.append(f"薪资 {f.salary}")
    for label, values in (
        ("经验", f.experience),
        ("学历", f.degree),
        ("行业", f.industry),
        ("规模", f.scale),
    ):
        if values:
            parts.append(f"{label} {'/'.join(values)}")
    return " · ".join(parts) if parts else "不限（推荐流）"
