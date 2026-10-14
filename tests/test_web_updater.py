"""自更新：bat 模板钉死「先验证后破坏」、下载/校验、进度协议、完成回执。

bat 的正文断言是**回归护栏**：有人把「验 zip」挪到「删文件」后面、或把 ``data``
目录删掉了，这里必须先红。不打真网，也不真起 cmd。
"""

from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from boss_web.services import updater


# --------------------------------------------------------------------------- #
# bat 模板：顺序与护栏
# --------------------------------------------------------------------------- #


def _bat() -> str:
    return updater.render_bat()


def test_bat_正文纯_ascii():
    """中文/非 ASCII 塞进 bat 极易因代码页出事；路径走环境变量，正文必须干净。"""
    body = _bat()
    body.encode("ascii")  # 不抛就是纯 ASCII
    # 盘符路径一个都不许写死——全靠 Popen(env=) 传进来
    assert ":\\" not in body.replace("%TEMP%\\", "").replace("%WORK_DIR%\\", "")


def test_bat_行尾是_crlf():
    body = _bat()
    assert "\r\n" in body
    assert body.replace("\r\n", "").find("\n") == -1, "混用 LF 会让 cmd 的 goto 失灵"


def test_bat_护栏顺序_先验_zip_再动文件():
    body = _bat()
    guard = body.index('if not exist "%ZIP_PATH%"')
    list_ok = body.index('tar -tf "%ZIP_PATH%"')
    extract = body.index('tar -xf "%ZIP_PATH%"')
    exe_check = body.index('if not exist "%EXTRACT_DIR%\\%EXE_NAME%"')
    lib_check = body.index('if not exist "%EXTRACT_DIR%\\_internal\\base_library.zip"')
    wipe = body.index('if /I not "%%D"=="data"')

    assert guard < list_ok < extract < exe_check < lib_check < wipe, (
        "zip 守卫必须排在删文件之前——zip 不在 / 解压不出 exe / 缺 base_library.zip，"
        "任何一种都绝不能碰安装目录"
    )


def test_bat_缺_base_library_就停在原地不换():
    """残缺包换上去 exe 根本起不来，比不更新还糟。

    真踩过：``base_library.zip``（PyInstaller 的整个 stdlib）被漏掉，更新「成功」
    之后程序再也打不开。所以护栏必须在 ``:wipe`` 之前，且报错要停住不往下走。
    """
    body = _bat()
    check = body.index('if not exist "%EXTRACT_DIR%\\_internal\\base_library.zip"')
    wipe = body.index('if /I not "%%D"=="data"')
    assert check < wipe, "缺文件必须在删安装目录之前拦住"

    block = body[check:wipe]
    assert ":fail" in block, "要报错给人看，不能静默跳过"
    assert "exit /b 1" in block, "报错之后不许继续往下换"


def test_bat_保留_data_目录():
    body = _bat()
    assert 'if /I not "%%D"=="data"' in body
    assert 'if /I not "%%F"=="data"' in body


def test_bat_等不到进程就_taskkill():
    body = _bat()
    assert "tasklist" in body
    assert "taskkill /PID %EXE_PID% /F" in body


def test_bat_成功才写_done_并同端口重启():
    body = _bat()
    assert "done.json" in body
    assert "--port %APP_PORT%" in body
    assert "--no-browser" in body


def test_bat_失败停在_pause_且不自删():
    body = _bat()
    fail_block = body.rsplit(":fail", 1)[1]  # 尾部那个 :fail 子程序
    assert "pause" in fail_block
    assert 'del /q "%ZIP_PATH%"' not in fail_block
    assert 'del /q "%~f0"' not in fail_block
    assert 'Your data folder "data" was NOT deleted.' in fail_block


def test_bat_成功后才清理现场():
    body = _bat()
    cleanup = body.index('del /q "%ZIP_PATH%"')
    done = body.index("done.json")
    assert done < cleanup


# --------------------------------------------------------------------------- #
# 下载
# --------------------------------------------------------------------------- #


class _FakeResp:
    def __init__(self, payload: bytes, *, length: int | None = None):
        self._payload = payload
        self.headers = {"Content-Length": str(len(payload) if length is None else length)}
        self.status_code = 200

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=1024):
        for i in range(0, len(self._payload), chunk_size):
            yield self._payload[i : i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_download_zip_先写_part_再落盘(tmp_path, monkeypatch):
    seen: list[int] = []
    monkeypatch.setattr(
        updater.requests,
        "get",
        lambda *a, **k: _FakeResp(b"PK\x03\x04" + b"x" * 200),
    )
    out = updater.download_zip(
        "https://gitee.com/x/BossAutoDelivery-1.2.0.zip",
        tmp_path,
        on_progress=seen.append,
    )
    assert out.name == "BossAutoDelivery-1.2.0.zip"
    assert out.read_bytes().startswith(b"PK\x03\x04")
    assert not out.with_name(out.name + ".part").exists()
    assert seen and seen[-1] == 100


def test_download_zip_网络失败抛可读错误(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise updater.requests.RequestException("connection reset")

    monkeypatch.setattr(updater.requests, "get", boom)
    with pytest.raises(updater.UpdateApplyError) as ei:
        updater.download_zip("https://gitee.com/x/a.zip", tmp_path)
    assert "下载更新包失败" in str(ei.value)


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #


def _make_zip(path: Path, names: list[str]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for n in names:
            zf.writestr(n, b"x")
    return path


def test_validate_zip_认扁平布局(tmp_path):
    p = _make_zip(tmp_path / "a.zip", ["BossAutoDelivery.exe", "_internal/foo.dll"])
    updater.validate_zip(p)  # 不抛


def test_validate_zip_认一层子目录布局(tmp_path):
    p = _make_zip(tmp_path / "a.zip", ["BossAutoDelivery/BossAutoDelivery.exe"])
    updater.validate_zip(p)


def test_validate_zip_拒_zip_slip(tmp_path):
    p = _make_zip(tmp_path / "a.zip", ["../evil.txt", "BossAutoDelivery.exe"])
    with pytest.raises(updater.UpdateApplyError) as ei:
        updater.validate_zip(p)
    assert "可疑路径" in str(ei.value)


def test_validate_zip_没有_exe_就不给过(tmp_path):
    p = _make_zip(tmp_path / "a.zip", ["readme.txt", "_internal/foo.dll"])
    with pytest.raises(updater.UpdateApplyError) as ei:
        updater.validate_zip(p)
    assert "BossAutoDelivery.exe" in str(ei.value)


def test_validate_zip_不是_zip_直接拒(tmp_path):
    p = tmp_path / "a.zip"
    p.write_bytes(b"not a zip at all")
    with pytest.raises(updater.UpdateApplyError):
        updater.validate_zip(p)


# --------------------------------------------------------------------------- #
# 进度协议
# --------------------------------------------------------------------------- #


def test_parse_progress_line_认_stage_percent_text():
    ev = updater.parse_progress_line("extract|40|expanding files")
    assert ev is not None
    assert ev.stage == "extract"
    assert ev.percent == 40
    # bat 写英文，界面给中文
    assert ev.text == "正在解压文件"


def test_parse_progress_line_坏行回_None():
    assert updater.parse_progress_line("") is None
    assert updater.parse_progress_line("nonsense") is None
    assert updater.parse_progress_line("||") is None


def test_parse_progress_line_percent_越界夹住():
    ev = updater.parse_progress_line("extract|999|x")
    assert ev.percent == 100
    ev = updater.parse_progress_line("extract|-5|x")
    assert ev.percent == 0


def test_parse_progress_line_error_带原始原因():
    ev = updater.parse_progress_line("error|100|update zip not found")
    assert ev.stage == "error"
    assert "update zip not found" in ev.text


# --------------------------------------------------------------------------- #
# done.json 回执
# --------------------------------------------------------------------------- #


def test_consume_done_marker_读一次即删(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "work_dir", lambda: tmp_path)
    updater.reset_done_marker_cache()
    (tmp_path / "done.json").write_text(
        json.dumps({"ok": True, "version": "1.2.0"}), encoding="utf-8"
    )

    got = updater.consume_done_marker()
    assert got and got["version"] == "1.2.0"
    assert not (tmp_path / "done.json").exists()
    # 文件没了，但进程内缓存还在，多端问都能拿到同一份
    assert updater.consume_done_marker()["version"] == "1.2.0"


def test_consume_done_marker_过期的不当真(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "work_dir", lambda: tmp_path)
    updater.reset_done_marker_cache()
    marker = tmp_path / "done.json"
    marker.write_text(json.dumps({"ok": True, "version": "9.9.9"}), encoding="utf-8")
    # 把 mtime 推到很久以前
    old = 1_000_000.0
    import os

    os.utime(marker, (old, old))

    assert updater.consume_done_marker() is None
    assert not marker.exists()


def test_consume_done_marker_没文件就_None(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "work_dir", lambda: tmp_path)
    updater.reset_done_marker_cache()
    assert updater.consume_done_marker() is None


# --------------------------------------------------------------------------- #
# spawn / 退出
# --------------------------------------------------------------------------- #


def test_spawn_bat_把路径塞进环境变量(tmp_path, monkeypatch):
    calls: list[dict] = []

    class FakePopen:
        def __init__(self, args, **kwargs):
            calls.append({"args": args, **kwargs})

    monkeypatch.setattr(updater.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(updater.sys, "platform", "win32")
    bat = tmp_path / "apply_update.bat"
    bat.write_text("rem noop")

    updater.spawn_bat(
        bat,
        env_extra={
            "INSTALL_DIR": r"D:\带空格 的目录",
            "ZIP_PATH": r"C:\Temp\a.zip",
            "EXTRACT_DIR": r"C:\Temp\extract",
            "EXE_PID": "4242",
            "APP_PORT": "8765",
            "NEW_VERSION": "1.2.0",
            "WORK_DIR": r"C:\Temp",
        },
    )
    assert calls, "spawn_bat 没真去起进程"
    env = calls[0]["env"]
    assert env["INSTALL_DIR"] == r"D:\带空格 的目录"
    assert env["EXE_PID"] == "4242"
    assert env["APP_PORT"] == "8765"
    # 路径里有中文/空格也没写进 bat 命令行，全靠 env
    assert "带空格" not in " ".join(calls[0]["args"])
    # 回归：曾经的 DETACHED_PROCESS 让 cmd 没有控制台，bat 第一句管道就永久挂死
    flags = calls[0]["creationflags"]
    assert flags & subprocess.CREATE_NO_WINDOW
    assert not flags & getattr(subprocess, "DETACHED_PROCESS", 0)


def test_request_exit_设_should_exit_并硬退(monkeypatch):
    class FakeServer:
        should_exit = False

    server = FakeServer()
    updater.bind_server(server)
    exited: list[int] = []
    monkeypatch.setattr(updater.os, "_exit", lambda code: exited.append(code))

    updater.request_exit(delay=0, hard_exit_after=0)
    import time

    for _ in range(50):
        if exited:
            break
        time.sleep(0.02)
    assert server.should_exit is True
    assert exited and exited[0] == 0


# --------------------------------------------------------------------------- #
# start_update_job 全链路
# --------------------------------------------------------------------------- #


def test_start_update_job_下载校验后才起_bat(tmp_path, monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(updater, "work_dir", lambda: tmp_path)
    monkeypatch.setattr(
        updater,
        "download_zip",
        lambda url, dest, **k: (order.append("download"), tmp_path / "a.zip")[1],
    )
    monkeypatch.setattr(
        updater, "validate_zip", lambda p: order.append("validate")
    )
    monkeypatch.setattr(
        updater, "write_bat", lambda d=None: (order.append("write"), tmp_path / "b.bat")[1]
    )
    monkeypatch.setattr(
        updater, "spawn_bat", lambda *a, **k: order.append("spawn")
    )
    monkeypatch.setattr(
        updater, "_tail_progress", lambda *a, **k: order.append("tail")
    )
    monkeypatch.setattr(updater, "publish", lambda ev: None)

    updater.start_update_job(version="1.2.0", zip_url="https://x/y.zip")
    import time

    for _ in range(50):
        if "spawn" in order:
            break
        time.sleep(0.02)

    assert order[:3] == ["download", "validate", "write"]
    assert "spawn" in order
    assert order.index("spawn") > order.index("validate"), "没校验完不许起 bat"


def test_start_update_job_失败推_error_事件(monkeypatch):
    events: list = []
    monkeypatch.setattr(updater, "publish", events.append)
    monkeypatch.setattr(
        updater,
        "download_zip",
        lambda *a, **k: (_ for _ in ()).throw(updater.UpdateApplyError("下载更新包失败：x")),
    )

    updater.start_update_job(version="1.2.0", zip_url="https://x/y.zip")
    import time

    for _ in range(50):
        if events:
            break
        time.sleep(0.02)

    assert events and events[-1].stage == "error"
    assert "下载更新包失败" in events[-1].text


def test_端口从_bind_server_带到_bat(monkeypatch, tmp_path):
    """bat 必须用**原来的端口**重启，否则浏览器那个标签页再也连不回来。"""
    monkeypatch.setattr(updater, "work_dir", lambda: tmp_path)
    monkeypatch.setattr(updater, "download_zip", lambda url, dest, **k: tmp_path / "a.zip")
    monkeypatch.setattr(updater, "validate_zip", lambda p: None)
    monkeypatch.setattr(updater, "write_bat", lambda d=None: tmp_path / "b.bat")

    seen: list[dict] = []
    monkeypatch.setattr(
        updater, "spawn_bat", lambda *a, **k: seen.append(k.get("env_extra") or {})
    )
    monkeypatch.setattr(updater, "_tail_progress", lambda *a, **k: None)
    monkeypatch.setattr(updater, "publish", lambda ev: None)

    class FakeServer:
        class config:
            port = 8765

    updater.bind_server(FakeServer())
    updater.start_update_job(version="1.2.0", zip_url="https://x/y.zip")
    import time

    for _ in range(50):
        if seen:
            break
        time.sleep(0.02)

    assert seen and seen[0]["APP_PORT"] == "8765"


# --------------------------------------------------------------------------- #
# 过程日志读取：给「系统设置 → 版本更新」就地看
# --------------------------------------------------------------------------- #


def test_读日志_文件都不存在不是错误(tmp_path, monkeypatch):
    """源码跑 / 从没更新过就没有日志，回空 + 一句话提示，**不许报错**。"""
    monkeypatch.setattr(updater, "work_dir", lambda: tmp_path)
    files, hint = updater.read_update_logs()
    assert [f["name"] for f in files] == ["update.log", "progress.log"]
    assert all(f["exists"] is False for f in files)
    assert all(f["content"] == "" for f in files)
    assert all(f["read_error"] is None for f in files)
    assert hint and "还没有更新日志" in hint


def test_读日志_小文件原样回(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "work_dir", lambda: tmp_path)
    (tmp_path / "update.log").write_bytes(b"line1\nline2\n")
    files, hint = updater.read_update_logs()
    assert hint is None, "有日志就不该再给空态提示"
    item = files[0]
    assert item["exists"] is True
    assert item["truncated"] is False
    assert item["content"] == "line1\nline2\n"
    assert item["size"] > 0
    assert item["mtime"] is not None


def test_读日志_超长只取尾部并丢掉残行(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "work_dir", lambda: tmp_path)
    # 前面半行 + 完整两行，砍完必须只剩完整的那两行
    (tmp_path / "update.log").write_bytes(b"PARTIAL" + b"\nFULL-A\n" + b"FULL-B\n")
    files, _ = updater.read_update_logs(tail_bytes=len(b"PARTIAL\nFULL-A\nFULL-B\n") - 5)
    item = files[0]
    assert item["truncated"] is True
    assert item["content"].startswith("FULL-A\n"), f"残行没丢干净：{item['content']!r}"
    assert "FULL-B" in item["content"]
    assert "PARTIAL" not in item["content"]


def test_读日志_读失败不抛且回原因(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "work_dir", lambda: tmp_path)
    (tmp_path / "update.log").write_text("x", encoding="utf-8")

    def boom(self):
        raise PermissionError("being used by another process")

    monkeypatch.setattr(Path, "read_bytes", boom)
    files, _ = updater.read_update_logs()
    item = files[0]
    assert item["exists"] is True
    assert item["content"] == ""
    assert item["read_error"] and "another process" in item["read_error"]


def test_peek_progress_不许吃掉_done_json(tmp_path, monkeypatch):
    """看一眼诊断不该把「更新完成」的回执吞掉——那是 snapshot_progress 的活。"""
    monkeypatch.setattr(updater, "work_dir", lambda: tmp_path)
    monkeypatch.setattr(updater, "done_marker_path", lambda: tmp_path / "done.json")
    (tmp_path / "done.json").write_text(
        json.dumps({"ok": True, "version": "1.2.0"}), encoding="utf-8"
    )
    updater.reset_done_marker_cache()

    payload = updater.peek_progress()
    assert payload.get("update_done") is None, "peek 不许给出完成回执"
    assert (tmp_path / "done.json").is_file(), "done.json 必须还在，等真正的消费方来拿"
