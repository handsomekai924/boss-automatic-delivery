"""站点限速/配额与投递熔断（docs 6.5：`site_settings` 节流 + 每日配额 + 熔断）。

限速是**站点属性**、执行是**共享规则**：`SiteLimits` 只是形状，怎么停怎么走由
`run_deliver` 与投递队列照着它办，站点不许自己另写一套限速逻辑。故障分类到
动作的映射也定在这儿——站点只负责 `classify_fault` 如实归类（BOSS 的
code 36 / 开聊提醒这类语义留在站点侧），判不判停是共享层的事。

`boss_db.get_site_settings()` 是这张表的仓储口；本模块不碰 SQLite，
`SiteLimits.from_mapping()` 认它的返回值，也认任何同名字段的 Mapping。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .contracts import DeliverFault

__all__ = [
    "BREAKER_TRIP",
    "Breaker",
    "FaultAction",
    "SiteLimits",
    "fault_action",
    "throttle_hook",
]


@dataclass(frozen=True)
class SiteLimits:
    """一站点的节流/每日配额/并发/投递开关（`site_settings` 的形状）。

    Attributes:
        site: 站点 id。
        throttle_ms: 条间隔（毫秒）；`0` = 用调用方自带的站点默认。
        daily_quota: 每日发对话配额；`0` = 不限。
        max_parallel: 并发上限。
        apply_enabled: 门槛过了之后要不要真投简历。
    """

    site: str = ""
    throttle_ms: int = 0
    daily_quota: int = 0
    max_parallel: int = 1
    apply_enabled: bool = False

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any] | Any | None) -> "SiteLimits":
        """从仓储行造一份；`None` / 缺字段 / 坏值一律按默认兜底。

        Args:
            row: `boss_db.SiteSettings`、`site_settings` 的一行，或任何同名字段映射。

        Returns:
            归一化后的限速/配额。
        """
        if row is None:
            return cls()
        data = dict(row) if isinstance(row, Mapping) else {
            "site": getattr(row, "site", ""),
            "throttle_ms": getattr(row, "throttle_ms", 0),
            "daily_quota": getattr(row, "daily_quota", 0),
            "max_parallel": getattr(row, "max_parallel", 1),
            "apply_enabled": getattr(row, "apply_enabled", False),
        }
        return cls(
            site=str(data.get("site") or ""),
            throttle_ms=_nonneg(data.get("throttle_ms"), 0),
            daily_quota=_nonneg(data.get("daily_quota"), 0),
            max_parallel=max(1, _nonneg(data.get("max_parallel"), 1)),
            apply_enabled=bool(data.get("apply_enabled")),
        )

    @property
    def throttle_seconds(self) -> float:
        """条间隔（秒）；`0` = 没配，调用方用自己的站点默认。"""
        return self.throttle_ms / 1000.0

    @property
    def has_daily_quota(self) -> bool:
        """配额是不是有限（`0` = 不限）。"""
        return self.daily_quota > 0

    def quota_left(self, sent_today: int) -> int | None:
        """今天还能发几条；`None` = 不限。

        Args:
            sent_today: 今天已经发出去的条数。
        """
        if not self.has_daily_quota:
            return None
        return max(0, self.daily_quota - max(0, int(sent_today)))


class FaultAction:
    """故障分类之后的三种动作（熔断的判据，挂在队列与报告上）。"""

    STOP = "stop"  # 整批停住：账号异常 / 配额耗尽 / 登录态失效
    COOLOFF = "cooloff"  # 退避一次再试下一条：限频 / 安全网关
    CONTINUE = "continue"  # 只废这一条，后面的照发


#: 退避类故障连续多少次就整批停——退避也没用，说明不是偶发限频。
BREAKER_TRIP = 3


def fault_action(fault: DeliverFault) -> str:
    """故障类 → 动作（共享规则，站点只管分类）。

    Args:
        fault: `SiteAdapter.classify_fault` 的归类结果。

    Returns:
        `FaultAction` 之一。
    """
    if fault in (DeliverFault.RISK_CONTROL, DeliverFault.QUOTA, DeliverFault.SESSION_CLOSED):
        return FaultAction.STOP
    if fault is DeliverFault.RATE_LIMIT:
        return FaultAction.COOLOFF
    return FaultAction.CONTINUE


class Breaker:
    """投递熔断：停类一发就断，退避类攒到阈值断，成功即清零。

    Attributes:
        trip: 退避类故障连续多少次触发熔断。
    """

    def __init__(self, trip: int = BREAKER_TRIP) -> None:
        """初始化。"""
        self.trip = max(1, int(trip))
        self.streak = 0
        self.tripped = False
        self.reason = ""

    def ok(self) -> None:
        """来了一次成功，连续故障计数清零。"""
        self.streak = 0

    def record(self, fault: DeliverFault) -> str:
        """记一次故障，回这次该采取的动作。

        Args:
            fault: 故障归类。

        Returns:
            `FaultAction` 之一；触发熔断时回 `STOP`。
        """
        action = fault_action(fault)
        if action == FaultAction.STOP:
            self._trip(f"{fault.value} 类故障，整批停住")
            return FaultAction.STOP
        if action == FaultAction.COOLOFF:
            self.streak += 1
            if self.streak >= self.trip:
                self._trip(f"连续 {self.streak} 次限频类故障，整批停住")
                return FaultAction.STOP
            return FaultAction.COOLOFF
        self.streak = 0
        return FaultAction.CONTINUE

    def _trip(self, reason: str) -> None:
        self.tripped = True
        self.reason = reason


def throttle_hook(
    limits: SiteLimits | None,
    sleep: Callable[[float], None],
) -> Callable[[], None]:
    """按 `site_settings.throttle_ms` 造条间节流钩子；没配 / `0` = 不睡。

    Args:
        limits: 站点限速；`None` = 用调用方自带的默认，不由这里代睡。
        sleep: 可替换的 sleep（测试里注入记录器）。

    Returns:
        直接丢给 `CrawlHooks.throttle` / `DeliverHooks.throttle` 的可调用对象。
    """
    seconds = limits.throttle_seconds if limits is not None else 0.0
    if seconds <= 0:
        return lambda: None
    return lambda: sleep(seconds)


def _nonneg(value: Any, default: int) -> int:
    """整数列读进来夹一下：坏值当默认，负数顶到 0。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, n)
