"""启动时的登录态自检：验真伪、失效清库、断网不动库。"""

from __future__ import annotations

import threading
import time

import pytest

from boss_login.errors import BossLoginError, SessionExpired, TransportError
from boss_login.models import UserInfo
from boss_login.session import load_session, save_session, StoredSession
from boss_web.services import session_check


class FakeClient:
    """只实现 `fetch_user_info` 的假客户端。"""

    def __init__(self, result):
        """初始化。"""
        self._result = result

    def fetch_user_info(self):
        """返回预设结果；预设为异常时原样抛出。"""
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


def _factory(result):
    """造出返回固定结果的 client_factory。"""
    def make(**_kwargs):
        """构造固定结果的假客户端。"""
        return FakeClient(result)

    return make


def _seed_session(path, *, name="张三") -> None:
    """往库里塞一份假登录态。"""
    save_session(
        StoredSession(
            token="tok-1",
            cookies={"__zp_stoken__": "stale"},
            phone_masked="138****8000",
            user={"user_id": "u1", "name": name, "identity": "geek"},
            saved_at=1_700_000_000.0,
        ),
        path,
    )


@pytest.fixture(autouse=True)
def _clean_cache():
    """每条用例前后清掉自检缓存。"""
    session_check.reset_cache()
    yield
    session_check.reset_cache()




@pytest.mark.parametrize(
    ("message", "want"),
    [
        ("未登录", True),
        ("当前登录状态已失效", True),
        ("登录已过期", True),
        ("凭证失效，请重新登录", True),
        ("系统繁忙", False),
        ("接口不存在", False),
        ("", False),
    ],
)
def test_looks_like_session_expired(message, want):
    """验失效话术的识别边界。"""
    assert session_check.looks_like_session_expired(message) is want




def test_absent_when_db_has_nothing(tmp_path):
    """验空库报未登录且不清库。"""
    result = session_check.check_now(session_path=tmp_path / "boss.db")

    assert result["state"] == session_check.ST_ABSENT
    assert result["logged_in"] is False
    assert result["cleared"] is False


def test_valid_refreshes_stored_user(tmp_path):
    """验有效会话刷新用户信息但不动登录时刻。"""
    path = tmp_path / "boss.db"
    _seed_session(path, name="旧名字")
    user = UserInfo(user_id="u1", name="新名字", phone_masked="138****8000", identity="geek")

    result = session_check.check_now(session_path=path, client_factory=_factory(user))

    assert result["state"] == session_check.ST_VALID
    assert result["logged_in"] is True
    assert result["user"]["name"] == "新名字"
    stored = load_session(path)
    assert stored.user["name"] == "新名字"
    assert stored.saved_at == 1_700_000_000.0
    assert stored.cookies == {"__zp_stoken__": "stale"}


def test_expired_clears_db(tmp_path):
    """服务端明说失效：库里那份必须被更新掉，不能继续谎报「已登录」。"""
    path = tmp_path / "boss.db"
    _seed_session(path)

    result = session_check.check_now(
        session_path=path,
        client_factory=_factory(SessionExpired("当前登录状态已失效")),
    )

    assert result["state"] == session_check.ST_EXPIRED
    assert result["logged_in"] is False
    assert result["cleared"] is True
    assert load_session(path).is_empty


def test_busy_error_keeps_db(tmp_path):
    """「系统繁忙」也会被 fetch_user_info 包成 SessionExpired，但那不是失效。"""
    path = tmp_path / "boss.db"
    _seed_session(path)

    result = session_check.check_now(
        session_path=path,
        client_factory=_factory(SessionExpired("系统繁忙")),
    )

    assert result["state"] == session_check.ST_UNKNOWN
    assert result["logged_in"] is True
    assert result["cleared"] is False
    assert load_session(path).cookies == {"__zp_stoken__": "stale"}


def test_network_error_keeps_db(tmp_path):
    """验网络错误不动库。"""
    path = tmp_path / "boss.db"
    _seed_session(path)

    result = session_check.check_now(
        session_path=path,
        client_factory=_factory(TransportError("网络请求失败：timed out")),
    )

    assert result["state"] == session_check.ST_UNKNOWN
    assert result["cleared"] is False
    assert load_session(path).cookies == {"__zp_stoken__": "stale"}


def test_unexpected_error_keeps_db(tmp_path):
    """验意外错误不动库。"""
    path = tmp_path / "boss.db"
    _seed_session(path)

    result = session_check.check_now(
        session_path=path,
        client_factory=_factory(RuntimeError("假客户端炸了")),
    )

    assert result["state"] == session_check.ST_UNKNOWN
    assert result["cleared"] is False
    assert load_session(path).cookies == {"__zp_stoken__": "stale"}


def test_generic_login_error_message_still_counts(tmp_path):
    """话术里点了名「登录」的，就算是别的异常类型也得认。"""
    path = tmp_path / "boss.db"
    _seed_session(path)

    result = session_check.check_now(
        session_path=path,
        client_factory=_factory(BossLoginError("请先登录")),
    )

    assert result["state"] == session_check.ST_EXPIRED
    assert load_session(path).is_empty




def test_snapshot_carries_last_result(tmp_path):
    """验快照带上最近一次自检结论。"""
    session_check.check_now(session_path=tmp_path / "boss.db")
    snap = session_check.snapshot()

    assert snap["state"] == session_check.ST_ABSENT
    assert snap["checked_at"] > 0


def test_schedule_startup_check_sets_event_and_calls_back(tmp_path):
    """验调度完成置位事件并回调。"""
    seen: list[dict] = []
    done = session_check.schedule_startup_check(
        session_path=tmp_path / "boss.db",
        client_factory=_factory(UserInfo(user_id="u1", name="张三")),
        on_done=seen.append,
    )

    assert done.wait(5.0) is True
    assert seen and seen[0]["state"] == session_check.ST_ABSENT


def test_schedule_startup_check_sets_event_even_on_callback_crash(tmp_path):
    """验回调炸了也照样置位。"""
    def boom(_result):
        """抛预设异常。"""
        raise RuntimeError("打印挂了")

    done = session_check.schedule_startup_check(
        session_path=tmp_path / "boss.db",
        client_factory=_factory(UserInfo()),
        on_done=boom,
    )

    assert done.wait(5.0) is True


def test_schedule_startup_check_sets_event_when_checker_crashes(tmp_path, monkeypatch):
    """自检本身炸了也要放行开浏览器，否则用户对着一个永远打不开的页面。"""

    def boom(**_kwargs):
        """抛预设异常。"""
        raise RuntimeError("自检炸了")

    monkeypatch.setattr(session_check, "check_now", boom)
    done = session_check.schedule_startup_check(session_path=tmp_path / "boss.db")

    assert done.wait(5.0) is True




def test_format_console_lines_covers_states():
    """验各状态的控制台单行文案。"""
    cases = {
        "valid": "已登录",
        "expired": "已失效",
        "absent": "未登录",
        "unknown": "没测到",
    }
    for state, want in cases.items():
        lines = session_check.format_console_lines({"state": state, "message": "", "error": "xx"})
        assert len(lines) == 1
        assert lines[0].startswith("  登录状态")
        assert want in lines[0]


def test_format_console_lines_unknown_keeps_the_error():
    """验未知态把错误原文留下。"""
    lines = session_check.format_console_lines(
        {"state": "unknown", "error": "网络请求失败：timed out"}
    )
    assert "timed out" in lines[0]
    assert "原样保留" in lines[0]




def test_auth_status_carries_check_result():
    """首屏会话卡片要能区分「库里没有」和「启动时验过、确实失效」。"""
    from fastapi.testclient import TestClient

    from boss_web.app import create_app

    session_check.reset_cache()
    client = TestClient(create_app())
    body = client.get("/api/auth/status").json()

    assert "logged_in" in body
    assert "check" in body
    assert set(body["check"]) >= {"state", "checked_at", "message", "cleared"}


def test_auth_status_logged_in_false_when_check_says_expired(tmp_path):
    """清库失败的边角：自检说过期就不能再让界面报「已登录」。"""
    from boss_web.services.login_task import login_tasks

    path = tmp_path / "boss.db"
    save_session(
        StoredSession(cookies={"__zp_stoken__": "stale"}, token="tok"),
        path,
    )
    session_check.reset_cache()
    session_check._store(state=session_check.ST_EXPIRED, cleared=False, logged_in=False)

    try:
        payload = login_tasks.status_payload()
        assert payload["logged_in"] is False
        assert payload["check"]["state"] == session_check.ST_EXPIRED
    finally:
        session_check.reset_cache()


def test_api_responses_are_not_cached():
    """/api/auth/status 被浏览器缓存过一次「未登录」，登录后首屏就一直显示离线。"""
    from fastapi.testclient import TestClient

    from boss_web.app import create_app

    client = TestClient(create_app())
    resp = client.get("/api/auth/status")
    assert resp.headers.get("Cache-Control") == "no-store"
