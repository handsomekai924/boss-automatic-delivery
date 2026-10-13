"""Collection configuration and blacklist rule APIs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from ..errors import ConflictError, NotFoundError, UpstreamError, ValidationWebError
from ..services import blacklist
from ..services.collection_config_store import (
    CollectionConfig,
    load_config,
    reset_config,
    save_config,
)
from ..services.blacklist_recheck import recheck_tasks
from .filters import SearchFilterBody, _filter_payload
from boss_filter.search import JobSearchFilter

router = APIRouter()


class CollectionBody(BaseModel):
    query: str = ""
    city: str = ""
    jobType: str = ""
    salary: str = ""
    experience: list[str] = Field(default_factory=list)
    degree: list[str] = Field(default_factory=list)
    industry: list[str] = Field(default_factory=list)
    scale: list[str] = Field(default_factory=list)
    payType: list[str] = Field(default_factory=list)
    partTime: list[str] = Field(default_factory=list)
    stage: list[str] = Field(default_factory=list)
    pageSize: int = Field(15, ge=1, le=50)
    max_pages: int = Field(5, ge=1, le=200)
    start_page: int = Field(1, ge=1)
    page_interval: float = Field(1.0, ge=0, le=30)
    #: 兼容字段，恒为 True。旧 false = 已移除的「推荐页」special/zone。
    use_search: bool = True
    fetch_details: bool = True
    detail_interval: float = Field(1.0, ge=0, le=30)
    store_blacklisted: bool = True
    # 期望流（空 = 不启用，走搜索流）
    expect_kind: str = ""
    encrypt_expect_id: str = ""
    mix_expect_type: str = ""
    expect_info: str = ""
    #: 仅展示（jobs 页摘要用），不进请求参数
    expect_name: str = ""


class RuleBody(BaseModel):
    rule_type: str
    pattern: str
    match_mode: str
    scope: str = ""
    description: str = ""
    enabled: bool = True


class RuleTestBody(RuleBody):
    job_name: str = ""
    job_desc: str = ""
    brand_name: str = ""


class ToggleBody(BaseModel):
    enabled: bool


class RecheckBody(BaseModel):
    job_ids: list[str] = Field(default_factory=list)
    limit: int | None = Field(None, ge=1, le=5000)


@router.get("/collection/config")
def get_collection_config() -> dict[str, Any]:
    from boss_filter import load_search_filter

    return {"search": _filter_payload(load_search_filter()), "collection": load_config().to_dict()}


@router.get("/collection/expects")
def get_collection_expects() -> dict[str, Any]:
    """在线拉当前账号的求职期望列表，给前端下拉用。

    期望 id 是**账号维度**的，不能写死在配置里，必须在线拉
    （``GET /wapi/zpgeek/pc/recommend/expect/list.json``）。响应分三组，
    与站点三个 tab 对应：``mix``（推荐）、``partTime``（兼职）、``expects``（我的期望）。
    """
    from boss_jobs import create_client
    from boss_jobs.errors import JobError

    client = create_client()
    try:
        expects = client.fetch_expect_list()
    except JobError as exc:
        raise UpstreamError(f"拉求职期望列表失败：{exc}") from exc

    def _row(item: Any) -> dict[str, Any]:
        return {
            "kind": item.kind,
            "encryptExpectId": item.encrypt_expect_id,
            "positionName": item.position_name,
            "locationName": item.location_name,
            "tagName": item.tag_name,
            "mixExpectType": item.mix_expect_type,
            "label": item.label,
        }

    return {
        "mix": _row(expects.mix) if expects.mix else None,
        "expects": [_row(item) for item in expects.expects],
        "partTime": [_row(item) for item in expects.part_time],
    }


@router.put("/collection/config")
def put_collection_config(body: CollectionBody) -> dict[str, Any]:
    from boss_filter import save_search_filter

    data = body.model_dump()
    search = JobSearchFilter(
        query=data.pop("query"), city=data.pop("city"), job_type=data.pop("jobType"),
        salary=data.pop("salary"), experience=tuple(data.pop("experience")),
        degree=tuple(data.pop("degree")), industry=tuple(data.pop("industry")),
        scale=tuple(data.pop("scale")), pay_type=tuple(data.pop("payType")),
        part_time=tuple(data.pop("partTime")), stage=tuple(data.pop("stage")),
        # 期望 + 关键词：前端 onSearch 就把 lastExpectId 塞进搜索请求，
        # 所以 encrypt_expect_id 期望流与搜索流**共用同一份**（不 pop，下面
        # CollectionConfig 还要同值的那一份）
        encrypt_expect_id=str(data.get("encrypt_expect_id") or "").strip(),
        page_size=int(data.pop("pageSize", 15) or 15),
    )
    save_search_filter(search)
    cfg = save_config(CollectionConfig(**data))
    return {"search": _filter_payload(search), "collection": cfg.to_dict(), "saved": True}


@router.post("/collection/config/reset")
def reset_collection_config() -> dict[str, Any]:
    from boss_filter import save_search_filter

    f = JobSearchFilter()
    save_search_filter(f)
    return {"search": _filter_payload(f), "collection": reset_config().to_dict(), "saved": True}


@router.post("/blacklist/recheck")
def start_recheck(body: RecheckBody) -> dict[str, Any]:
    try:
        task = recheck_tasks.start(job_ids=body.job_ids, limit=body.limit)
    except ConflictError:
        raise
    return task.snapshot()


@router.get("/blacklist/recheck/status")
def recheck_status() -> dict[str, Any]:
    return recheck_tasks.snapshot()


@router.post("/blacklist/recheck/cancel")
def cancel_recheck() -> dict[str, Any]:
    try:
        return recheck_tasks.cancel().snapshot()
    except NotFoundError:
        raise


@router.get("/blacklist/rules")
def get_rules(rule_type: str | None = Query(None, alias="type")) -> dict[str, Any]:
    if rule_type and rule_type not in {blacklist.COMPANY, blacklist.JD_KEYWORD}:
        raise ValidationWebError("type 必须是 company 或 jd_keyword")
    return {"items": blacklist.list_rules(rule_type=rule_type)}


@router.post("/blacklist/rules")
def post_rule(body: RuleBody) -> dict[str, Any]:
    try:
        return {"item": blacklist.create_rule(body.model_dump())}
    except ValueError as exc:
        raise ValidationWebError(str(exc)) from exc


@router.put("/blacklist/rules/{rule_id}")
def put_rule(rule_id: str, body: RuleBody) -> dict[str, Any]:
    try:
        return {"item": blacklist.update_rule(rule_id, body.model_dump())}
    except KeyError as exc:
        raise NotFoundError(f"规则不存在：{rule_id}") from exc
    except ValueError as exc:
        raise ValidationWebError(str(exc)) from exc


@router.post("/blacklist/rules/{rule_id}/toggle")
def toggle_rule(rule_id: str, body: ToggleBody) -> dict[str, Any]:
    try:
        return {"item": blacklist.set_rule_enabled(rule_id, body.enabled)}
    except KeyError as exc:
        raise NotFoundError(f"规则不存在：{rule_id}") from exc
    except ValueError as exc:
        raise ValidationWebError(str(exc)) from exc


@router.delete("/blacklist/rules/{rule_id}")
def remove_rule(rule_id: str) -> dict[str, Any]:
    if not blacklist.delete_rule(rule_id):
        raise NotFoundError(f"规则不存在：{rule_id}")
    return {"deleted": True}


@router.post("/blacklist/test")
def test_rule(body: RuleTestBody) -> dict[str, Any]:
    try:
        blacklist.validate_rule_payload(body.rule_type, body.pattern, body.match_mode, body.scope)
        rule = {**body.model_dump(), "rule_id": "test", "enabled": True}
        if body.rule_type == blacklist.COMPANY:
            hits = blacklist.evaluate_company(body.brand_name, [rule])
        else:
            hits = blacklist.evaluate_jd(body.job_name, body.job_desc, [rule])
        return {"matched": bool(hits), "matches": [hit.to_dict() for hit in hits]}
    except ValueError as exc:
        raise ValidationWebError(str(exc)) from exc


@router.get("/blacklist/rules/{rule_id}/hits")
def get_rule_hits(rule_id: str, limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    if blacklist.get_rule(rule_id) is None:
        raise NotFoundError(f"规则不存在：{rule_id}")
    return {"items": blacklist.list_hits(rule_id, limit=limit)}


@router.get("/blacklist/hits")
def get_hits(limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    return {"items": blacklist.list_hits(limit=limit)}
