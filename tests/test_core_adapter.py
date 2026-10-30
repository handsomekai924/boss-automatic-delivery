"""`boss_web.core` 的单元测试：契约形状、注册表、BOSS 薄壳自述。"""

from __future__ import annotations

import pytest

from boss_web.core import (
    ApplyGate,
    ConversationState,
    DeliverFault,
    SearchCriteria,
    build_descriptor,
    get_adapter,
    register,
    registered_sites,
    reset_registry,
)
from boss_web.core.adapters.boss import BossAdapter
from boss_web.core.registry import all_adapters


@pytest.fixture(autouse=True)
def _restore_registry():
    """每条用例前后把注册表还原成「只有 boss」。"""
    reset_registry()
    yield
    reset_registry()
    from boss_web.core.adapters import load

    load()


def test_注册表_取到_boss():
    """验 import 装配后能按 site_id 取到 BossAdapter。"""
    from boss_web.core.adapters import load

    load()
    adapter = get_adapter("boss")
    assert isinstance(adapter, BossAdapter)
    assert adapter.site_id == "boss"
    assert adapter.display_name == "BOSS直聘"
    assert registered_sites() == ("boss",)


def test_注册表_没这个站点报错():
    """验未知 site_id 抛 KeyError。"""
    from boss_web.core.adapters import load

    load()
    with pytest.raises(KeyError):
        get_adapter("51job")


def test_注册表_同名不同类报错():
    """验重复注册同名不同类抛 ValueError。"""

    class _A:
        site_id = "x"

    class _B:
        site_id = "x"

    register("x")(_A)
    with pytest.raises(ValueError):
        register("x")(_B)


def test_SearchCriteria_is_blank():
    """验全空维度识别（抓取前要拦）。"""
    assert SearchCriteria().is_blank()
    assert not SearchCriteria(keyword="前端").is_blank()
    assert not SearchCriteria(city="101280100").is_blank()


def test_boss_caps_自述():
    """验 BOSS 能力自述与契约 JSON 同构。"""
    adapter = get_adapter("boss")
    auth = adapter.auth.caps()
    assert auth.supports_sms is True
    assert auth.supports_slider is True
    assert auth.supports_qr is True

    source = adapter.jobs.caps()
    assert source.has_expect_flow is True
    assert source.has_tags is True
    assert source.has_skills is True

    assert adapter.catalog.unsupported() == ()

    delivery = adapter.applier.caps()
    assert delivery.apply_gate is ApplyGate.AFTER_MUTUAL_CONVERSATION
    assert delivery.apply_channel == "onekeydeliver"


def test_boss_nav_extras():
    """验 BOSS 私有入口挂求职期望流。"""
    extras = get_adapter("boss").nav_extras()
    assert [(n.key, n.label, n.route) for n in extras] == [
        ("expect", "求职期望", "expect")
    ]


def test_build_descriptor_形状():
    """验描述符与前端 PlatformDescriptor 同构（docs §七）。"""
    descriptor = build_descriptor(
        get_adapter("boss"),
        {"logged_in": True, "phone_masked": "138****8000", "busy": False},
    )
    assert descriptor["site_id"] == "boss"
    assert descriptor["display_name"] == "BOSS直聘"
    assert descriptor["icon"] == "boss"
    assert descriptor["caps"]["auth"] == {"sms": True, "slider": True, "qr": True}
    assert descriptor["caps"]["source"] == {
        "expect_flow": True,
        "tags": True,
        "skills": True,
    }
    assert descriptor["caps"]["catalog"]["unsupported"] == []
    assert descriptor["caps"]["delivery"] == {
        "apply_gate": "after_mutual_conversation",
        "apply_channel": "onekeydeliver",
    }
    assert descriptor["session"] == {
        "logged_in": True,
        "phone_masked": "138****8000",
        "busy": False,
    }
    assert descriptor["nav_extras"] == [
        {"key": "expect", "label": "求职期望", "route": "expect"}
    ]


def test_to_search_params_空维度不进串():
    """验通用条件映射成 BOSS 参数串时空值不进串。"""
    adapter = get_adapter("boss")
    params = adapter.catalog.to_search_params(SearchCriteria())
    assert params["page"] == "1"
    assert params["pageSize"] == "15"
    assert "query" not in params
    assert "city" not in params

    params = adapter.catalog.to_search_params(
        SearchCriteria(keyword="前端", city="101280100", experience=("108",))
    )
    assert params["query"] == "前端"
    assert params["city"] == "101280100"
    assert params["experience"] == "108"


def test_classify_fault_映射():
    """验 BOSS 业务码 → 统一故障类。"""
    from boss_jobs.errors import ChatSendError, JobApiError, JobTransportError

    adapter = get_adapter("boss")
    assert adapter.classify_fault(JobApiError(7, "请先登录")) is DeliverFault.SESSION_CLOSED
    assert adapter.classify_fault(JobApiError(36, "账户存在异常行为")) is DeliverFault.RISK_CONTROL
    assert adapter.classify_fault(JobApiError(37, "浏览器环境异常")) is DeliverFault.RISK_CONTROL
    assert adapter.classify_fault(JobApiError(1, "今日沟通次数已用完")) is DeliverFault.QUOTA
    assert adapter.classify_fault(JobApiError(1, "操作过于频繁")) is DeliverFault.RATE_LIMIT
    assert adapter.classify_fault(JobApiError(1, "别的")) is DeliverFault.UNKNOWN
    assert adapter.classify_fault(ChatSendError("没发出去")) is DeliverFault.UNKNOWN
    assert adapter.classify_fault(JobTransportError("超时")) is DeliverFault.UNKNOWN
    assert adapter.classify_fault(RuntimeError("x")) is DeliverFault.UNKNOWN


def test_applier_未接入时诚实拒绝():
    """验 onekeydeliver 通道没接入时不假装成功（要真实接口不要界面模拟）。"""
    from boss_web.core.contracts import ResumeRef, SessionRef

    adapter = get_adapter("boss")
    session = SessionRef(site_id="boss", session_id="j1")
    resume = ResumeRef(site_resume_id="r1")
    with pytest.raises(NotImplementedError):
        adapter.applier.apply(session, resume)


def test_conversation_state_默认等回复():
    """验拿不到会话对象时按等回复处理（不误判 REPLIED）。"""
    from boss_web.core.contracts import SessionRef

    adapter = get_adapter("boss")
    state = adapter.messenger.conversation_state(
        SessionRef(site_id="boss", session_id="j1")
    )
    assert state is ConversationState.WAITING_REPLY


def test_all_adapters_装配完整():
    """验装配点把 BossAdapter 挂全。"""
    adapters = all_adapters()
    assert [a.site_id for a in adapters] == ["boss"]
