"""平台设置：每站点的节流/每日配额/并发/投递开关（`site_settings`）。

限速是**站点属性**，所以这条路由是平台作用域的：`/api/sites/{site}/settings`
（legacy 别名 `/api/settings` 归 `site="boss"`）。前端 `#/:siteId/settings`
页读写的就是它；任务中心「今天还能发几条」也从这儿出（`quota_left`），
口径见 `boss_web.services.site_settings_store`。

`apply_enabled` 管的是**门槛过了之后要不要真投简历**，不是要不要发对话——
发对话照发，投递由 `run_deliver` 按这个开关收口。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ..services.site_settings_store import (
    limits_payload,
    load_site_limits,
    save_site_limits,
)

router = APIRouter()


class SettingsBody(BaseModel):
    """平台限速/配额；**不传的字段保持原值**（跟 `PUT /deliver/config` 同规矩）。

    Attributes:
        throttle_ms: 条间隔（毫秒）；`0` = 用站点默认。
        daily_quota: 每日发对话配额；`0` = 不限。
        max_parallel: 并发上限。
        apply_enabled: 门槛过了之后要不要真投简历。
    """

    throttle_ms: int | None = Field(None, ge=0)
    daily_quota: int | None = Field(None, ge=0)
    max_parallel: int | None = Field(None, ge=1)
    apply_enabled: bool | None = None


@router.get("")
@router.get("/")
def get_settings(request: Request) -> dict[str, Any]:
    """读该站点的限速/配额 + 今日余量。

    Returns:
        `{site, throttle_ms, daily_quota, max_parallel, apply_enabled,
        sent_today, quota_left}`；`quota_left` 为 `null` = 不限。
    """
    limits, sent_today = load_site_limits(str(request.state.site))
    return limits_payload(limits, sent_today)


@router.put("")
@router.put("/")
def put_settings(request: Request, body: SettingsBody) -> dict[str, Any]:
    """改该站点的限速/配额；越界直接 422，坏值不落库。

    Args:
        body: 要改的字段；`None` 的保持原值。

    Returns:
        写完之后的设置 + 今日余量（形状同 `GET`）。
    """
    limits, sent_today = save_site_limits(
        str(request.state.site),
        throttle_ms=body.throttle_ms,
        daily_quota=body.daily_quota,
        max_parallel=body.max_parallel,
        apply_enabled=body.apply_enabled,
    )
    return limits_payload(limits, sent_today)
