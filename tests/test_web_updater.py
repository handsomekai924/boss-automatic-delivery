"""自更新：bat 模板钉死「先验证后破坏」、下载/校验、进度协议、完成回执。

bat 的正文断言是回归护栏：有人把「验 zip」挪到「删文件」后面、或把 `data`
目录删掉了，这里必须先红。不打真网，也不真起 cmd。
"""

from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from boss_web.services import updater


def _bat() -> str:
    """取出渲染好的 bat 模板正文。"""
    return updater.render_bat()


def test_bat_正文纯_ascii():
    """中文/非 ASCII 塞进 bat 极易因代码页出事；路径走环境变量，正文必须干净。"""
    body = _bat()
    body.encode("ascii")
    assert ":\\" not in body.replace("%TEMP%\\", "").replace("%WORK_DIR%\\", "")


def test_bat_行尾是_crlf():
    """bat 行尾必须是 CRLF。"""
    body = _bat()
    assert "\r\n" in body
    assert body.replace("\r\n", "").find("\n") == -1, "混用 LF 会让 cmd 的 goto 失灵"


def test_bat_护栏顺序_先验_zip_再动文件():
    """zip 守卫必须排在删文件之前。"""
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
    """残缺包换上去 exe 根本起不来；缺 base_library.zip 必须在删目录前拦住。"""
    body = _bat()
    check = body.index('if not exist "%EXTRACT_DIR%\\_internal\\base_library.zip"')
    wipe = body.index('if /I not "%%D"=="data"')
    assert check < wipe, "缺文件必须在删安装目录之前拦住"

    block = body[check:wipe]
    assert ":fail" in block, "要报错给人看，不能静默跳过"
    assert "exit /b 1" in block, "报错之后不许继续往下换"


def test_bat_保留_data_目录():
    """更新时保留 `data` 目录。"""
    body = _bat()
    assert 'if /I not "%%D"=="data"' in body
    assert 'if /I not "%%F"=="data"' in body


def test_bat_等不到进程就_taskkill():
    """等不到进程退出就 taskkill。"""
    body = _bat()
    assert "tasklist" in body
    assert "taskkill /PID %EXE_PID% /F" in body


def test_bat_成功才写_done_并同端口重启():
    """成功才写 done.json 并按原端口重启。"""
    body = _bat()
    assert "done.json" in body
    assert "--port %APP_PORT%" in body
    assert "--no-browser" in body


def test_bat_失败停在_pause_且不自删():
    """失败停在 pause，且不自删安装包与脚本。"""
    body = _bat()
    fail_block = body.rsplit(":fail", 1)[1]
    assert "pause" in fail_block
    assert 'del /q "%ZIP_PATH%"' not in fail_block
    assert 'del /q "%~f0"' not in fail_block
    assert 'Your data folder "data" was NOT deleted.' in fail_block


def test_bat_成功后才清理现场():
    """写完 done.json 之后才清理现场。"""
    body = _bat()
    cleanup = body.index('del /q "%ZIP_PATH%"')
    done = body.index("done.json")
    assert done < cleanup


class _FakeResp:
    """假下载响应。"""

    def __init__(self, payload: bytes, *, length: int | None = None):
        """初始化。"""
        self._payload = payload
        self.headers = {"Content-Length": str(len(payload) if length is None else length)}
        self.status_code = 200

    def raise_for_status(self):
        """无操作占位。"""
        return None

    def iter_content(self, chunk_size=1024):
        """按块吐出载荷。"""
        for i in range(0, len(self._payload), chunk_size):
            yield self._payload[i : i + chunk_size]

    def __enter__(self):
        """进入上下文。"""
        return self

    def __exit__(self, *exc):
        """退出上下文。"""
        return False


def test_download_zip_先写_part_再落盘(tmp_path, monkeypatch):
    """下载先写 `.part` 再落盘，进度收到 100。"""
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
    """网络失败抛可读的 `UpdateApplyError`。"""

    def boom(*a, **k):
        """模拟网络异常。"""
        raise updater.requests.RequestException("connection reset")

    monkeypatch.setattr(updater.requests, "get", boom)
    with pytest.raises(updater.UpdateApplyError) as ei:
        updater.download_zip("https://gitee.com/x/a.zip", tmp_path)
    assert "下载更新包失败" in str(ei.value)


def _make_zip(path: Path, names: list[str]) -> Path:
    """按给定条目名造一个 zip。"""
    with zipfile.ZipFile(path, "w") as zf:
        for n in names:
            zf.writestr(n, b"x")
    return path


def test_validate_zip_认扁平布局(tmp_path):
    """扁平布局校验通过。"""
    p = _make_zip(tmp_path / "a.zip", ["BossAutoDelivery.exe", "_internal/foo.dll"])
    updater.validate_zip(p)


def test_validate_zip_认一层子目录布局(tmp_path):
    """一层子目录布局校验通过。"""
    p = _make_zip(tmp_path / "a.zip", ["BossAutoDelivery/BossAutoDelivery.exe"])
    updater.validate_zip(p)


def test_validate_zip_拒_zip_slip(tmp_path):
    """拒绝 zip slip 路径。"""
    p = _make_zip(tmp_path / "a.zip", ["../evil.txt", "BossAutoDelivery.exe"])
    with pytest.raises(updater.UpdateApplyError) as ei:
        updater.validate_zip(p)
    assert "可疑路径" in str(ei.value)


def test_validate_zip_没有_exe_就不给过(tmp_path):
    """没有 exe 不给过。"""
    p = _make_zip(tmp_path / "a.zip", ["readme.txt", "_internal/foo.dll"])
    with pytest.raises(updater.UpdateApplyError) as ei:
        updater.validate_zip(p)
    assert "BossAutoDelivery.exe" in str(ei.value)


def test_validate_zip_不是_zip_直接拒(tmp_path):
    """不是 zip 直接拒。"""
    p = tmp_path / "a.zip"
    p.write_bytes(b"not a zip at all")
    with pytest.raises(updater.UpdateApplyError):
        updater.validate_zip(p)


def test_parse_progress_line_认_stage_percent_text():
    """识别 `stage|percent|text` 进度行。"""
    ev = updater.parse_progress_line("extract|40|expanding files")
    assert ev is not None
    assert ev.stage == "extract"
    assert ev.percent == 40
    assert ev.text == "正在解压文件"


def test_parse_progress_line_坏行回_None():
    """坏进度行返回 `None`。"""
    assert updater.parse_progress_line("") is None
    assert updater.parse_progress_line("nonsense") is None
    assert updater.parse_progress_line("||") is None


def test_parse_progress_line_percent_越界夹住():
    """percent 越界夹到 0–100。"""
    ev = updater.parse_progress_line("extract|999|x")
    assert ev.percent == 100
    ev = updater.parse_progress_line("extract|-5|x")
    assert ev.percent == 0


def test_parse_progress_line_error_带原始原因():
    """error 行带上原始原因。"""
    ev = updater.parse_progress_line("error|100|update zip not found")
    assert ev.stage == "error"
    assert "update zip not found" in ev.text


def test_consume_done_marker_读一次即删(tmp_path, monkeypatch):
    """读一次即删，进程内缓存仍可再取。"""
    monkeypatch.setattr(updater, "work_dir", lambda: tmp_path)
    updater.reset_done_marker_cache()
    (tmp_path / "done.json").write_text(
        json.dumps({"ok": True, "version": "1.2.0"}), encoding="utf-8"
    )

    got = updater.consume_done_marker()
    assert got and got["version"] == "1.2.0"
    assert not (tmp_path / "done.json").exists()
    assert updater.consume_done_marker()["version"] == "1.2.0"


def test_consume_done_marker_过期的不当真(tmp_path, monkeypatch):
    """过期回执不当真。"""
    monkeypatch.setattr(updater, "work_dir", lambda: tmp_path)
    updater.reset_done_marker_cache()
    marker = tmp_path / "done.json"
    marker.write_text(json.dumps({"ok": True, "version": "9.9.9"}), encoding="utf-8")
    old = 1_000_000.0
    import os

    os.utime(marker, (old, old))

    assert updater.consume_done_marker() is None
    assert not marker.exists()


def test_consume_done_marker_没文件就_None(tmp_path, monkeypatch):
    """没文件返回 `None`。"""
    monkeypatch.setattr(updater, "work_dir", lambda: tmp_path)
    updater.reset_done_marker_cache()
    assert updater.consume_done_marker() is None


def test_spawn_bat_把路径塞进环境变量(tmp_path, monkeypatch):
    """路径走环境变量传入，命令行不带路径。"""
    calls: list[dict] = []

    class FakePopen:
        """假 `Popen`，只记调用。"""

        def __init__(self, args, **kwargs):
            """初始化。"""
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
    assert "带空格" not in " ".join(calls[0]["args"])
    flags = calls[0]["creationflags"]
    assert flags & subprocess.CREATE_NO_WINDOW
    assert not flags & getattr(subprocess, "DETACHED_PROCESS", 0)


def test_request_exit_设_should_exit_并硬退(monkeypatch):
    """`request_exit` 置位 `should_exit` 并硬退。"""

    class FakeServer:
        """假服务端，只带 `should_exit`。"""

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


def test_start_update_job_下载校验后才起_bat(tmp_path, monkeypatch):
    """下载、校验之后才起 bat。"""
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
    """失败推 error 事件。"""
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
    """bat 必须用原来的端口重启，否则浏览器那个标签页再也连不回来。"""
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
        """假服务端，只带端口配置。"""

        class config:
            """最小配置桩。"""

            port = 8765

    updater.bind_server(FakeServer())
    updater.start_update_job(version="1.2.0", zip_url="https://x/y.zip")
    import time

    for _ in range(50):
        if seen:
            break
        time.sleep(0.02)

    assert seen and seen[0]["APP_PORT"] == "8765"


def test_读日志_文件都不存在不是错误(tmp_path, monkeypatch):
    """源码跑 / 从没更新过就没有日志，回空 + 一句话提示，不许报错。"""
    monkeypatch.setattr(updater, "work_dir", lambda: tmp_path)
    files, hint = updater.read_update_logs()
    assert [f["name"] for f in files] == ["update.log", "progress.log"]
    assert all(f["exists"] is False for f in files)
    assert all(f["content"] == "" for f in files)
    assert all(f["read_error"] is None for f in files)
    assert hint and "还没有更新日志" in hint


def test_读日志_小文件原样回(tmp_path, monkeypatch):
    """小日志文件原样返回。"""
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
    """超长日志只取尾部并丢掉残行。"""
    monkeypatch.setattr(updater, "work_dir", lambda: tmp_path)
    (tmp_path / "update.log").write_bytes(b"PARTIAL" + b"\nFULL-A\n" + b"FULL-B\n")
    files, _ = updater.read_update_logs(tail_bytes=len(b"PARTIAL\nFULL-A\nFULL-B\n") - 5)
    item = files[0]
    assert item["truncated"] is True
    assert item["content"].startswith("FULL-A\n"), f"残行没丢干净：{item['content']!r}"
    assert "FULL-B" in item["content"]
    assert "PARTIAL" not in item["content"]


def test_读日志_读失败不抛且回原因(tmp_path, monkeypatch):
    """读失败不抛且把原因写进结果。"""
    monkeypatch.setattr(updater, "work_dir", lambda: tmp_path)
    (tmp_path / "update.log").write_text("x", encoding="utf-8")

    def boom(self):
        """模拟读文件失败。"""
        raise PermissionError("being used by another process")

    monkeypatch.setattr(Path, "read_bytes", boom)
    files, _ = updater.read_update_logs()
    item = files[0]
    assert item["exists"] is True
    assert item["content"] == ""
    assert item["read_error"] and "another process" in item["read_error"]


def test_peek_progress_不许吃掉_done_json(tmp_path, monkeypatch):
    """看一眼诊断不该把「更新完成」的回执吞掉——回执归状态接口一次性取用。"""
    monkeypatch.setattr(updater, "work_dir", lambda: tmp_path)
    monkeypatch.setattr(updater, "done_marker_path", lambda: tmp_path / "done.json")
    (tmp_path / "done.json").write_text(
        json.dumps({"ok": True, "version": "1.2.0"}), encoding="utf-8"
    )
    updater.reset_done_marker_cache()

    payload = updater.peek_progress()
    assert payload.get("update_done") is None, "peek 不许给出完成回执"
    assert (tmp_path / "done.json").is_file(), "done.json 必须还在，等真正的消费方来拿"
