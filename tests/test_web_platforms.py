"""`GET /api/platforms` 的接口契约测试（docs/multiplatform-console-redesign.md §七）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from boss_web import create_app


@pytest.fixture
def client():
    """提供隔离库下的 TestClient。"""
    return TestClient(create_app())


def test_platforms_返回_boss_描述符(client: TestClient):
    """验描述符形状与前端 PlatformDescriptor 同构。"""
    res = client.get("/api/platforms")
    assert res.status_code == 200
    platforms = res.json()["platforms"]
    assert [p["site_id"] for p in platforms] == ["boss"]

    boss = platforms[0]
    assert boss["display_name"] == "BOSS直聘"
    assert boss["icon"] == "boss"
    assert boss["caps"] == {
        "auth": {"sms": True, "slider": True, "qr": True},
        "source": {"expect_flow": True, "tags": True, "skills": True},
        "catalog": {"unsupported": []},
        "delivery": {
            "apply_gate": "after_mutual_conversation",
            "apply_channel": "onekeydeliver",
        },
    }
    assert set(boss["session"]) == {"logged_in", "phone_masked", "busy"}
    assert boss["nav_extras"] == [
        {"key": "expect", "label": "求职期望", "route": "expect"}
    ]


def test_platforms_会话摘要来自登录态(client: TestClient):
    """验 session 三件套与 `GET /api/auth/status` 同源。"""
    platforms = client.get("/api/platforms").json()["platforms"]
    status = client.get("/api/auth/status").json()
    session = platforms[0]["session"]
    assert session["logged_in"] is bool(status["logged_in"])
    assert session["phone_masked"] == (status.get("phone_masked") or "")
    assert isinstance(session["busy"], bool)


def test_platforms_不写死平台清单(client: TestClient):
    """验返回体没有前端写死的痕迹——形状只来自注册表。"""
    body = client.get("/api/platforms").json()
    assert set(body) == {"platforms"}
    assert all("site_id" in p for p in body["platforms"])
