"""编排骨架：模板方法的洞数 + 投递门槛是共享规则（todo3 成功标准 #4/#5）。"""

from __future__ import annotations

from typing import Any

import pytest

from boss_web.core.contracts import (
    ApplyGate,
    ApplyReceipt,
    ConversationState,
    DeliveryCaps,
    DeliverFault,
    JobRef,
    PageResult,
    ResumeRef,
    SearchCriteria,
    SearchParams,
    SendReceipt,
    SessionRef,
)
from boss_web.core.flow import (
    CrawlHooks,
    DeliverHooks,
    DeliverTarget,
    GateOutcome,
    run_crawl,
    run_deliver,
)
from boss_web.core.limits import SiteLimits


class FakeJob:
    """站点私有职位对象：骨架只认「能取出 job_id」。"""

    def __init__(self, job_id: str) -> None:
        """初始化。"""
        self.job_id = job_id


class RecordingAdapter:
    """记录每一次站点调用，用来验「洞数 = 3」与「门槛没过就不调 apply」。"""

    site_id = "fake"
    display_name = "假站点"
    icon = "fake"

    def __init__(
        self,
        *,
        gate: ApplyGate = ApplyGate.AFTER_MUTUAL_CONVERSATION,
        state: ConversationState = ConversationState.REPLIED,
        apply_raises: bool = False,
        fault: DeliverFault = DeliverFault.UNKNOWN,
        send_error: Exception | None = None,
    ) -> None:
        """初始化。"""
        self.calls: list[str] = []
        self._gate = gate
        self._state = state
        self._apply_raises = apply_raises
        self._fault = fault
        self._send_error = send_error
        self.catalog = self
        self.jobs = self
        self.messenger = self
        self.applier = self

    # --- Catalog ---
    def to_search_params(self, criteria: SearchCriteria) -> SearchParams:
        self.calls.append("to_search_params")
        return {"q": criteria.keyword}

    # --- JobSource ---
    def iter_pages(self, params: SearchParams):
        self.calls.append("iter_pages")
        yield PageResult(items=(FakeJob("j1"), FakeJob("j2")), page=1, has_more=False)

    def fetch_detail(self, ref: JobRef) -> Any:
        self.calls.append("fetch_detail")
        return {"job_id": ref.job_id}

    # --- Messenger ---
    def open_session(self, ref: JobRef) -> SessionRef:
        self.calls.append("open_session")
        if self._send_error is not None:
            raise self._send_error
        return SessionRef(site_id=self.site_id, session_id=f"s-{ref.job_id}")

    def send_text(self, session: SessionRef, text: str) -> SendReceipt:
        self.calls.append("send_text")
        return SendReceipt(ok=True)

    def conversation_state(self, session: SessionRef) -> ConversationState:
        self.calls.append("conversation_state")
        return self._state

    # --- 故障归类（站点只管归类，判不判停是共享层的事） ---
    def classify_fault(self, err: Exception) -> DeliverFault:
        self.calls.append("classify_fault")
        return self._fault

    # --- Applier ---
    def caps(self) -> DeliveryCaps:
        self.calls.append("caps")
        return DeliveryCaps(apply_gate=self._gate, apply_channel="fake")

    def apply(self, session: SessionRef, resume: ResumeRef) -> ApplyReceipt:
        self.calls.append("apply")
        if self._apply_raises:
            raise RuntimeError("投递通道没接上")
        return ApplyReceipt(ok=True)


class CollectingHooks:
    """把钩子调用收下来，测试直接看账。"""

    def __init__(self) -> None:
        """初始化。"""
        self.upserted: list[str] = []
        self.greeted: list[str] = []
        self.applied: list[str] = []
        self.skipped: list[tuple[str, str]] = []
        self.throttles = 0
        self.cooldowns = 0

    def crawl(self) -> CrawlHooks:
        return CrawlHooks(
            blocked=lambda ref, detail: ref.job_id == "j2",
            needs_detail=lambda ref: ref.job_id != "skip",
            upsert=lambda ref, detail: self.upserted.append(ref.job_id),
            throttle=self._tick,
        )

    def deliver(self) -> DeliverHooks:
        return DeliverHooks(
            throttle=self._tick,
            cooldown=self._cool,
            on_greeted=lambda ref, receipt: self.greeted.append(ref.job_id),
            on_applied=lambda ref, receipt: self.applied.append(ref.job_id),
            on_skipped=lambda ref, reason: self.skipped.append((ref.job_id, reason)),
        )

    def _tick(self) -> None:
        self.throttles += 1

    def _cool(self) -> None:
        self.cooldowns += 1


CRITERIA = SearchCriteria(keyword="python")


def targets(*job_ids: str, greeting: str = "你好") -> list[DeliverTarget]:
    return [DeliverTarget(ref=JobRef(site_id="fake", job_id=j), greeting=greeting) for j in job_ids]


RESUME = ResumeRef(site_resume_id="sr-1")


# --- todo3 成功标准 #5：投递门槛是共享规则 ---


def test_默认门槛_对方没回话就不调_apply():
    """默认路径（AFTER_MUTUAL_CONVERSATION）不满足 REPLIED → **不调** applier.apply。"""
    adapter = RecordingAdapter(state=ConversationState.WAITING_REPLY)
    hooks = CollectingHooks()

    report = run_deliver(adapter, targets("j1"), RESUME, hooks.deliver())

    assert "apply" not in adapter.calls, "门槛没过却调了 applier.apply"
    assert report.greeted == 1, "对话该照发"
    assert report.applied == 0
    assert report.outcomes["j1"] == GateOutcome.SKIPPED_BY_GATE
    assert hooks.skipped and "门槛" in hooks.skipped[0][1]


def test_默认门槛_对方回话了才调_apply():
    """AFTER_MUTUAL_CONVERSATION + REPLIED → 调 applier.apply。"""
    adapter = RecordingAdapter(state=ConversationState.REPLIED)
    hooks = CollectingHooks()

    report = run_deliver(adapter, targets("j1"), RESUME, hooks.deliver())

    assert adapter.calls.count("apply") == 1
    assert report.applied == 1
    assert report.outcomes["j1"] == GateOutcome.APPLIED


def test_会话关闭也不投():
    """CLOSED 同样不是 REPLIED，也不投。"""
    adapter = RecordingAdapter(state=ConversationState.CLOSED)
    report = run_deliver(adapter, targets("j1"), RESUME, CollectingHooks().deliver())
    assert report.applied == 0
    assert "apply" not in adapter.calls


def test_after_greeting_发完对话就投_不问会话阶段():
    """AFTER_GREETING 路径发完对话才调 apply，且不探 conversation_state。"""
    adapter = RecordingAdapter(gate=ApplyGate.AFTER_GREETING, state=ConversationState.WAITING_REPLY)
    hooks = CollectingHooks()

    report = run_deliver(adapter, targets("j1"), RESUME, hooks.deliver())

    assert adapter.calls.count("apply") == 1
    assert "conversation_state" not in adapter.calls
    assert report.gate == ApplyGate.AFTER_GREETING.value
    assert report.outcomes["j1"] == GateOutcome.APPLIED


# --- todo3 成功标准 #4：模板方法洞数固定 ---


def test_run_deliver_站点调用点恰好三个():
    """deliver 的洞 = open_session / send_text / apply；caps 是共享规则的输入。"""
    adapter = RecordingAdapter(gate=ApplyGate.AFTER_GREETING)
    run_deliver(adapter, targets("j1"), RESUME, CollectingHooks().deliver())

    holes = [c for c in adapter.calls if c in {"open_session", "send_text", "apply"}]
    assert holes == ["open_session", "send_text", "apply"]
    # 门槛路径多一次会话阶段探测，但那不是洞，是共享规则的判据
    adapter2 = RecordingAdapter(state=ConversationState.REPLIED)
    run_deliver(adapter2, targets("j1"), RESUME, CollectingHooks().deliver())
    assert "conversation_state" in adapter2.calls
    assert set(adapter2.calls) <= {"caps", "open_session", "send_text", "conversation_state", "apply"}


def test_run_crawl_站点调用点恰好三个():
    """crawl 的洞 = to_search_params / iter_pages / fetch_detail。"""
    adapter = RecordingAdapter()
    hooks = CollectingHooks()

    report = run_crawl(adapter, CRITERIA, hooks.crawl())

    assert adapter.calls == ["to_search_params", "iter_pages", "fetch_detail", "fetch_detail"]
    assert report.seen == 2
    assert report.pages == 1


# --- 骨架的共享步骤 ---


def test_run_crawl_空检索拦在站点调用之前():
    """检索维度全空直接拒，一个站点方法都不许碰。"""
    adapter = RecordingAdapter()
    with pytest.raises(ValueError):
        run_crawl(adapter, SearchCriteria(), CollectingHooks().crawl())
    assert adapter.calls == []


def test_run_crawl_黑名单与只改不增():
    """黑名单的不入库；`needs_detail` 说不用补抓的直接跳过。"""
    adapter = RecordingAdapter()
    hooks = CollectingHooks()

    report = run_crawl(adapter, CRITERIA, hooks.crawl())

    assert hooks.upserted == ["j1"], "j2 进了黑名单，不该入库"
    assert report.blocked == 1
    assert report.stored == 1


def test_run_crawl_到页数上限就停():
    """max_pages 是共享侧的兜底，不该让站点翻到底。"""

    class ManyPages(RecordingAdapter):
        def iter_pages(self, params):
            self.calls.append("iter_pages")
            for i in range(1, 6):
                yield PageResult(items=(FakeJob(f"j{i}"),), page=i, has_more=True)

    adapter = ManyPages()
    hooks = CollectingHooks()
    report = run_crawl(adapter, CRITERIA, hooks.crawl(), max_pages=2)
    assert report.pages == 2
    assert report.stopped_reason == "到达页数上限 2"


def test_run_deliver_单条失败不拖垮整批():
    """发对话抛异常只废这一条，后面的照发。"""

    class OneBad(RecordingAdapter):
        def open_session(self, ref):
            self.calls.append("open_session")
            if ref.job_id == "j1":
                raise RuntimeError("会话建不起来")
            return SessionRef(site_id=self.site_id, session_id="s")

    adapter = OneBad(gate=ApplyGate.AFTER_GREETING)
    hooks = CollectingHooks()

    report = run_deliver(adapter, targets("j1", "j2"), RESUME, hooks.deliver())

    assert report.greeted == 1
    assert report.skipped == 1
    assert report.outcomes == {"j1": GateOutcome.SKIPPED_BY_GATE, "j2": GateOutcome.APPLIED}


def test_run_deliver_没招呼语正文不发():
    """空正文跳过，不浪费站点调用。"""
    adapter = RecordingAdapter(gate=ApplyGate.AFTER_GREETING)
    hooks = CollectingHooks()

    report = run_deliver(adapter, targets("j1", greeting="   "), RESUME, hooks.deliver())

    assert adapter.calls == ["caps"]
    assert report.greeted == 0
    assert hooks.skipped[0][0] == "j1"


def test_run_deliver_投递失败记跳过():
    """apply 抛异常不假装成功。"""
    adapter = RecordingAdapter(gate=ApplyGate.AFTER_GREETING, apply_raises=True)
    hooks = CollectingHooks()

    report = run_deliver(adapter, targets("j1"), RESUME, hooks.deliver())

    assert report.greeted == 1
    assert report.applied == 0
    assert report.outcomes["j1"] == GateOutcome.SKIPPED_BY_GATE


# --- site_settings 的共享执行规则（docs 6.5） ---


def test_没开投递_只发对话不投简历():
    """`apply_enabled=0` 是站点开关：话照发，简历不投，一个 apply 都不许调。"""
    adapter = RecordingAdapter(gate=ApplyGate.AFTER_GREETING)
    hooks = CollectingHooks()

    report = run_deliver(
        adapter,
        targets("j1"),
        RESUME,
        hooks.deliver(),
        limits=SiteLimits(site="fake", apply_enabled=False),
    )

    assert "apply" not in adapter.calls
    assert report.greeted == 1
    assert report.applied == 0
    assert report.outcomes["j1"] == GateOutcome.SKIPPED_BY_SETTINGS
    assert "apply_enabled" in hooks.skipped[0][1]


def test_开了投递_照旧投():
    """开关打开时行为跟不传 limits 一样。"""
    adapter = RecordingAdapter(gate=ApplyGate.AFTER_GREETING)
    report = run_deliver(
        adapter,
        targets("j1"),
        RESUME,
        CollectingHooks().deliver(),
        limits=SiteLimits(site="fake", apply_enabled=True),
    )
    assert report.applied == 1
    assert report.outcomes["j1"] == GateOutcome.APPLIED


def test_每日配额_到顶整批停():
    """配额按发对话条数计，到顶就不再碰站点，剩下的记跳过。"""
    adapter = RecordingAdapter(gate=ApplyGate.AFTER_GREETING)
    hooks = CollectingHooks()

    report = run_deliver(
        adapter,
        targets("j1", "j2", "j3"),
        RESUME,
        hooks.deliver(),
        limits=SiteLimits(site="fake", daily_quota=1, apply_enabled=True),
    )

    assert report.greeted == 1
    assert report.skipped == 2
    assert adapter.calls.count("open_session") == 1
    assert "配额" in report.stopped_reason
    assert report.outcomes["j2"] == GateOutcome.SKIPPED_BY_SETTINGS


def test_每日配额_算上今天已经发过的():
    """余量 = 上限 - 今天已发；今天已经用完就一条都不发。"""
    adapter = RecordingAdapter(gate=ApplyGate.AFTER_GREETING)
    report = run_deliver(
        adapter,
        targets("j1", "j2"),
        RESUME,
        CollectingHooks().deliver(),
        limits=SiteLimits(site="fake", daily_quota=3),
        sent_today=3,
    )
    assert report.greeted == 0
    assert adapter.calls == ["caps"], "配额没了不该碰站点"
    assert all(v == GateOutcome.SKIPPED_BY_SETTINGS for v in report.outcomes.values())


def test_配额不限时_不限制条数():
    """`daily_quota=0` = 不限，别拿 0 当上限。"""
    adapter = RecordingAdapter(gate=ApplyGate.AFTER_GREETING)
    report = run_deliver(
        adapter,
        targets("j1", "j2", "j3"),
        RESUME,
        CollectingHooks().deliver(),
        limits=SiteLimits(site="fake", daily_quota=0),
        sent_today=99,
    )
    assert report.greeted == 3


# --- 熔断（classify_fault 驱动）：站点只归类，共享层判停 ---


def test_熔断_账号异常一发就停整批():
    """`RISK_CONTROL` 这类不该攒次数，剩下的直接不发。"""
    adapter = RecordingAdapter(
        gate=ApplyGate.AFTER_GREETING,
        fault=DeliverFault.RISK_CONTROL,
        send_error=RuntimeError("账号异常"),
    )
    hooks = CollectingHooks()

    report = run_deliver(adapter, targets("j1", "j2", "j3"), RESUME, hooks.deliver())

    assert adapter.calls.count("open_session") == 1
    assert report.greeted == 0
    assert report.skipped == 3
    assert "整批停住" in report.stopped_reason
    assert report.outcomes["j3"] == GateOutcome.SKIPPED_BY_SETTINGS


def test_熔断_限频攒到阈值才停():
    """限频先退避，攒满 3 次才停；每次退避都调 `hooks.cooldown`。"""
    adapter = RecordingAdapter(
        gate=ApplyGate.AFTER_GREETING,
        fault=DeliverFault.RATE_LIMIT,
        send_error=RuntimeError("太频繁"),
    )
    hooks = CollectingHooks()

    report = run_deliver(
        adapter, targets("j1", "j2", "j3", "j4", "j5"), RESUME, hooks.deliver()
    )

    assert adapter.calls.count("open_session") == 3
    assert hooks.cooldowns == 2, "触发熔断那次不再退避，直接停"
    assert report.skipped == 5
    assert "连续 3 次" in report.stopped_reason


def test_熔断_普通失败不计入():
    """`UNKNOWN` 只废这一条，计数清零，整批照走。"""
    adapter = RecordingAdapter(
        gate=ApplyGate.AFTER_GREETING,
        fault=DeliverFault.UNKNOWN,
        send_error=RuntimeError("偶发"),
    )
    hooks = CollectingHooks()

    report = run_deliver(adapter, targets("j1", "j2", "j3"), RESUME, hooks.deliver())

    assert adapter.calls.count("open_session") == 3
    assert report.stopped_reason == ""
    assert hooks.cooldowns == 0
