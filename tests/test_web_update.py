"""版本检测：版本号比较、忽略水位线、挑最新、接口契约。

不打真网——`fetch_latest_release` 全部换掉。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from boss_web import __version__ as APP_VERSION
from boss_web.app import create_app
from boss_web.services import app_settings_store, update_check


@pytest.fixture
def client():
    """构造 FastAPI 测试客户端。"""
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _no_cache():
    """每个用例都从「没测过」开始，免得缓存串味。"""
    update_check.reset_cache()
    yield
    update_check.reset_cache()


def _release(tag: str, *, prerelease: bool = False, notes: str = "说明") -> dict:
    """构造一条发布记录。"""
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
    """把远端发布列表换成固定列表。"""
    monkeypatch.setattr(update_check, "fetch_latest_release", lambda: update_check.pick_latest(releases))


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
    """验版本号解析：能认的出数字元组，认不出的回 None。"""
    assert update_check.parse_version(raw) == want


@pytest.mark.parametrize(
    ("a", "b", "want"),
    [
        ("1.0.0", "1.0.1", -1),
        ("1.0.1", "1.0.0", 1),
        ("1.0.0", "v1.0.0", 0),
        ("1.0", "1.0.0", 0),
        ("v1.1.0", "1.0.0", 1),
        ("1.10.0", "1.9.0", 1),
        ("1.0.0", "1.0.0-rc1", 0),
    ],
)
def test_compare_versions(a, b, want):
    """验版本号按数值比较，预发布后缀不参与。"""
    assert update_check.compare_versions(a, b) == want


def test_pick_latest_取最高正式版(monkeypatch):
    """验从列表里挑最高正式版。"""
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
    """验跳过预发布版本。"""
    latest = update_check.pick_latest(
        [
            _release("v2.0.0-rc1", prerelease=True),
            _release("v1.5.0"),
        ]
    )
    assert latest.version == "1.5.0"


def test_pick_latest_跳过认不出的_tag():
    """验跳过认不出的 tag。"""
    latest = update_check.pick_latest([_release("nightly"), _release("v1.1.0")])
    assert latest.version == "1.1.0"


def test_pick_latest_没有正式版时回_None():
    """验没有正式版时回 None。"""
    assert update_check.pick_latest([]) is None
    assert update_check.pick_latest([_release("v2.0.0", prerelease=True)]) is None


def test_pick_latest_发行说明截断():
    """验发行说明超长会截断。"""
    latest = update_check.pick_latest([_release("v1.1.0", notes="x" * 5000)])
    assert len(latest.notes) <= update_check.NOTES_LIMIT + 5
    assert latest.notes.endswith("…")


def test_pick_latest_带上资产链接():
    """验带上资产下载链接。"""
    latest = update_check.pick_latest([_release("v1.1.0")])
    assert any(a.url.endswith("BossAutoDelivery.exe") for a in latest.assets)
    assert any(a.name.endswith(".zip") for a in latest.assets)


def test_pick_zip_asset_优先叫_BossAutoDelivery_的包():
    """验优先挑叫 BossAutoDelivery 的 zip。"""
    assets = (
        update_check.Asset(name="notes.txt", url="https://x/notes.txt"),
        update_check.Asset(name="随便.zip", url="https://x/随便.zip"),
        update_check.Asset(name="BossAutoDelivery-1.1.0.zip", url="https://x/BossAutoDelivery-1.1.0.zip"),
    )
    picked = update_check.pick_zip_asset(assets)
    assert picked is not None
    assert picked.name.startswith("BossAutoDelivery")


def test_pick_zip_asset_名字空看_url_尾段():
    """验资产名为空时看 url 尾段。"""
    assets = (update_check.Asset(name="", url="https://x/BossAutoDelivery-1.1.0.zip?token=1"),)
    picked = update_check.pick_zip_asset(assets)
    assert picked is not None
    assert picked.url.startswith("https://x/BossAutoDelivery-1.1.0.zip")


def test_pick_zip_asset_没有_zip_就回_None():
    """验没有 zip 时回 None。"""
    assert update_check.pick_zip_asset((update_check.Asset(name="a.exe", url="https://x/a.exe"),)) is None
    assert update_check.pick_zip_asset(()) is None


def test_can_self_update_源码跑为假(monkeypatch):
    """验源码跑不能自更新。"""
    from boss_web import runtime

    monkeypatch.setattr(runtime, "is_frozen", lambda: False)
    monkeypatch.setattr(runtime, "is_temp_run", lambda: False)
    assert update_check.can_self_update() is False


def test_can_self_update_压缩包预览跑也为假(monkeypatch):
    """验压缩包预览跑不能自更新。"""
    from boss_web import runtime

    monkeypatch.setattr(runtime, "is_frozen", lambda: True)
    monkeypatch.setattr(runtime, "is_temp_run", lambda: True)
    assert update_check.can_self_update() is False


def test_can_self_update_装在本地的_exe_为真(monkeypatch):
    """验装在本地的 exe 可自更新。"""
    from boss_web import runtime

    monkeypatch.setattr(runtime, "is_frozen", lambda: True)
    monkeypatch.setattr(runtime, "is_temp_run", lambda: False)
    assert update_check.can_self_update() is True


def test_有新版本就该提示(monkeypatch):
    """验有新版本时该提示。"""
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
    """验版本一样不提示。"""
    _fake_gitee(monkeypatch, [_release(f"v{APP_VERSION}")])
    body = update_check.build_status(force=True)

    assert body["has_update"] is False
    assert body["should_prompt"] is False


def test_本地比远端新也不提示(monkeypatch):
    """验本地比远端新也不提示。"""
    _fake_gitee(monkeypatch, [_release("v1.0.0")])
    monkeypatch.setattr(update_check, "APP_VERSION", "9.9.9")
    body = update_check.build_status(force=True)

    assert body["has_update"] is False
    assert body["should_prompt"] is False


def test_忽略之后这一版不再提示(monkeypatch):
    """验忽略之后这一版不再提示。"""
    _fake_gitee(monkeypatch, [_release("v9.9.9")])
    update_check.save_ignored_version("9.9.9")
    body = update_check.build_status(force=True)

    assert body["has_update"] is True, "「有更新」仍然成立，只是不弹窗"
    assert body["should_prompt"] is False
    assert body["ignored"] == "9.9.9"


def test_忽略之后来了更新的版本照样提示(monkeypatch):
    """验忽略是水位线，更新的版本照样提示。"""
    update_check.save_ignored_version("9.9.9")
    _fake_gitee(monkeypatch, [_release("v10.0.0")])
    body = update_check.build_status(force=True)

    assert body["latest"] == "10.0.0"
    assert body["should_prompt"] is True


def test_连续忽略多个版本只留最高水位(monkeypatch):
    """验连续忽略只留最高水位。"""
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
    """验忽略版本写进状态库且能撤销。"""
    assert update_check.save_ignored_version("v1.2.0") == "1.2.0"
    assert app_settings_store.load_settings().ignored_update_version == "1.2.0"
    assert update_check.load_ignored_version() == "1.2.0"

    assert update_check.save_ignored_version("") == ""
    assert update_check.load_ignored_version() == ""


def test_忽略版本不冲掉别的设置():
    """验忽略版本不冲掉别的设置。"""
    app_settings_store.apply_chrome_mode("visible")
    update_check.save_ignored_version("1.2.0")

    settings = app_settings_store.load_settings()
    assert settings.chrome_mode == "visible"
    assert settings.ignored_update_version == "1.2.0"

    app_settings_store.apply_chrome_mode("")
    assert update_check.load_ignored_version() == "1.2.0"


def test_坏的忽略版本号按没忽略处理():
    """验坏的忽略版本号按没忽略处理。"""
    assert update_check.save_ignored_version("不是版本") == ""
    assert update_check.load_ignored_version() == ""


def test_检测失败不抛错只带人话(monkeypatch):
    """验检测失败不抛错只带人话。"""
    def boom():
        """抛检测错误。"""
        raise update_check.UpdateCheckError("连不上 Gitee，稍后再试；不影响程序其它功能。")

    monkeypatch.setattr(update_check, "fetch_latest_release", boom)
    body = update_check.build_status(force=True)

    assert body["latest"] is None
    assert body["has_update"] is False
    assert body["should_prompt"] is False
    assert "连不上 Gitee" in body["error"]


def test_结果走缓存不重复打接口(monkeypatch):
    """验结果走缓存不重复打接口。"""
    calls: list[int] = []

    def fake():
        """计数并回固定版本。"""
        calls.append(1)
        return update_check.Release(version="9.9.9", notes="", assets=())

    monkeypatch.setattr(update_check, "fetch_latest_release", fake)
    update_check.build_status(force=True)
    update_check.build_status()
    update_check.build_status()

    assert len(calls) == 1

    update_check.build_status(force=True)
    assert len(calls) == 2


def test_api_update_返回完整载荷(client, monkeypatch):
    """验接口返回完整载荷。"""
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
    """验忽略后再查不再提示。"""
    _fake_gitee(monkeypatch, [_release("v9.9.9")])

    r = client.post("/api/system/update/ignore", json={"version": "9.9.9"})
    assert r.status_code == 200
    assert r.json()["ignored"] == "9.9.9"

    body = client.get("/api/system/update", params={"force": True}).json()
    assert body["has_update"] is True
    assert body["should_prompt"] is False
    assert body["ignored"] == "9.9.9"


def test_api_update_忽略空串是撤销(client, monkeypatch):
    """验忽略空串是撤销。"""
    _fake_gitee(monkeypatch, [_release("v9.9.9")])
    client.post("/api/system/update/ignore", json={"version": "9.9.9"})
    r = client.post("/api/system/update/ignore", json={"version": ""})

    assert r.json()["ignored"] is None
    assert client.get("/api/system/update", params={"force": True}).json()["should_prompt"] is True


def test_api_update_下载入口两个都有(client, monkeypatch):
    """验两个下载入口都给出。"""
    _fake_gitee(monkeypatch, [_release("v9.9.9")])
    body = client.get("/api/system/update", params={"force": True}).json()

    assert "gitee.com" in body["gitee_url"]
    assert "github.com" in body["github_url"]
    assert body["gitee_url"].endswith("/releases")
    assert body["github_url"].endswith("/releases")


def test_api_update_检测失败也是_200(client, monkeypatch):
    """验检测失败也是 200。"""
    def boom():
        """抛检测错误。"""
        raise update_check.UpdateCheckError("连不上 Gitee，稍后再试；不影响程序其它功能。")

    monkeypatch.setattr(update_check, "fetch_latest_release", boom)
    resp = client.get("/api/system/update", params={"force": True})

    assert resp.status_code == 200
    body = resp.json()
    assert body["latest"] is None
    assert body["should_prompt"] is False


def test_api_update_带上自更新所需字段(client, monkeypatch):
    """验带上自更新所需字段。"""
    _fake_gitee(monkeypatch, [_release("v9.9.9")])
    body = client.get("/api/system/update", params={"force": True}).json()

    assert body["zip_asset"] and body["zip_asset"]["url"].endswith(".zip")
    assert isinstance(body["assets"], list) and body["assets"]
    assert "can_self_update" in body
    assert "update_done" in body


def test_apply_源码模式回_409(client, monkeypatch):
    """验源码模式回 409。"""
    from boss_web import runtime

    monkeypatch.setattr(runtime, "is_frozen", lambda: False)
    _fake_gitee(monkeypatch, [_release("v9.9.9")])
    r = client.post("/api/system/update/apply", json={"version": ""})
    assert r.status_code == 409
    assert "源码" in r.json()["message"] or "开发" in r.json()["message"]


def test_apply_压缩包预览跑回_409(client, monkeypatch):
    """验压缩包预览跑回 409。"""
    from boss_web import runtime

    monkeypatch.setattr(runtime, "is_frozen", lambda: True)
    monkeypatch.setattr(runtime, "is_temp_run", lambda: True)
    _fake_gitee(monkeypatch, [_release("v9.9.9")])
    r = client.post("/api/system/update/apply", json={"version": ""})
    assert r.status_code == 409
    assert "压缩包" in r.json()["message"]


def test_apply_没有_zip_附件回_409(client, monkeypatch):
    """验没有 zip 附件回 409。"""
    from boss_web import runtime

    monkeypatch.setattr(runtime, "is_frozen", lambda: True)
    monkeypatch.setattr(runtime, "is_temp_run", lambda: False)

    def no_zip():
        """回没有 zip 的发布。"""
        return update_check.Release(
            version="9.9.9", notes="", assets=(update_check.Asset("a.exe", "https://x/a.exe"),)
        )

    monkeypatch.setattr(update_check, "fetch_latest_release", no_zip)
    r = client.post("/api/system/update/apply", json={"version": ""})
    assert r.status_code == 409
    assert "zip" in r.json()["message"].lower()


def test_apply_版本漂移回_409(client, monkeypatch):
    """验版本漂移回 409。"""
    from boss_web import runtime

    monkeypatch.setattr(runtime, "is_frozen", lambda: True)
    monkeypatch.setattr(runtime, "is_temp_run", lambda: False)
    _fake_gitee(monkeypatch, [_release("v9.9.9")])
    r = client.post("/api/system/update/apply", json={"version": "1.0.0"})
    assert r.status_code == 409
    assert "版本已变化" in r.json()["message"]


def test_apply_正常情况启动更新任务(client, monkeypatch):
    """验正常情况启动更新任务。"""
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
    """验 health 带上当前版本号。"""
    body = client.get("/api/system/health").json()
    assert body["version"] == APP_VERSION


def test_控制台按行打印_已是最新():
    """验已是最新时的控制台文案。"""
    lines = update_check.format_console_lines(
        {"current": "1.1.0", "latest": "1.1.0", "has_update": False, "should_prompt": False}
    )
    assert len(lines) == 3
    assert "当前版本" in lines[0] and "v1.1.0" in lines[0]
    assert "发行版本" in lines[1] and "v1.1.0" in lines[1]
    assert "是否需要更新" in lines[2] and "已是最新" in lines[2]
    assert not any("下载" in line for line in lines)


def test_控制台按行打印_有新版本带下载行():
    """验有新版本时带下载行。"""
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
    """验已忽略时也说清楚。"""
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
    """验检测失败也要打出当前版本。"""
    lines = update_check.format_console_lines(
        {"current": "1.1.0", "latest": None, "error": "连不上 Gitee，稍后再试；不影响程序其它功能。"}
    )
    text = "\n".join(lines)
    assert "v1.1.0" in text, "报问题时第一句就是「我这是哪一版」，不能因为网络挂了就不打"
    assert "——" in text
    assert "没测到" in text and "连不上 Gitee" in text


def test_日志接口_没有文件也_200_并带诊断(client, monkeypatch):
    """验日志接口没文件也 200 并带诊断。"""
    from boss_web.services import updater

    monkeypatch.setattr(updater, "work_dir", lambda: __import__("pathlib").Path("/no/such/dir-xyz"))
    resp = client.get("/api/system/update/log")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    names = [f["name"] for f in body["files"]]
    assert names == ["update.log", "progress.log"]
    assert all(f["exists"] is False for f in body["files"])
    assert body["hint"], "空态要有一句话"
    d = body["diagnostics"]
    for key in (
        "current",
        "frozen",
        "temp_run",
        "install_mode",
        "install_mode_label",
        "install_dir",
        "data_dir",
        "checked_at",
        "error",
        "can_self_update",
        "last_stage",
        "last_stage_text",
    ):
        assert key in d, f"diagnostics 缺 {key}"
    assert d["current"] == APP_VERSION.lstrip("vV")


def test_日志接口_有文件就带回正文(client, monkeypatch, tmp_path):
    """验日志接口有文件就带回正文。"""
    from boss_web.services import updater

    monkeypatch.setattr(updater, "work_dir", lambda: tmp_path)
    (tmp_path / "update.log").write_text("=== update start ===\nFAIL extract failed\n", encoding="utf-8")
    resp = client.get("/api/system/update/log")
    assert resp.status_code == 200
    files = {f["name"]: f for f in resp.json()["files"]}
    assert "FAIL extract failed" in files["update.log"]["content"]


def test_日志接口_安装方式三种映射(client, monkeypatch):
    """验安装方式三种映射。"""
    from boss_web import runtime

    monkeypatch.setattr(runtime, "is_frozen", lambda: False)
    monkeypatch.setattr(runtime, "is_temp_run", lambda: False)
    assert client.get("/api/system/update/log").json()["diagnostics"]["install_mode"] == "source"

    monkeypatch.setattr(runtime, "is_frozen", lambda: True)
    monkeypatch.setattr(runtime, "is_temp_run", lambda: True)
    assert client.get("/api/system/update/log").json()["diagnostics"]["install_mode"] == "temp_zip"

    monkeypatch.setattr(runtime, "is_frozen", lambda: True)
    monkeypatch.setattr(runtime, "is_temp_run", lambda: False)
    body = client.get("/api/system/update/log").json()["diagnostics"]
    assert body["install_mode"] == "installed"
    assert "可自动更新" in body["install_mode_label"]


def test_日志接口_诊断不许打_Gitee(client, monkeypatch):
    """验诊断不许打网络。"""
    def boom():
        """断言不许打网络。"""
        raise AssertionError("诊断接口不许打 Gitee")

    monkeypatch.setattr(update_check, "fetch_latest_release", boom)
    resp = client.get("/api/system/update/log")
    assert resp.status_code == 200
    assert resp.json()["diagnostics"]["latest"] is None


def test_cached_snapshot_只读缓存(monkeypatch):
    """验 cached_snapshot 只读缓存。"""
    def boom():
        """断言不许打网络。"""
        raise AssertionError("cached_snapshot 不许打网络")

    monkeypatch.setattr(update_check, "fetch_latest_release", boom)
    snap = update_check.cached_snapshot()
    assert snap["ok"] is True
    assert snap["latest"] is None
    assert snap["current"] == APP_VERSION.lstrip("vV")


def test_周期检测_force_绕过缓存且每轮一次(monkeypatch):
    """验周期检测 force 绕过缓存且每轮一次。"""
    calls = []

    def fake():
        """计数并回固定版本。"""
        calls.append(1)
        return update_check.pick_latest([_release("v1.9.9")])

    monkeypatch.setattr(update_check, "fetch_latest_release", fake)
    update_check.refresh(force=True)
    update_check.refresh(force=True)
    assert len(calls) == 2, "周期检测每轮都要真测一次，不许吃缓存"


def test_周期检测_起的是守护线程(monkeypatch):
    """验周期检测起的是守护线程。"""
    import threading

    seen = {}
    real_thread = threading.Thread

    def spy(*args, **kwargs):
        """记录线程参数并返回空线程。"""
        seen.update(kwargs)
        return real_thread(target=lambda: None, daemon=True)

    monkeypatch.setattr(threading, "Thread", spy)
    update_check.schedule_periodic_check(interval=0.01)
    assert seen.get("name") == "update-check-periodic"
    assert seen.get("daemon") is True
