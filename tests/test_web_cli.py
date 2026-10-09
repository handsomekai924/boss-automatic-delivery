"""``boss_web`` 入口层测试。

存在的理由和 ``tests/test_cli.py`` 一样：入口是最容易坏、又最不容易被测到的一层
（那次的教训是一个不存在的名字被 import 进来，整套测试却全绿）。这里真的
import 模块、真的调 ``main()``。
"""

from __future__ import annotations

import socket

import pytest

from boss_web import cli


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
