"""`boss_web` 入口层测试。

入口是最容易坏又最不容易被测到的一层：这里真 import 模块、真调 `main()`。
"""

from __future__ import annotations

import socket

import pytest

from boss_web import cli
from boss_web.services import update_check


@pytest.fixture(autouse=True)
def _fake_gitee(monkeypatch):
    """自检会打一次 Gitee；测试里必须换掉，否则跑一次测试就撞一次真网。"""
    update_check.reset_cache()
    monkeypatch.setattr(
        update_check,
        "fetch_latest_release",
        lambda: update_check.Release(version="1.1.0", notes="", assets=()),
    )
    yield
    update_check.reset_cache()


def _occupy(host: str = "127.0.0.1") -> socket.socket:
    """占住一个空闲端口，返回已 listen 的 socket。"""
    sock = socket.socket()
    sock.bind((host, 0))
    sock.listen(1)
    return sock


def test_module_imports_and_parser_builds():
    """验模块能 import，解析器默认参数正确。"""
    parser = cli.build_parser()
    args = parser.parse_args([])
    assert args.host == "127.0.0.1"
    assert args.port == 8787
    assert args.self_check is False


def test_parser_accepts_documented_flags():
    """验文档里写过的 CLI 开关都能解析。"""
    args = cli.build_parser().parse_args(
        ["--host", "0.0.0.0", "--port", "9000", "--no-browser", "--self-check", "--log-level", "debug"]
    )
    assert (args.host, args.port, args.no_browser, args.self_check) == (
        "0.0.0.0",
        9000,
        True,
        True,
    )
    assert args.log_level == "debug"


def test_pick_port_returns_preferred_when_free():
    """验首选端口空闲时原样返回。"""
    sock = _occupy()
    free = sock.getsockname()[1]
    sock.close()
    assert cli.pick_port("127.0.0.1", free) == free


def test_pick_port_skips_occupied_port():
    """上一个 exe 没关干净时，双击第二份不能直接启动失败。"""
    sock = _occupy()
    occupied = sock.getsockname()[1]
    try:
        picked = cli.pick_port("127.0.0.1", occupied, tries=5)
        assert picked != occupied
        assert occupied < picked <= occupied + 5
    finally:
        sock.close()


def test_pick_port_falls_back_to_system_when_all_taken():
    """验首选端口占满时让系统另派端口。"""
    socks = [_occupy() for _ in range(3)]
    first = socks[0].getsockname()[1]
    try:
        picked = cli.pick_port("127.0.0.1", first, tries=1)
        assert picked != first
    finally:
        for sock in socks:
            sock.close()


def test_self_check_passes_on_a_good_tree(capsys):
    """自检要能核对「开发态验不了」的东西：资源根、前端目录、库路径。"""
    assert cli.main(["--self-check"]) == 0
    out = capsys.readouterr().out
    assert "前端目录" in out
    assert "正常" in out
    assert "状态库" in out
    assert "自检结果" in out


def test_self_check_打印当前版本_发行版本_是否需要更新(capsys):
    """控制台必须能看到「我这是哪一版 / Gitee 发了多少 / 要不要更新」。"""
    assert cli.main(["--self-check"]) == 0
    out = capsys.readouterr().out
    assert "当前版本" in out
    assert "发行版本" in out
    assert "是否需要更新" in out


def test_self_check_有新版本时给出下载地址(monkeypatch, capsys):
    """验有新版本时自检输出 Gitee 与 GitHub 下载地址。"""
    monkeypatch.setattr(
        update_check,
        "fetch_latest_release",
        lambda: update_check.Release(version="9.9.9", notes="", assets=()),
    )
    assert cli.main(["--self-check"]) == 0
    out = capsys.readouterr().out
    assert "有新版本" in out
    assert "gitee.com" in out
    assert "github.com" in out


def test_self_check_reports_missing_static(monkeypatch, capsys):
    """前端资源没打进去时必须报失败——这正是「浏览器打开是空壳」的病根。"""
    monkeypatch.setattr(cli.C, "STATIC_DIR", cli.C.STATIC_DIR / "不存在的目录")
    assert cli.main(["--self-check"]) == 1
    assert "缺失" in capsys.readouterr().out


def test_self_check_does_not_start_server(monkeypatch):
    """自检路径不许把 uvicorn 拉起来——否则打包冒烟测试会挂住。"""
    started: list[int] = []
    monkeypatch.setattr(cli, "pick_port", lambda *_a, **_k: started.append(1) or 9999)
    assert cli.main(["--self-check"]) == 0
    assert started == []


def test_横幅先打版本对比占位_测完再补真结果(monkeypatch, capsys):
    """启动不能卡在网络检测上，所以横幅先占位，后台测完再打一行结果。

    同步跑 on_done 方便断言（真跑在守护线程里）。
    """
    monkeypatch.setattr(cli, "_setup_logging", lambda *_a, **_k: None)
    monkeypatch.setattr("uvicorn.Server.run", lambda self: None, raising=False)
    monkeypatch.setattr(cli, "pick_port", lambda *_a, **_k: 8799)

    monkeypatch.setattr(
        update_check,
        "schedule_startup_check",
        lambda on_done=None: on_done and on_done(update_check.build_status(force=True)),
    )

    assert cli.main(["--no-browser"]) == 0
    out = capsys.readouterr().out
    assert "版本对比" in out
    assert "正在查 Gitee" in out
    assert "当前版本" in out
    assert "发行版本" in out
    assert "是否需要更新" in out


def test_打印行格式固定三行(monkeypatch, capsys):
    """验版本对比控制台固定打印三行。"""
    from boss_web.services.update_check import format_console_lines

    lines = format_console_lines(
        {"current": "1.1.0", "latest": "1.2.0", "has_update": True, "should_prompt": True}
    )
    text = "\n".join(lines)
    assert "当前版本" in text and "v1.1.0" in text
    assert "发行版本" in text and "v1.2.0" in text
    assert "是否需要更新" in text and "有新版本" in text


def test_横幅先打登录状态占位_测完再补真结果(monkeypatch, capsys):
    """登录态自检也不能卡住启动：横幅先占位，后台打完 user_info 再补一行。"""
    import threading

    from boss_web.services import session_check

    monkeypatch.setattr(cli, "_setup_logging", lambda *_a, **_k: None)
    monkeypatch.setattr("uvicorn.Server.run", lambda self: None, raising=False)
    monkeypatch.setattr(cli, "pick_port", lambda *_a, **_k: 8799)
    monkeypatch.setattr(update_check, "schedule_startup_check", lambda on_done=None: None)

    def fake_schedule(on_done=None, **_kwargs):
        """同步回调登录态结果，返回已 set 的完成事件。"""
        if on_done is not None:
            on_done({"state": "absent", "message": "库里没有登录态", "error": None})
        done = threading.Event()
        done.set()
        return done

    monkeypatch.setattr(session_check, "schedule_startup_check", fake_schedule)

    assert cli.main(["--no-browser"]) == 0
    out = capsys.readouterr().out
    assert "登录状态" in out
    assert "正在校验" in out
    assert "未登录" in out


def test_开浏览器要等登录态自检出结果():
    """首屏会话卡片不能先渲染一份没验过的状态——测完再开，用户不用刷新。"""
    import threading
    import time
    import types

    opened: list[str] = []
    ready = threading.Event()

    server = types.SimpleNamespace(started=True, should_exit=False)

    def fake_open(url):
        """记录浏览器打开的 URL。"""
        opened.append(url)

    original = cli.webbrowser.open
    cli.webbrowser.open = fake_open
    try:
        t = threading.Thread(
            target=cli._open_browser_when_ready,
            args=(server, "http://127.0.0.1:8787", ready),
            daemon=True,
        )
        t.start()
        time.sleep(0.3)
        assert opened == []

        ready.set()
        t.join(timeout=2.0)
        assert opened == ["http://127.0.0.1:8787"]
    finally:
        cli.webbrowser.open = original
