"""多平台核心契约（todo3 2.1）：零依赖的数据类与枚举。

这里只描述「形状」，不 import 任何 `boss_*` 包——把现有实现适配到这些形状上
是 `boss_web/core/adapters/*`（平台包的前身）的活。server 编排层只认这里。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class ApplyGate(Enum):
    """投递门槛：发对话 → 过门槛 → 投递（todo3 2.3）。

    管的是**什么时候允许投简历**，不是「要不要发对话」——发对话永远是第一步
    （2026-10-28 口径）。产品层的「投递」是平台能力（BOSS = 打招呼，直投站 = 投简历），
    这里专指**投简历**那一腿。
    """

    AFTER_MUTUAL_CONVERSATION = "after_mutual_conversation"  # 默认：双方对话成立后才投简历
    AFTER_GREETING = "after_greeting"  # 发完对话即可投简历（不用等对方回话）


class ConversationState(Enum):
    """会话阶段（发对话与投递之间的门槛判据）。"""

    WAITING_REPLY = "waiting_reply"  # 对话已发出，对方未回
    REPLIED = "replied"  # 双方对话成立
    CLOSED = "closed"  # 会话关闭/对方拒绝


class DeliverFault(Enum):
    """发对话/投递共用的故障分类（站点自述映射，前端按类渲染统一文案）。"""

    QUOTA = "quota"  # 配额用尽
    RATE_LIMIT = "rate_limit"  # 限频
    RISK_CONTROL = "risk_control"  # 风控/安全
    SESSION_CLOSED = "session_closed"  # 登录态失效
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AuthCaps:
    """登录能力自述：缺哪项 UI 就降级哪项入口。"""

    supports_sms: bool = True
    supports_slider: bool = True  # 人机协作滑块（不是绕过）
    supports_qr: bool = False  # 扫码


@dataclass(frozen=True)
class SourceCaps:
    """职位来源能力自述。"""

    has_expect_flow: bool = False  # BOSS 求职期望流这类站点私有检索路径
    has_tags: bool = False
    has_skills: bool = False


@dataclass(frozen=True)
class DeliveryCaps:
    """投递能力自述。"""

    apply_gate: ApplyGate = ApplyGate.AFTER_MUTUAL_CONVERSATION
    apply_channel: str = ""  # 站点投递机制自述（onekeydeliver / 站内表单 / …）


@dataclass(frozen=True)
class SearchCriteria:
    """通用检索维度；站点差异走 `Catalog.caps().unsupported` 置灰，不硬塞。

    code 语义沿用 `boss_filter`（字符串 code，空 = 站点默认）。
    """

    keyword: str = ""
    city: str = ""
    job_type: str = ""
    salary: str = ""
    experience: tuple[str, ...] = ()
    degree: tuple[str, ...] = ()
    industry: tuple[str, ...] = ()
    scale: tuple[str, ...] = ()
    pay_type: tuple[str, ...] = ()
    part_time: tuple[str, ...] = ()
    stage: tuple[str, ...] = ()
    page: int = 1
    page_size: int = 15

    def is_blank(self) -> bool:
        """检索维度是否全空（空 = 站点默认全量，抓取前应拦）。"""
        return not any([
            self.keyword.strip(), self.city, self.job_type, self.salary,
            self.experience, self.degree, self.industry, self.scale,
            self.pay_type, self.part_time, self.stage,
        ])


@dataclass(frozen=True)
class JobRef:
    """职位引用：跨站点唯一定位 = (site_id, job_id)；站点私有键进 `extra`。

    BOSS 对应 job_id=encrypt_job_id，extra 带 security_id / lid / encrypt_boss_id。
    """

    site_id: str
    job_id: str
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PageResult:
    """一页职位结果（核心层只看形状，item 是站点私有职位对象）。"""

    items: tuple[Any, ...] = ()
    page: int = 1
    has_more: bool = False


@dataclass(frozen=True)
class SessionRef:
    """发对话会话引用（BOSS：friend/add 建起来的会话）。"""

    site_id: str
    session_id: str
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SendReceipt:
    """发对话回执：只表示「话发出去了」。"""

    ok: bool
    message: str = ""


@dataclass(frozen=True)
class ResumeRef:
    """投递用简历引用：**能走站内简历就用站内简历 ID**（todo3 2.1 拍板）。

    对应关系挂在 resume 侧（resume_id + site → site_resume_id）。
    """

    site_resume_id: str
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ApplyReceipt:
    """投递回执。"""

    ok: bool
    message: str = ""


@dataclass(frozen=True)
class LoginState:
    """登录状态轮询结果（poll 的返回）。"""

    status: str  # running / need_code / need_slider / need_scan / done / error
    message: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)


# 登录任务句柄：BOSS 就是 `login_task` 的 task_id。
LoginHandle = str


@dataclass(frozen=True)
class NavExtra:
    """平台私有入口（BOSS 求职期望流），注册进 `/:siteId/:route`。"""

    key: str
    label: str
    route: str


# 站点筛选选项表：形状站点私有（FilterConditions.to_dict() 一类），核心层不解析。
SiteFilterOptions = Mapping[str, Any]

# 站点私有检索参数串：Catalog 产出、JobSource 消费，server 只当不透明对象传递。
SearchParams = Mapping[str, Any]
