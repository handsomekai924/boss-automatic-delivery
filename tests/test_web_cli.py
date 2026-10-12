"""``boss_web`` 入口层测试。

存在的理由和 ``tests/test_cli.py`` 一样：入口是最容易坏、又最不容易被测到的一层
（那次的教训是一个不存在的名字被 import 进来，整套测试却全绿）。这里真的
import 模块、真的调 ``main()``。
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
    sock = socket.socket()
    sock.bind((host, 0))
    sock.listen(1)
    return sock


# --------------------------------------------------------------------------- #
# 入口本身
# --------------------------------------------------------------------------- #


def test_module_imports_and_parser_builds():
    parser = cli.build_parser()
    args = parser.parse_args([])
    assert args.host == "127.0.0.1"
    assert args.port == 8787
    assert args.self_check is False


def test_parser_accepts_documented_flags():
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


# --------------------------------------------------------------------------- #
# 端口顺延
# --------------------------------------------------------------------------- #


def test_pick_port_returns_preferred_when_free():
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
    socks = [_occupy() for _ in range(3)]
    first = socks[0].getsockname()[1]
    try:
        # tries=1 只试第一个；它被占着，就该走「让系统派一个」
        picked = cli.pick_port("127.0.0.1", first, tries=1)
        assert picked != first
    finally:
        for sock in socks:
            sock.close()


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #


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
    """启动不能卡在网络检测上，所以横幅先占位，后台测完再打一行结果。"""
    monkeypatch.setattr(cli, "_setup_logging", lambda *_a, **_k: None)
    monkeypatch.setattr("uvicorn.Server.run", lambda self: None, raising=False)
    monkeypatch.setattr(cli, "pick_port", lambda *_a, **_k: 8799)

    # 同步跑 on_done，方便断言；真跑是守护线程里跑的
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
    from boss_web.services.update_check import format_console_lines

    lines = format_console_lines(
        {"current": "1.1.0", "latest": "1.2.0", "has_update": True, "should_prompt": True}
    )
    text = "\n".join(lines)
    assert "当前版本" in text and "v1.1.0" in text
    assert "发行版本" in text and "v1.2.0" in text
    assert "是否需要更新" in text and "有新版本" in text
