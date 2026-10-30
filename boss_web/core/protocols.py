"""能力协议（todo3 2.1）：五套 Protocol + 喂料口 + 门面。

登录、筛选、抓取、发对话、投递五套协议差异极大，按能力拆开——策略落在能力上，
工厂/注册表落在门面上（`registry`）。server 编排层只 import 这里的 Protocol，
不 import 任何 `boss_*` 实现。
"""

from __future__ import annotations

from typing import Any, Iterator, Mapping, Protocol, runtime_checkable

from .contracts import (
    ApplyGate,
    ApplyReceipt,
    AuthCaps,
    ConversationState,
    DeliveryCaps,
    DeliverFault,
    JobRef,
    LoginHandle,
    LoginState,
    NavExtra,
    PageResult,
    ResumeRef,
    SearchCriteria,
    SearchParams,
    SendReceipt,
    SessionRef,
    SiteFilterOptions,
    SourceCaps,
)


@runtime_checkable
class AuthSink(Protocol):
    """人机协作喂料口：只通知（全 `-> None`），票据/验证码由外部塞回。

    server 登录任务层实现，桥接到前端表单（现 `login_task` 的喂料回调）。
    """

    def need_code(self, handle: LoginHandle, payload: Mapping[str, Any]) -> None:
        """要短信验证码。"""

    def need_slider(self, handle: LoginHandle, payload: Mapping[str, Any]) -> None:
        """要人机协作滑块（不是绕过）。"""

    def need_scan(self, handle: LoginHandle, payload: Mapping[str, Any]) -> None:
        """要扫码。"""

    def finished(self, handle: LoginHandle, state: LoginState) -> None:
        """登录成功。"""

    def failed(self, handle: LoginHandle, message: str) -> None:
        """登录失败。"""


@runtime_checkable
class AuthFlow(Protocol):
    """交互式登录：人机协作，不是同步方法。

    喂料模型（2026-10-14 拍板）：**推送为主**——`AuthSink` 只通知，
    验证码 / 滑块票据由外部经 `submit_*` 塞回，`poll` 看状态。
    """

    def caps(self) -> AuthCaps:
        """登录能力自述；缺哪项 UI 就降级哪项入口。"""

    def start_sms(self, phone: str, sink: AuthSink) -> LoginHandle:
        """起短信登录，返回句柄。"""

    def submit_code(self, handle: LoginHandle, code: str) -> None:
        """把验证码塞回阻塞中的流程。"""

    def submit_slider(self, handle: LoginHandle, ticket: str | Mapping[str, Any]) -> None:
        """把滑块票据塞回阻塞中的流程（票据形态站点私有）。"""

    def start_qr(self, sink: AuthSink) -> LoginHandle:
        """起扫码登录。"""

    def poll(self, handle: LoginHandle) -> LoginState:
        """看一眼状态。"""


@runtime_checkable
class Catalog(Protocol):
    """筛选条件：拉可选值 + 把通用条件映射成站点检索参数。"""

    def fetch_filter_options(self) -> SiteFilterOptions:
        """站点筛选选项表（形状站点私有）。"""

    def unsupported(self) -> tuple[str, ...]:
        """不支持的 `SearchCriteria` 维度 key；UI 置灰不硬塞。"""

    def to_search_params(self, criteria: SearchCriteria) -> SearchParams:
        """产出**站点私有参数串**；server 只当不透明对象往下传给 `JobSource`。"""


@runtime_checkable
class JobSource(Protocol):
    """职位发现与详情。入参是 `Catalog` 产出的站点参数，不是 `SearchCriteria`。"""

    def caps(self) -> SourceCaps:
        """来源能力自述（期望流/标签/技能等）。"""

    def search_page(self, params: SearchParams, page: int) -> PageResult:
        """抓一页搜索结果。"""

    def iter_pages(self, params: SearchParams) -> Iterator[PageResult]:
        """翻页迭代；翻页策略是站点语义（BOSS 的 hasMore 第 1 页不可信、页间硬睡）——站点自己管。"""

    def fetch_detail(self, ref: JobRef) -> Any:
        """抓一条职位详情；返回形状站点私有（BOSS = `boss_jobs.JobDetail`）。"""


@runtime_checkable
class Messenger(Protocol):
    """发对话（打招呼）。BOSS = 建会话(friend/add) + MQTT 发正文；其他站点另说。"""

    def open_session(self, ref: JobRef) -> SessionRef:
        """建会话（不发正文）。"""

    def send_text(self, session: SessionRef, text: str) -> SendReceipt:
        """发正文。回执只表示「话发出去了」。"""

    def conversation_state(self, session: SessionRef) -> ConversationState:
        """会话阶段：发对话与投递之间的门槛判据。"""


@runtime_checkable
class Applier(Protocol):
    """投递简历——**独立于发对话**，且默认带门槛（todo3 2.3）。

    术语约定：「发对话」= 打招呼/聊天正文；「投递」= 简历送达（BOSS 对应
    `resume/onekeydeliver.json` 一类的投递通道）。两者是两个阶段，别混叫「投递」。
    产品层的「投递」是**平台能力**（BOSS 的投递行为 = 打招呼，直投站 = 投简历）；
    本协议里「投/apply」专指简历送达那一腿，发对话走 `Messenger`。
    """

    def caps(self) -> DeliveryCaps:
        """投递能力自述（门槛 + 通道名）。"""

    def apply(self, session: SessionRef, resume: ResumeRef) -> ApplyReceipt:
        """把简历投出去；调用方必须先过门槛。"""


@runtime_checkable
class SiteAdapter(Protocol):
    """站点门面：五能力组合 + 故障分类 + 私有入口，不做业务编排。"""

    site_id: str
    display_name: str
    icon: str
    auth: AuthFlow
    catalog: Catalog
    jobs: JobSource
    messenger: Messenger
    applier: Applier

    def nav_extras(self) -> tuple[NavExtra, ...]:
        """平台私有入口（BOSS 求职期望流）。"""

    def session_summary(self) -> Mapping[str, Any]:
        """登录态摘要（`logged_in` / `phone_masked` / `busy`），`/api/platforms` 用。"""

    def classify_fault(self, err: Exception) -> DeliverFault:
        """配额/限频/风控分类（2026-10-14 升到站点级共享）：发对话与投递共用。"""


__all__ = [
    "ApplyGate",
    "ApplyReceipt",
    "AuthCaps",
    "AuthFlow",
    "AuthSink",
    "Catalog",
    "ConversationState",
    "DeliveryCaps",
    "DeliverFault",
    "JobRef",
    "JobSource",
    "LoginHandle",
    "LoginState",
    "Messenger",
    "NavExtra",
    "PageResult",
    "ResumeRef",
    "SearchCriteria",
    "SearchParams",
    "SendReceipt",
    "SessionRef",
    "SiteAdapter",
    "SiteFilterOptions",
    "SourceCaps",
]
