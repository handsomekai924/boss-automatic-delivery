"""`site_settings` 的服务层口：读成共享层的 `SiteLimits`，顺手算今日余量。

`boss_db` 只管存取，`boss_web.core.limits` 只管形状与判据，这里负责把两者接上：
读不到就退回「没配」（读失败不该把发送整批拖死），并把「今天还能发几条」算好——
任务快照与平台设置接口共用这一个口径，免得两处各算各的。

`sent_today` 数的是**发对话条数**（站点限的本来就是沟通次数），账记在
`jobs.last_greeted_at`，见 `boss_db.count_greeted_today`。
"""

from __future__ import annotations

import logging
from typing import Any

import boss_db

from ..core.limits import SiteLimits

logger = logging.getLogger(__name__)

__all__ = ["limits_payload", "load_site_limits", "save_site_limits"]


def load_site_limits(site: str) -> tuple[SiteLimits, int]:
    """读一站点的限速/配额与今天的已发数；读不到就退回「没配」。

    Args:
        site: 站点 id。

    Returns:
        `(该站点的限速/配额, 今天已发出的条数)`。
    """
    try:
        row = boss_db.get_site_settings(site)
        return SiteLimits.from_mapping(row), boss_db.count_greeted_today(site)
    except Exception:  # noqa: BLE001 - 读不到限速不挡发送
        logger.warning("读不到 %s 的 site_settings，按没配处理", site, exc_info=True)
        return SiteLimits(site=site), 0


def save_site_limits(
    site: str,
    *,
    throttle_ms: int | None = None,
    daily_quota: int | None = None,
    max_parallel: int | None = None,
    apply_enabled: bool | None = None,
) -> tuple[SiteLimits, int]:
    """改一站点的限速/配额并重读；传 `None` 的字段保持原值。

    Args:
        site: 站点 id。
        throttle_ms: 条间隔（毫秒）。
        daily_quota: 每日发对话配额。
        max_parallel: 并发上限。
        apply_enabled: 门槛过了之后要不要真投简历。

    Returns:
        写完之后的 `(限速/配额, 今天已发出的条数)`。
    """
    boss_db.set_site_settings(
        site,
        throttle_ms=throttle_ms,
        daily_quota=daily_quota,
        max_parallel=max_parallel,
        apply_enabled=apply_enabled,
    )
    return load_site_limits(site)


def limits_payload(limits: SiteLimits, sent_today: int) -> dict[str, Any]:
    """给接口与任务快照的同一份视图：设置 + 今日余量。

    Args:
        limits: 站点限速/配额。
        sent_today: 今天已经发出去的条数。

    Returns:
        `{site, throttle_ms, daily_quota, max_parallel, apply_enabled,
        sent_today, quota_left}`；`quota_left` 为 `null` = 不限。
    """
    return {
        "site": limits.site,
        "throttle_ms": limits.throttle_ms,
        "daily_quota": limits.daily_quota,
        "max_parallel": limits.max_parallel,
        "apply_enabled": limits.apply_enabled,
        "sent_today": sent_today,
        "quota_left": limits.quota_left(sent_today),
    }
