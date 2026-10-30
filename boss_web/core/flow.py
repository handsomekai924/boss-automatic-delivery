"""编排骨架（todo3 2.3）：`run_crawl` / `run_deliver` 两个模板方法。

共享层定死流程——抓取的黑名单/入库/只改不增/节流，投递的「发对话 → 过门槛 → 投递」——
站点只填洞。洞数是契约：`run_crawl` 3 个（`to_search_params` / `iter_pages` /
`fetch_detail`），`run_deliver` 3 个（`open_session` / `send_text` / `apply`）。
`caps()` 与 `conversation_state()` 不算洞：它们是共享规则的输入（门槛判据），
站点只是**如实自述**，不能借它们夹带编排步骤。

投递门槛是共享规则（todo3 成功标准 #5），不许各站点自己写一套：
`AFTER_MUTUAL_CONVERSATION`（默认）——`conversation_state()` 不是 `REPLIED` 就**不调**
`applier.apply`；`AFTER_GREETING`——发完对话即可投。

`site_settings` 的节流/每日配额/投递开关同样是共享规则：`limits` 传进来就照着它
停/走——`throttle_ms` 由 `throttle_hook` 变成 `hooks.throttle`，`daily_quota` 到顶
整批停，`apply_enabled=0` 只发对话不投简历。熔断也在这儿：站点只管
`classify_fault` 归类，`Breaker` 照类判停（停类一发就断、退避类攒到阈值断）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from .contracts import (
    ApplyGate,
    ApplyReceipt,
    ConversationState,
    JobRef,
    ResumeRef,
    SearchCriteria,
    SendReceipt,
    SessionRef,
)
from .limits import Breaker, FaultAction, SiteLimits
from .protocols import SiteAdapter

__all__ = [
    "CrawlHooks",
    "CrawlReport",
    "DeliverHooks",
    "DeliverReport",
    "DeliverTarget",
    "GateOutcome",
    "run_crawl",
    "run_deliver",
]


# ---------------------------------------------------------------------------
# 共享侧钩子：黑名单 / 入库 / 只改不增 / 节流都由 server 注入，站点看不见
# ---------------------------------------------------------------------------


@dataclass
class CrawlHooks:
    """`run_crawl` 要的四个共享钩子（依赖注入，站点不许自己碰库）。

    Attributes:
        blocked: 黑名单判定；`True` = 这条不入库。
        needs_detail: 是否还要补抓详情（只改不增：已完成的不再抓）。
        upsert: 入库口；**只改不增**由实现方保证，骨架只负责调用。
        throttle: 条间节流；站点限速是站点属性，实现方从 `site_settings` 取。
    """

    blocked: Callable[[JobRef, Any], bool] = lambda ref, detail: False
    needs_detail: Callable[[JobRef], bool] = lambda ref: True
    upsert: Callable[[JobRef, Any], None] = lambda ref, detail: None
    throttle: Callable[[], None] = lambda: None


@dataclass(frozen=True)
class CrawlReport:
    """一次抓取的结果账。"""

    seen: int = 0
    stored: int = 0
    blocked: int = 0
    skipped: int = 0
    pages: int = 0
    stopped_reason: str = ""


@dataclass
class DeliverHooks:
    """`run_deliver` 要的共享钩子（节流 + 结果落账）。

    Attributes:
        throttle: 条间节流；从 `site_settings.throttle_ms` 取（`throttle_hook` 造）。
        cooldown: 熔断判到「退避」时多歇一会儿再试下一条。
        on_greeted: 对话发出去了。
        on_applied: 简历投出去了。
        on_skipped: 这条没发/没投，带原因（门槛没过也算）。
    """

    throttle: Callable[[], None] = lambda: None
    cooldown: Callable[[], None] = lambda: None
    on_greeted: Callable[[JobRef, SendReceipt], None] = lambda ref, receipt: None
    on_applied: Callable[[JobRef, ApplyReceipt], None] = lambda ref, receipt: None
    on_skipped: Callable[[JobRef, str], None] = lambda ref, reason: None


@dataclass(frozen=True)
class DeliverTarget:
    """一条要发对话的职位 + 招呼语正文。"""

    ref: JobRef
    greeting: str


class GateOutcome:
    """投递门槛与站点开关的去向（挂在报告里，测试与前端都认它）。"""

    NOT_APPLICABLE = "not_applicable"  # 这条根本不投（门槛路径没要求投）
    SKIPPED_BY_GATE = "skipped_by_gate"  # 门槛没过，**没调** apply
    SKIPPED_BY_SETTINGS = "skipped_by_settings"  # 站点没开投递 / 配额到顶 / 整批已停
    APPLIED = "applied"  # 过了门槛，投了


@dataclass(frozen=True)
class DeliverReport:
    """一次发对话 + 投递的结果账。

    Attributes:
        greeted: 对话发出去几条。
        applied: 简历投出去几条。
        skipped: 没发/没投几条。
        gate: 投递门槛路径（`ApplyGate` 的值）。
        outcomes: 每条的去向（`GateOutcome`）。
        stopped_reason: 整批停住的原因；没停 = 空串。
    """

    greeted: int = 0
    applied: int = 0
    skipped: int = 0
    gate: str = ""
    outcomes: Mapping[str, str] = field(default_factory=dict)
    stopped_reason: str = ""


# ---------------------------------------------------------------------------
# 模板方法
# ---------------------------------------------------------------------------


def run_crawl(
    adapter: SiteAdapter,
    criteria: SearchCriteria,
    hooks: CrawlHooks,
    *,
    max_pages: int | None = None,
) -> CrawlReport:
    """抓取骨架：拦空检索 → 翻页 → 黑名单 → 补抓详情 → 只改不增入库 → 节流。

    站点调用点恰好 3 个（todo3 成功标准 #4）：`to_search_params` / `iter_pages`
    / `fetch_detail`。翻页策略（BOSS 的 hasMore 第 1 页不可信、页间硬睡）在
    `iter_pages` 里，属于站点语义，骨架不掺和。

    Args:
        adapter: 站点门面。
        criteria: 通用检索维度。
        hooks: 共享侧钩子（黑名单/入库/节流）。
        max_pages: 最多翻几页；`None` = 站点说翻完为止。

    Returns:
        抓取账目。

    Raises:
        ValueError: 检索维度全空——抓全量没意义，拦在站点调用之前。
    """
    if criteria.is_blank():
        raise ValueError("检索维度全空，先给条件再抓（空检索会打成站点全量）")

    seen = stored = blocked = skipped = pages = 0
    stopped = ""

    # 站点调用点 1/3：通用条件 → 站点私有参数串（server 当不透明对象往下传）
    params = adapter.catalog.to_search_params(criteria)

    # 站点调用点 2/3：翻页迭代（翻页策略是站点语义）
    for page in adapter.jobs.iter_pages(params):
        pages += 1
        for item in page.items:
            seen += 1
            ref = _ref_of(adapter, item)
            if not hooks.needs_detail(ref):
                skipped += 1
                continue

            # 站点调用点 3/3：抓详情（形状站点私有）
            detail = adapter.jobs.fetch_detail(ref)

            if hooks.blocked(ref, detail):
                blocked += 1
                continue

            hooks.upsert(ref, detail)  # 只改不增由实现方保证
            stored += 1
            hooks.throttle()

        if max_pages is not None and pages >= max_pages:
            stopped = f"到达页数上限 {max_pages}"
            break
        if not page.has_more:
            break

    return CrawlReport(
        seen=seen, stored=stored, blocked=blocked,
        skipped=skipped, pages=pages, stopped_reason=stopped,
    )


def run_deliver(
    adapter: SiteAdapter,
    targets: Sequence[DeliverTarget],
    resume: ResumeRef,
    hooks: DeliverHooks,
    *,
    limits: SiteLimits | None = None,
    sent_today: int = 0,
) -> DeliverReport:
    """发对话 + 投递骨架：发对话 → 过门槛 → 投递（todo3 2.3）。

    站点调用点恰好 3 个（todo3 成功标准 #4）：`open_session` / `send_text`
    / `apply`。**投递门槛是共享规则**（成功标准 #5），不是站点私有判断：
    `caps().apply_gate` 是 `AFTER_MUTUAL_CONVERSATION` 时，`conversation_state()`
    不是 `REPLIED` 就不调 `apply`，这条写进测试。

    `limits`（`site_settings`）同样是共享规则：
    `apply_enabled=0` → 只发对话不投简历；`daily_quota` 到顶 → 整批停；
    `throttle_ms` 交给 `hooks.throttle`（见 `throttle_hook`）。熔断按
    `classify_fault` 的归类判：停类一发就断，退避类攒到 `Breaker.trip` 断。

    Args:
        adapter: 站点门面。
        targets: 要发对话的职位与正文。
        resume: 投递用简历（走站内简历 ID）。
        hooks: 共享侧钩子（节流 + 落账）。
        limits: 站点限速/配额；`None` = 不限速、不限配额、默认允许投递。
        sent_today: 今天已经发出去的条数（每日配额的账，见 `count_greeted_today`）。

    Returns:
        发送与投递的账目，含每条的去向与整批停住的原因。
    """
    gate = adapter.applier.caps().apply_gate
    apply_enabled = True if limits is None else bool(limits.apply_enabled)
    quota_left = None if limits is None else limits.quota_left(sent_today)
    breaker = Breaker()
    greeted = applied = skipped = 0
    outcomes: dict[str, str] = {}
    stopped = ""

    def abandon(rest: Sequence[DeliverTarget], reason: str) -> None:
        """剩下的这批不发了，逐条落账。"""
        nonlocal skipped
        for left in rest:
            skipped += 1
            outcomes[left.ref.job_id] = GateOutcome.SKIPPED_BY_SETTINGS
            hooks.on_skipped(left.ref, reason)

    for position, target in enumerate(targets):
        ref = target.ref

        if quota_left is not None and greeted >= quota_left:
            stopped = f"今日发对话配额已用完（{limits.daily_quota} 条），整批停住"
            abandon(targets[position:], "今日发对话配额已用完，未发送")
            break

        text = (target.greeting or "").strip()
        if not text:
            skipped += 1
            outcomes[ref.job_id] = GateOutcome.SKIPPED_BY_GATE
            hooks.on_skipped(ref, "没有招呼语正文，未发送")
            continue

        try:
            # 站点调用点 1/3：建会话（不发正文）
            session: SessionRef = adapter.messenger.open_session(ref)
            # 站点调用点 2/3：发正文；回执只表示「话发出去了」
            receipt: SendReceipt = adapter.messenger.send_text(session, text)
        except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮整批
            action = breaker.record(adapter.classify_fault(exc))
            if action == FaultAction.STOP:
                skipped += 1
                outcomes[ref.job_id] = GateOutcome.SKIPPED_BY_GATE
                hooks.on_skipped(ref, f"发对话失败：{exc}")
                stopped = breaker.reason
                abandon(targets[position + 1:], f"整批已停（{stopped}），未发送")
                break
            if action == FaultAction.COOLOFF:
                hooks.cooldown()
            skipped += 1
            outcomes[ref.job_id] = GateOutcome.SKIPPED_BY_GATE
            hooks.on_skipped(ref, f"发对话失败：{exc}")
            continue

        greeted += 1
        breaker.ok()
        hooks.on_greeted(ref, receipt)
        hooks.throttle()

        if not apply_enabled:
            # 站点开关是共享规则：门槛过没过都一样，只发对话不投简历
            skipped += 1
            outcomes[ref.job_id] = GateOutcome.SKIPPED_BY_SETTINGS
            hooks.on_skipped(ref, "站点没开投递（site_settings.apply_enabled=0），只发对话")
            continue

        # 门槛：共享规则，站点只如实回报会话阶段
        if gate == ApplyGate.AFTER_GREETING:
            pass  # 发完对话即可投
        elif adapter.messenger.conversation_state(session) is not ConversationState.REPLIED:
            # 默认路径不满足 REPLIED → **不调 applier.apply**（成功标准 #5）
            skipped += 1
            outcomes[ref.job_id] = GateOutcome.SKIPPED_BY_GATE
            hooks.on_skipped(ref, "对方还没回话，未投递（门槛：双方对话成立后才投）")
            continue

        try:
            # 站点调用点 3/3：投递简历
            applied_receipt: ApplyReceipt = adapter.applier.apply(session, resume)
        except Exception as exc:  # noqa: BLE001
            action = breaker.record(adapter.classify_fault(exc))
            if action == FaultAction.STOP:
                skipped += 1
                outcomes[ref.job_id] = GateOutcome.SKIPPED_BY_GATE
                hooks.on_skipped(ref, f"投递失败：{exc}")
                stopped = breaker.reason
                abandon(targets[position + 1:], f"整批已停（{stopped}），未发送")
                break
            if action == FaultAction.COOLOFF:
                hooks.cooldown()
            skipped += 1
            outcomes[ref.job_id] = GateOutcome.SKIPPED_BY_GATE
            hooks.on_skipped(ref, f"投递失败：{exc}")
            continue

        applied += 1
        breaker.ok()
        outcomes[ref.job_id] = GateOutcome.APPLIED
        hooks.on_applied(ref, applied_receipt)

    return DeliverReport(
        greeted=greeted, applied=applied, skipped=skipped,
        gate=gate.value, outcomes=outcomes, stopped_reason=stopped,
    )


def _ref_of(adapter: SiteAdapter, item: Any) -> JobRef:
    """从站点私有职位对象取引用；站点对象自带 `job_id` 一类的键，形状私有。

    抓下来的东西骨架不当它是啥，只认「能取出一个 `JobRef`」——取不出来说明
    站点返回的不是职位，当场炸比静默入库好。
    """
    if isinstance(item, JobRef):
        return item
    job_id = str(getattr(item, "job_id", "") or (item.get("job_id") if isinstance(item, Mapping) else ""))
    if not job_id:
        raise ValueError(f"站点返回的职位取不出 job_id：{item!r}")
    extra = dict(getattr(item, "extra", {}) or {})
    return JobRef(site_id=adapter.site_id, job_id=job_id, extra=extra)
