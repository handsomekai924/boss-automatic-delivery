"""职位获取模块的异常。

分两层：
    JobError        基类
      ├── JobTransportError  网络层失败（超时、连接错误、响应不是 JSON）
      ├── JobApiError        服务端返回了失败业务码（含登录态失效、浏览器校验）
      ├── JobDataError       响应是成功码但结构对不上，解析不出来
      └── ChatSendError      聊天通道（MQTT over WebSocket）发消息失败
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from . import config as C

#: 弹窗话术里的剩余次数，如「您今天已与120位BOSS沟通，还剩30次沟通机会哦」
_CHAT_REMAIN_RE = re.compile(r"还剩\s*(\d+)\s*次")


def dig_chat_remind(payload: Any) -> Mapping[str, Any] | None:
    """从响应包里抠出 ``zpData.bizData.chatRemindDialog``（没有回 ``None``）。

    「开聊提醒」那类**每日沟通配额**弹窗就长这样（2026-10-09 实测
    ``friend/add.json``）::

        {"code":1,"message":"开聊提醒",
         "zpData":{"bizCode":1,"bizMessage":"开聊提醒",
                   "bizData":{"chatRemindDialog":{"title":"温馨提示",
                       "content":"您今天已与120位BOSS沟通，还剩30次沟通机会哦",
                       "remindType":524288,"blockLevel":0, …}}}}

    顶层 ``message`` 只是「开聊提醒」这个短标签，**真话在 dialog.content 里**。
    """
    if not isinstance(payload, Mapping):
        return None
    zp = payload.get("zpData")
    if not isinstance(zp, Mapping):
        return None
    biz = zp.get("bizData")
    if not isinstance(biz, Mapping):
        return None
    dialog = biz.get("chatRemindDialog")
    return dialog if isinstance(dialog, Mapping) else None


def format_api_message(payload: Mapping[str, Any]) -> str:
    """从响应包里抠出**给人看的话术**。

    站点的顶层 ``message`` 常常只是个短标签（如「开聊提醒」）；有
    :func:`dig_chat_remind` 那种弹窗时，真话在 ``content`` 里，优先用它。
    """
    dialog = dig_chat_remind(payload)
    if dialog:
        content = str(dialog.get("content") or "").strip()
        if content:
            return content
        title = str(dialog.get("title") or "").strip()
        if title:
            return title
    for key in ("message", "msg"):
        val = payload.get(key)
        if val:
            return str(val)
    return ""


def chat_remind_remaining(payload: Any) -> int | None:
    """从「开聊提醒」话术里抠「还剩 N 次」；抠不出来回 ``None``。

    传响应包（Mapping）或直接传话术字符串都行。实测话术长这样：
    「您今天已与120位BOSS沟通，还剩30次沟通机会哦」——**30 是还能主动
    沟通的次数**，不是「今天已经没了」，所以 > 0 时发送任务该继续用掉它，
    别一见弹窗就整批停手。
    """
    if isinstance(payload, Mapping):
        text = format_api_message(payload)
    else:
        text = str(payload or "")
    m = _CHAT_REMAIN_RE.search(text)
    return int(m.group(1)) if m else None


class JobError(Exception):
    """职位获取失败的基类。"""

    def __init__(self, message: str, *, raw: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.raw = raw

    def __str__(self) -> str:  # pragma: no cover - 直接复用 message
        return self.message


class JobTransportError(JobError):
    """网络层错误：超时、DNS、连接被重置、响应不是合法 JSON 等。"""


class JobApiError(JobError):
    """服务端返回了非成功业务码。``code`` 为业务码，非 HTTP 状态码。"""

    def __init__(self, code: int, message: str, *, raw: Any = None) -> None:
        super().__init__(message or f"接口返回异常码 {code}", raw=raw)
        self.code = code

    @property
    def is_session_expired(self) -> bool:
        """True 表示登录态失效（接口回了「当前登录状态已失效」这类话术/码）。

        **code 1 不算**——它是业务失败的通用码，「聊天的Boss不存在」
        「非好友关系」「开聊提醒」（每日沟通配额）都走它；一律当登录失效
        会把人支去重新登录、登录了也还是发不出去（2026-10-09 实测踩过）。
        只有 code 7（实测的「当前登录状态已失效」）或话术里明说了登录
        问题才算。
        """
        return self.code in C.CODE_SESSION_EXPIRED or "登录" in self.message

    @property
    def chat_remind(self) -> Mapping[str, Any] | None:
        """``chatRemindDialog`` 那个弹窗（没有回 ``None``）。见 :func:`dig_chat_remind`。"""
        return dig_chat_remind(self.raw)

    @property
    def is_chat_remind(self) -> bool:
        """True 表示撞上「开聊提醒」——BOSS 的**每日沟通配额**提醒弹窗。

        实测（2026-10-09）：``friend/add.json`` 回 ``code 1`` +
        ``chatRemindDialog``，content 是「您今天已与120位BOSS沟通，还剩30次
        沟通机会哦」，``blockLevel: 0``。**这是提示弹窗，不是硬拦**：

        - 不是登录失效（别叫人去重新登录）；
        - 不是账号风控（code 36）；
        - 「还剩30次」= 今天**还能再主动沟通 30 次**，不是已经没了。

        站点前端点弹窗上的「好」之后会带 ``cid=1`` 再打一次
        ``friend/add``（模拟点击确认，见
        :meth:`boss_jobs.client.JobClient.greet`），带对了就能把剩下的
        次数用掉。
        """
        return self.chat_remind is not None or "开聊提醒" in self.message

    @property
    def chat_remind_remaining(self) -> int | None:
        """弹窗话术里的「还剩 N 次」；抠不出来回 ``None``。见 :func:`chat_remind_remaining`。"""
        return chat_remind_remaining(self.raw if self.raw is not None else self.message)

    @property
    def is_browser_check(self) -> bool:
        """True 表示撞上安全网关（code 37「浏览器环境异常」）。

        缺的是 ``__zp_stoken__`` 安全网关令牌。令牌的来源与算法见
        :mod:`boss_jobs.stoken`——本项目按站点前端那条链路**自动算**，
        算完重试即可，不用人工回浏览器。
        """
        return self.code == C.CODE_BROWSER_CHECK

    @property
    def is_risk_control(self) -> bool:
        """True 表示账号被风控（code 36「您的账户存在异常行为」）。

        这不是缺令牌，是账号维度的风险状态，通常要走人机验证
        （GeeTest）后才恢复。客户端不去绕，把话术说清楚。
        """
        return self.code == C.CODE_RISK_CONTROL


class JobDataError(JobError):
    """响应业务码是成功，但字段形状不对，没法映射成职位。"""


class ChatSendError(JobError):
    """聊天通道（MQTT over WebSocket）没连上 / 没发出去。

    跟 :class:`JobApiError` 分开，是因为它**没有业务码**：失败在传输层
    （握手被拒、CONNACK 非成功、PUBLISH 直接抛 / ``rc != 0``），重试策略也不一样——
    见 :mod:`boss_jobs.chat`。上层（发送任务）把它当「这条发送失败」记流水，
    **不重试**（避免重复打扰招聘方）。

    ⚠️ **没等到 PUBACK 不算失败**：这条网关对文本帧不回 PUBACK，发完就把连接
    关掉是常态（见 :mod:`boss_jobs.chat` 模块头）。本异常只在**握手/连接没成、
    或 PUBLISH 本身报错**时抛。
    """
