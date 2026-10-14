"""版本检测：版本号比较、忽略水位线、Gitee 挑最新、接口契约。

不打真网——``fetch_latest_release`` 全部换掉。Gitee 的 ``/releases/latest``
实测会挑错版本，所以「从列表里挑最高正式版」这条才是真正在用的逻辑，必须钉死。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from boss_web import __version__ as APP_VERSION
from boss_web.app import create_app
from boss_web.services import app_settings_store, update_check


@pytest.fixture
def client():
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _no_cache():
    """每个用例都从「没测过」开始，免得缓存串味。"""
    update_check.reset_cache()
    yield
    update_check.reset_cache()


def _release(tag: str, *, prerelease: bool = False, notes: str = "说明") -> dict:
    return {
        "tag_name": tag,
        "name": tag,
        "prerelease": prerelease,
        "body": notes,
        "assets": [
            {
                "name": "BossAutoDelivery.exe",
                "browser_download_url": f"https://gitee.com/x/releases/download/{tag}/BossAutoDelivery.exe",
            },
            {
                "name": f"BossAutoDelivery-{tag.lstrip('v')}.zip",
                "browser_download_url": f"https://gitee.com/x/releases/download/{tag}/BossAutoDelivery-{tag.lstrip('v')}.zip",
            },
        ],
    }


def _fake_gitee(monkeypatch, releases: list[dict]):
    """把 Gitee 换成固定列表。"""
    monkeypatch.setattr(update_check, "fetch_latest_release", lambda: update_check.pick_latest(releases))


# --------------------------------------------------------------------------- #
# 版本号解析 / 比较
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("v1.2.3", (1, 2, 3)),
        ("1.2.3", (1, 2, 3)),
        ("V1.2.3", (1, 2, 3)),
        ("1.2", (1, 2)),
        ("v1.2.3-rc1", (1, 2, 3)),
        ("v1.2.3-beta.2", (1, 2, 3)),
        ("10.0.0", (10, 0, 0)),
        ("", None),
        ("abc", None),
        ("v", None),
    ],
)
def test_parse_version(raw, want):
    assert update_check.parse_version(raw) == want


@pytest.mark.parametrize(
    ("a", "b", "want"),
    [
        ("1.0.0", "1.0.1", -1),
        ("1.0.1", "1.0.0", 1),
        ("1.0.0", "v1.0.0", 0),
        ("1.0", "1.0.0", 0),
        ("v1.1.0", "1.0.0", 1),
        ("1.10.0", "1.9.0", 1),  # 数值比较，不是字符串
        ("1.0.0", "1.0.0-rc1", 0),  # 后缀不参与，靠 prerelease 标记过滤
    ],
)
def test_compare_versions(a, b, want):
    assert update_check.compare_versions(a, b) == want


# --------------------------------------------------------------------------- #
# 从 Gitee 列表挑最新
# --------------------------------------------------------------------------- #


def test_pick_latest_取最高正式版(monkeypatch):
    """Gitee 返回顺序跟新旧无关，v1.0.0 排前面也不能拿它当最新。"""
    latest = update_check.pick_latest(
        [
            _release("v1.0.0"),
            _release("v1.1.0"),
            _release("v0.9.0"),
        ]
    )
    assert latest is not None
    assert latest.version == "1.1.0"


def test_pick_latest_跳过预发布():
    latest = update_check.pick_latest(
        [
            _release("v2.0.0-rc1", prerelease=True),
            _release("v1.5.0"),
        ]
    )
    assert latest.version == "1.5.0"


def test_pick_latest_跳过认不出的_tag():
    latest = update_check.pick_latest([_release("nightly"), _release("v1.1.0")])
    assert latest.version == "1.1.0"


def test_pick_latest_没有正式版时回_None():
    assert update_check.pick_latest([]) is None
    assert update_check.pick_latest([_release("v2.0.0", prerelease=True)]) is None


def test_pick_latest_发行说明截断():
    latest = update_check.pick_latest([_release("v1.1.0", notes="x" * 5000)])
    assert len(latest.notes) <= update_check.NOTES_LIMIT + 5
    assert latest.notes.endswith("…")


def test_pick_latest_带上资产链接():
    latest = update_check.pick_latest([_release("v1.1.0")])
    assert any(a.url.endswith("BossAutoDelivery.exe") for a in latest.assets)
    assert any(a.name.endswith(".zip") for a in latest.assets)


def test_pick_zip_asset_优先叫_BossAutoDelivery_的包():
    assets = (
        update_check.Asset(name="notes.txt", url="https://x/notes.txt"),
        update_check.Asset(name="随便.zip", url="https://x/随便.zip"),
        update_check.Asset(name="BossAutoDelivery-1.1.0.zip", url="https://x/BossAutoDelivery-1.1.0.zip"),
    )
    picked = update_check.pick_zip_asset(assets)
    assert picked is not None
    assert picked.name.startswith("BossAutoDelivery")


def test_pick_zip_asset_名字空看_url_尾段():
    assets = (update_check.Asset(name="", url="https://x/BossAutoDelivery-1.1.0.zip?token=1"),)
    picked = update_check.pick_zip_asset(assets)
    assert picked is not None
    assert picked.url.startswith("https://x/BossAutoDelivery-1.1.0.zip")


def test_pick_zip_asset_没有_zip_就回_None():
    assert update_check.pick_zip_asset((update_check.Asset(name="a.exe", url="https://x/a.exe"),)) is None
    assert update_check.pick_zip_asset(()) is None


def test_can_self_update_源码跑为假(monkeypatch):
    from boss_web import runtime

    monkeypatch.setattr(runtime, "is_frozen", lambda: False)
    monkeypatch.setattr(runtime, "is_temp_run", lambda: False)
    assert update_check.can_self_update() is False


def test_can_self_update_压缩包预览跑也为假(monkeypatch):
    from boss_web import runtime

    monkeypatch.setattr(runtime, "is_frozen", lambda: True)
    monkeypatch.setattr(runtime, "is_temp_run", lambda: True)
    assert update_check.can_self_update() is False


def test_can_self_update_装在本地的_exe_为真(monkeypatch):
    from boss_web import runtime

    monkeypatch.setattr(runtime, "is_frozen", lambda: True)
    monkeypatch.setattr(runtime, "is_temp_run", lambda: False)
    assert update_check.can_self_update() is True


# --------------------------------------------------------------------------- #
# 状态组装：要不要弹窗
# --------------------------------------------------------------------------- #


def test_有新版本就该提示(monkeypatch):
    _fake_gitee(monkeypatch, [_release("v9.9.9", notes="修了个 bug")])
    body = update_check.build_status(force=True)

    assert body["current"] == APP_VERSION
    assert body["latest"] == "9.9.9"
    assert body["has_update"] is True
    assert body["should_prompt"] is True
    assert body["ignored"] is None
    assert body["error"] is None
    assert "修了个 bug" in body["notes"]


def test_版本一样不提示(monkeypatch):
    _fake_gitee(monkeypatch, [_release(f"v{APP_VERSION}")])
    body = update_check.build_status(force=True)

    assert body["has_update"] is False
    assert body["should_prompt"] is False


def test_本地比远端新也不提示(monkeypatch):
    """本地是开发版（比如 9.9.9）而 Gitee 还停在 1.0.0，不该倒着提示。"""
    _fake_gitee(monkeypatch, [_release("v1.0.0")])
    monkeypatch.setattr(update_check, "APP_VERSION", "9.9.9")
    body = update_check.build_status(force=True)

    assert body["has_update"] is False
    assert body["should_prompt"] is False


def test_忽略之后这一版不再提示(monkeypatch):
    _fake_gitee(monkeypatch, [_release("v9.9.9")])
    update_check.save_ignored_version("9.9.9")
    body = update_check.build_status(force=True)

    assert body["has_update"] is True, "「有更新」仍然成立，只是不弹窗"
    assert body["should_prompt"] is False
    assert body["ignored"] == "9.9.9"


def test_忽略之后来了更新的版本照样提示(monkeypatch):
    """忽略是水位线不是白名单：抬到 9.9.9 之后，10.0.0 还得弹。"""
    update_check.save_ignored_version("9.9.9")
    _fake_gitee(monkeypatch, [_release("v10.0.0")])
    body = update_check.build_status(force=True)

    assert body["latest"] == "10.0.0"
    assert body["should_prompt"] is True


def test_连续忽略多个版本只留最高水位(monkeypatch):
    """忽略 1.2.0 再忽略 1.3.0 ⇒ 水位 1.3.0；1.2.x 静音，1.4.0 才再弹。"""
    update_check.save_ignored_version("1.2.0")
    update_check.save_ignored_version("1.3.0")
    assert update_check.load_ignored_version() == "1.3.0"

    _fake_gitee(monkeypatch, [_release("v1.2.5")])
    assert update_check.build_status(force=True)["should_prompt"] is False

    _fake_gitee(monkeypatch, [_release("v1.3.0")])
    assert update_check.build_status(force=True)["should_prompt"] is False

    _fake_gitee(monkeypatch, [_release("v1.4.0")])
    assert update_check.build_status(force=True)["should_prompt"] is True


def test_忽略版本写进状态库且能撤销():
    assert update_check.save_ignored_version("v1.2.0") == "1.2.0"
    assert app_settings_store.load_settings().ignored_update_version == "1.2.0"
    assert update_check.load_ignored_version() == "1.2.0"

    assert update_check.save_ignored_version("") == ""
    assert update_check.load_ignored_version() == ""


def test_忽略版本不冲掉别的设置():
    app_settings_store.apply_chrome_mode("visible")
    update_check.save_ignored_version("1.2.0")

    settings = app_settings_store.load_settings()
    assert settings.chrome_mode == "visible"
    assert settings.ignored_update_version == "1.2.0"

    app_settings_store.apply_chrome_mode("")
    assert update_check.load_ignored_version() == "1.2.0"


def test_坏的忽略版本号按没忽略处理():
    assert update_check.save_ignored_version("不是版本") == ""
    assert update_check.load_ignored_version() == ""


# --------------------------------------------------------------------------- #
# 检测失败 / 缓存
# --------------------------------------------------------------------------- #


def test_检测失败不抛错只带人话(monkeypatch):
    def boom():
        raise update_check.UpdateCheckError("连不上 Gitee，稍后再试；不影响程序其它功能。")

    monkeypatch.setattr(update_check, "fetch_latest_release", boom)
    body = update_check.build_status(force=True)

    assert body["latest"] is None
    assert body["has_update"] is False
    assert body["should_prompt"] is False
    assert "连不上 Gitee" in body["error"]


def test_结果走缓存不重复打接口(monkeypatch):
    calls: list[int] = []

    def fake():
        calls.append(1)
        return update_check.Release(version="9.9.9", notes="", assets=())

    monkeypatch.setattr(update_check, "fetch_latest_release", fake)
    update_check.build_status(force=True)  # 先测一次，把缓存填上
    update_check.build_status()  # 缓存期内不再打
    update_check.build_status()

    assert len(calls) == 1

    update_check.build_status(force=True)  # force = 重新打
    assert len(calls) == 2


# --------------------------------------------------------------------------- #
# HTTP 接口
# --------------------------------------------------------------------------- #


def test_api_update_返回完整载荷(client, monkeypatch):
    _fake_gitee(monkeypatch, [_release("v9.9.9", notes="新东西")])
    body = client.get("/api/system/update", params={"force": True}).json()

    assert body["ok"] is True
    assert body["current"] == APP_VERSION
    assert body["version"] == APP_VERSION
    assert body["latest"] == "9.9.9"
    assert body["should_prompt"] is True
    assert body["gitee_url"].startswith("https://gitee.com/")
    assert body["github_url"].startswith("https://github.com/")
    assert "新东西" in body["notes"]


def test_api_update_忽略后再查不再提示(client, monkeypatch):
    _fake_gitee(monkeypatch, [_release("v9.9.9")])

    r = client.post("/api/system/update/ignore", json={"version": "9.9.9"})
    assert r.status_code == 200
    assert r.json()["ignored"] == "9.9.9"

    body = client.get("/api/system/update", params={"force": True}).json()
    assert body["has_update"] is True
    assert body["should_prompt"] is False
    assert body["ignored"] == "9.9.9"


def test_api_update_忽略空串是撤销(client, monkeypatch):
    _fake_gitee(monkeypatch, [_release("v9.9.9")])
    client.post("/api/system/update/ignore", json={"version": "9.9.9"})
    r = client.post("/api/system/update/ignore", json={"version": ""})

    assert r.json()["ignored"] is None
    assert client.get("/api/system/update", params={"force": True}).json()["should_prompt"] is True


def test_api_update_下载入口两个都有(client, monkeypatch):
    _fake_gitee(monkeypatch, [_release("v9.9.9")])
    body = client.get("/api/system/update", params={"force": True}).json()

    assert "gitee.com" in body["gitee_url"]
    assert "github.com" in body["github_url"]
    # 版本判断只认 Gitee，但两个下载页都要能跳
    assert body["gitee_url"].endswith("/releases")
    assert body["github_url"].endswith("/releases")


def test_api_update_检测失败也是_200(client, monkeypatch):
    def boom():
        raise update_check.UpdateCheckError("连不上 Gitee，稍后再试；不影响程序其它功能。")

    monkeypatch.setattr(update_check, "fetch_latest_release", boom)
    resp = client.get("/api/system/update", params={"force": True})

    assert resp.status_code == 200
    body = resp.json()
    assert body["latest"] is None
    assert body["should_prompt"] is False


def test_api_update_带上自更新所需字段(client, monkeypatch):
    _fake_gitee(monkeypatch, [_release("v9.9.9")])
    body = client.get("/api/system/update", params={"force": True}).json()

    assert body["zip_asset"] and body["zip_asset"]["url"].endswith(".zip")
    assert isinstance(body["assets"], list) and body["assets"]
    assert "can_self_update" in body
    assert "update_done" in body


# --------------------------------------------------------------------------- #
# 立即更新（POST /api/system/update/apply）
# --------------------------------------------------------------------------- #


def test_apply_源码模式回_409(client, monkeypatch):
    from boss_web import runtime

    monkeypatch.setattr(runtime, "is_frozen", lambda: False)
    _fake_gitee(monkeypatch, [_release("v9.9.9")])
    r = client.post("/api/system/update/apply", json={"version": ""})
    assert r.status_code == 409
    assert "源码" in r.json()["message"] or "开发" in r.json()["message"]


def test_apply_压缩包预览跑回_409(client, monkeypatch):
    from boss_web import runtime

    monkeypatch.setattr(runtime, "is_frozen", lambda: True)
    monkeypatch.setattr(runtime, "is_temp_run", lambda: True)
    _fake_gitee(monkeypatch, [_release("v9.9.9")])
    r = client.post("/api/system/update/apply", json={"version": ""})
    assert r.status_code == 409
    assert "压缩包" in r.json()["message"]


def test_apply_没有_zip_附件回_409(client, monkeypatch):
    from boss_web import runtime

    monkeypatch.setattr(runtime, "is_frozen", lambda: True)
    monkeypatch.setattr(runtime, "is_temp_run", lambda: False)

    def no_zip():
        return update_check.Release(
            version="9.9.9", notes="", assets=(update_check.Asset("a.exe", "https://x/a.exe"),)
        )

    monkeypatch.setattr(update_check, "fetch_latest_release", no_zip)
    r = client.post("/api/system/update/apply", json={"version": ""})
    assert r.status_code == 409
    assert "zip" in r.json()["message"].lower()


def test_apply_版本漂移回_409(client, monkeypatch):
    from boss_web import runtime

    monkeypatch.setattr(runtime, "is_frozen", lambda: True)
    monkeypatch.setattr(runtime, "is_temp_run", lambda: False)
    _fake_gitee(monkeypatch, [_release("v9.9.9")])
    r = client.post("/api/system/update/apply", json={"version": "1.0.0"})
    assert r.status_code == 409
    assert "版本已变化" in r.json()["message"]


def test_apply_正常情况启动更新任务(client, monkeypatch):
    from boss_web import runtime
    from boss_web.services import updater

    monkeypatch.setattr(runtime, "is_frozen", lambda: True)
    monkeypatch.setattr(runtime, "is_temp_run", lambda: False)
    _fake_gitee(monkeypatch, [_release("v9.9.9")])

    started: list[dict] = []
    monkeypatch.setattr(
        updater,
        "start_update_job",
        lambda **kw: started.append(kw),
    )

    r = client.post("/api/system/update/apply", json={"version": "9.9.9"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["version"] == "9.9.9"
    assert started and started[0]["zip_url"].endswith(".zip")
    assert started[0]["version"] == "9.9.9"


def test_health_带上当前版本号(client):
    body = client.get("/api/system/health").json()
    assert body["version"] == APP_VERSION


# --------------------------------------------------------------------------- #
# 控制台文案
# --------------------------------------------------------------------------- #


def test_控制台按行打印_已是最新():
    lines = update_check.format_console_lines(
        {"current": "1.1.0", "latest": "1.1.0", "has_update": False, "should_prompt": False}
    )
    assert len(lines) == 3
    assert "当前版本" in lines[0] and "v1.1.0" in lines[0]
    assert "发行版本" in lines[1] and "v1.1.0" in lines[1]
    assert "是否需要更新" in lines[2] and "已是最新" in lines[2]
    # 没更新就不塞下载行
    assert not any("下载" in line for line in lines)


def test_控制台按行打印_有新版本带下载行():
    lines = update_check.format_console_lines(
        {"current": "1.1.0", "latest": "1.2.0", "has_update": True, "should_prompt": True}
    )
    text = "\n".join(lines)
    assert "当前版本" in text and "v1.1.0" in text
    assert "发行版本" in text and "v1.2.0" in text
    assert "是否需要更新" in text and "有新版本" in text
    assert "Gitee 下载" in text and "gitee.com" in text
    assert "GitHub 下载" in text and "github.com" in text


def test_控制台按行打印_已忽略也说清楚():
    lines = update_check.format_console_lines(
        {
            "current": "1.1.0",
            "latest": "1.2.0",
            "has_update": True,
            "should_prompt": False,
            "ignored": "1.2.0",
        }
    )
    text = "\n".join(lines)
    assert "有新版本" in text
    assert "已忽略" in text


def test_控制台按行打印_检测失败也要打出当前版本():
    lines = update_check.format_console_lines(
        {"current": "1.1.0", "latest": None, "error": "连不上 Gitee，稍后再试；不影响程序其它功能。"}
    )
    text = "\n".join(lines)
    assert "v1.1.0" in text, "报问题时第一句就是「我这是哪一版」，不能因为网络挂了就不打"
    assert "——" in text
    assert "没测到" in text and "连不上 Gitee" in text
