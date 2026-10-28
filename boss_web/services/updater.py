"""应用内自更新：下载 zip → 校验 → 交给 bat 停服/换文件/重启，全程推 WS 进度。

Python 只负责下载、校验、生成脚本、转发进度与退出；替换动作交给独立的
cmd 进程（Windows 上运行中的 exe 换不掉自己）。与 bat 的文件约定见
`notes/boss_web/services/updater.md`。

Attributes:
    UPDATE_TEMP_DIR_NAME: 更新工作目录名（挂在 %TEMP% 下）。
    MAX_ZIP_BYTES: 更新包大小上限。
    DOWNLOAD_TIMEOUT: `(连接超时, 读超时)`。
    DONE_MARKER_MAX_AGE: `done.json` 的有效期（秒）。
    STAGE_ZH: bat 的 stage → 界面中文。
    LOG_TAIL_BYTES: 诊断页默认读的尾部字节数。
    LOG_FILE_NAMES: 要展示的过程日志文件名。
    _last_event: 最近一次进度。
    _done_cache: 新进程的「更新完成」回执缓存。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests

from .. import __version__ as APP_VERSION

logger = logging.getLogger(__name__)

UPDATE_TEMP_DIR_NAME = "BossAutoDelivery-update"
MAX_ZIP_BYTES = 500 * 1024 * 1024
DOWNLOAD_TIMEOUT = (10.0, 300.0)
DONE_MARKER_MAX_AGE = 600.0

EXE_NAME = "BossAutoDelivery.exe"


class UpdateApplyError(Exception):
    """更新流程里可读的失败原因（界面直接摆这句话）。"""


@dataclass(frozen=True)
class ProgressEvent:
    """一条更新进度事件。

    Attributes:
        stage: `download` / `validate` / `extract` / `ready` / `stopping` /
            `swapping` / `done` / `error`。
        percent: 0-100 进度。
        text: 给界面看的中文说明。
        update_done: 自更新完成后的一次性回执；没有则 `None`。
    """

    stage: str
    percent: int
    text: str
    update_done: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        """转成给 WS / 接口用的字典。"""
        return {
            "stage": self.stage,
            "percent": self.percent,
            "text": self.text,
            "update_done": self.update_done,
        }


STAGE_ZH: dict[str, str] = {
    "download": "正在下载更新包",
    "validate": "正在校验更新包",
    "extract": "正在解压文件",
    "ready": "更新包已解压",
    "stopping": "正在关闭当前服务",
    "swapping": "正在替换程序文件",
    "done": "正在启动新版本",
    "error": "更新失败",
}

_lock = threading.RLock()
_last_event: ProgressEvent = ProgressEvent(stage="idle", percent=0, text="")
_done_cache: dict[str, Any] | None = None
_done_loaded = False


def publish(event: ProgressEvent) -> None:
    """线程安全广播一帧进度；更新工作线程里调，统一 WS 在事件循环里收。

    走统一任务中心广播（`update.progress`），前端一条 WS 就能看全。

    Args:
        event: 要广播的进度事件。
    """
    global _last_event
    with _lock:
        _last_event = event
    try:
        from .task_runtime import hub

        hub.broadcast("update.progress", event.to_payload(), task_type="update")
    except Exception:  # noqa: BLE001 - 广播失败不影响更新本身
        logger.debug("更新进度广播失败", exc_info=True)


def peek_progress() -> dict[str, Any]:
    """只看最近一次进度事件，不动 `done.json`。

    给诊断面板用：看一眼日志不该吃掉更新完成的回执；回执字段一律置空。
    """
    with _lock:
        payload = _last_event.to_payload()
    payload["update_done"] = None
    return payload


def parse_progress_line(line: str) -> ProgressEvent | None:
    """`stage|percent|text` → 事件；认不出回 `None`。

    Args:
        line: `progress.log` 里的一行。
    """
    text = (line or "").strip()
    if not text or text.count("|") < 2:
        return None
    stage, raw_percent, detail = text.split("|", 2)
    stage = stage.strip().lower()
    if not stage:
        return None
    try:
        percent = max(0, min(100, int(raw_percent.strip() or "0")))
    except ValueError:
        percent = 0
    zh = STAGE_ZH.get(stage)
    if stage == "error":
        shown = f"更新失败：{detail.strip()}" if detail.strip() else zh or "更新失败"
    else:
        shown = zh or detail.strip()
    return ProgressEvent(stage=stage, percent=percent, text=shown)


def work_dir() -> Path:
    """更新工作目录（挂在 %TEMP% 下）。"""
    base = os.environ.get("TEMP") or os.environ.get("TMP") or tempfile.gettempdir()
    return Path(base) / UPDATE_TEMP_DIR_NAME


def done_marker_path() -> Path:
    """`done.json` 的路径。"""
    return work_dir() / "done.json"


LOG_TAIL_BYTES = 64 * 1024
LOG_TAIL_MAX = 256 * 1024
LOG_FILE_NAMES = ("update.log", "progress.log")


def read_update_logs(*, tail_bytes: int = LOG_TAIL_BYTES) -> tuple[list[dict[str, Any]], str | None]:
    """读更新过程日志尾部，给网页直接看。

    文件不存在 / 读不出来都不是 500：前者回 `exists: False`，后者塞进
    `read_error`；超长只取尾部并丢掉残行。

    Args:
        tail_bytes: 每个文件最多读多少字节尾部。

    Returns:
        `(files, hint)`；`hint` 是给空态的一句话，没有就 `None`。
    """
    limit = max(1, int(tail_bytes or LOG_TAIL_BYTES))
    root = work_dir()
    files: list[dict[str, Any]] = []
    any_exists = False

    for name in LOG_FILE_NAMES:
        path = root / name
        item: dict[str, Any] = {
            "name": name,
            "path": str(path),
            "exists": False,
            "size": 0,
            "mtime": None,
            "truncated": False,
            "content": "",
            "read_error": None,
        }
        try:
            stat = path.stat()
        except FileNotFoundError:
            files.append(item)
            continue
        except OSError as exc:  # noqa: PERF203 - 目录都进不去也算「读不到」
            item["exists"] = True
            item["read_error"] = str(exc)
            files.append(item)
            any_exists = True
            continue

        item["exists"] = True
        item["size"] = int(stat.st_size)
        item["mtime"] = float(stat.st_mtime)
        any_exists = True

        try:
            raw = path.read_bytes()
        except OSError as exc:
            item["read_error"] = str(exc)
            files.append(item)
            continue

        if len(raw) > limit:
            raw = raw[-limit:]
            item["truncated"] = True
            nl = raw.find(b"\n")
            if 0 <= nl < len(raw) - 1:
                raw = raw[nl + 1 :]
        item["content"] = raw.decode("utf-8", errors="replace")
        files.append(item)

    hint = None
    if not any_exists:
        hint = "还没有更新日志。只有点过「立即更新」之后才会生成。"
    return files, hint


def consume_done_marker() -> dict[str, Any] | None:
    """读一次 `done.json` 并清掉文件；进程内缓存，多端问都能拿到同一份。

    超过 `DONE_MARKER_MAX_AGE` 的旧回执当垃圾丢掉，避免幽灵「更新完成」弹窗。
    """
    global _done_cache, _done_loaded
    with _lock:
        if _done_loaded:
            return dict(_done_cache) if _done_cache else None

    path = done_marker_path()
    data: dict[str, Any] | None = None
    try:
        if path.is_file():
            mtime = path.stat().st_mtime
            raw = json.loads(path.read_text(encoding="utf-8"))
            path.unlink()
            if (
                isinstance(raw, dict)
                and raw.get("ok")
                and (time.time() - mtime) <= DONE_MARKER_MAX_AGE
            ):
                data = raw
    except Exception:  # noqa: BLE001 - 回执坏了就当没有，不许拦住状态接口
        logger.warning("读更新完成标记失败", exc_info=True)
        data = None

    with _lock:
        _done_cache = data
        _done_loaded = True
        return dict(_done_cache) if _done_cache else None


def reset_done_marker_cache() -> None:
    """清掉完成回执缓存。给测试用。"""
    global _done_cache, _done_loaded
    with _lock:
        _done_cache = None
        _done_loaded = False


def download_zip(
    url: str,
    dest_dir: Path,
    *,
    expected_name: str = "",
    on_progress: Callable[[int], None] | None = None,
) -> Path:
    """流式下载更新包；失败抛 `UpdateApplyError`。

    先写 `.part` 再改名，半截文件不会被当成完整包。

    Args:
        url: 下载直链。
        dest_dir: 目标目录。
        expected_name: 期望文件名；空则从 URL 取。
        on_progress: 下载进度回调（0-100）。
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = expected_name.strip() or url.rstrip("/").rsplit("/", 1)[-1].split("?")[0] or "update.zip"
    if not name.lower().endswith(".zip"):
        name += ".zip"
    dest = dest_dir / name
    part = dest.with_name(dest.name + ".part")
    total = 0
    try:
        with requests.get(
            url,
            stream=True,
            timeout=DOWNLOAD_TIMEOUT,
            headers={"User-Agent": f"BossAutoDelivery/{APP_VERSION}"},
        ) as resp:
            resp.raise_for_status()
            length = int(resp.headers.get("Content-Length") or 0)
            if length > MAX_ZIP_BYTES:
                raise UpdateApplyError("更新包出奇地大，已取消下载")
            with open(part, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_ZIP_BYTES:
                        raise UpdateApplyError("更新包出奇地大，已取消下载")
                    fh.write(chunk)
                    if on_progress and length > 0:
                        on_progress(min(99, int(total * 100 / length)))
    except UpdateApplyError:
        part.unlink(missing_ok=True)
        raise
    except requests.RequestException as exc:
        part.unlink(missing_ok=True)
        raise UpdateApplyError(f"下载更新包失败：{exc}") from exc
    except Exception:
        part.unlink(missing_ok=True)
        raise

    if total <= 0:
        part.unlink(missing_ok=True)
        raise UpdateApplyError("下载到的更新包是空的")
    os.replace(part, dest)
    if on_progress:
        on_progress(100)
    return dest


def validate_zip(path: Path) -> None:
    """校验更新包：真 zip、无 zip-slip 路径、且解得出 `BossAutoDelivery.exe`。

    Args:
        path: 待校验的 zip 路径。
    """
    if not path.is_file():
        raise UpdateApplyError("更新包不存在")
    if path.stat().st_size <= 0:
        raise UpdateApplyError("更新包是空文件")
    if not zipfile.is_zipfile(path):
        raise UpdateApplyError("更新包不是有效的 zip 文件")

    with zipfile.ZipFile(path) as zf:
        names = [n.replace("\\", "/") for n in zf.namelist()]
    for n in names:
        parts = n.split("/")
        if ".." in parts or n.startswith("/") or (len(n) > 1 and n[1] == ":"):
            raise UpdateApplyError("更新包里有可疑路径，已拒绝安装")
    has_exe = (EXE_NAME in names) or any(
        n.count("/") == 1 and n.endswith("/" + EXE_NAME) for n in names
    )
    if not has_exe:
        raise UpdateApplyError(f"更新包里找不到 {EXE_NAME}，已取消更新")


_BAT_TEMPLATE = r"""@echo off
chcp 65001 >nul
setlocal EnableExtensions
title BossAutoDelivery Update
if not defined WORK_DIR set "WORK_DIR=%TEMP%\BossAutoDelivery-update"
set "LOG=%WORK_DIR%\update.log"
set "PROG=%WORK_DIR%\progress.log"
set "EXE_NAME=BossAutoDelivery.exe"
call :log === update start ===

if not defined INSTALL_DIR (
  call :fail "INSTALL_DIR is not set"
  exit /b 1
)
if not defined ZIP_PATH (
  call :fail "ZIP_PATH is not set"
  exit /b 1
)
if not defined EXTRACT_DIR (
  call :fail "EXTRACT_DIR is not set"
  exit /b 1
)
if not defined EXE_PID (
  call :fail "EXE_PID is not set"
  exit /b 1
)

rem ---- guard 1: zip must exist before ANY destructive action ----
if not exist "%ZIP_PATH%" (
  call :fail "update zip not found"
  exit /b 1
)
for %%A in ("%ZIP_PATH%") do set "ZIP_SIZE=%%~zA"
if not defined ZIP_SIZE (
  call :fail "cannot read zip size"
  exit /b 1
)
if "%ZIP_SIZE%"=="0" (
  call :fail "update zip is empty"
  exit /b 1
)

rem ---- guard 2: archive readable (list only, still no wipe) ----
tar -tf "%ZIP_PATH%" >nul 2>&1
if errorlevel 1 (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Add-Type -A IO.Compression.FileSystem; [IO.Compression.ZipFile]::OpenRead('%ZIP_PATH%').Dispose() } catch { exit 1 }"
  if errorlevel 1 (
    call :fail "update zip is unreadable or corrupt"
    exit /b 1
  )
)

rem ---- step 3: extract to staging (old app still running) ----
call :progress extract 40 "expanding files"
if exist "%EXTRACT_DIR%" rd /s /q "%EXTRACT_DIR%" 2>nul
mkdir "%EXTRACT_DIR%" 2>nul
tar -xf "%ZIP_PATH%" -C "%EXTRACT_DIR%" 2>nul
if errorlevel 1 (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath '%ZIP_PATH%' -DestinationPath '%EXTRACT_DIR%' -Force"
  if errorlevel 1 (
    call :fail "extract failed"
    exit /b 1
  )
)
if not exist "%EXTRACT_DIR%\%EXE_NAME%" (
  for /D %%D in ("%EXTRACT_DIR%\*") do (
    if exist "%%D\%EXE_NAME%" (
      xcopy "%%D\*" "%EXTRACT_DIR%\" /E /I /Y >nul
      rd /s /q "%%D" 2>nul
    )
  )
)
if not exist "%EXTRACT_DIR%\%EXE_NAME%" (
  call :fail "BossAutoDelivery.exe missing after extract"
  exit /b 1
)

rem ---- guard 3: onedir package must carry _internal\base_library.zip ----
rem PyInstaller keeps the whole stdlib (encodings, codecs...) in that zip.
rem Without it the swapped-in exe dies at startup with
rem "No module named 'encodings'", leaving the user with an app that cannot
rem open. Refuse the package BEFORE the wipe - an old app beats no app.
if not exist "%EXTRACT_DIR%\_internal\base_library.zip" (
  call :fail "package is incomplete: _internal\base_library.zip missing"
  exit /b 1
)
call :progress ready 80 "package extracted"

rem ---- step 4: signal stopping, wait for old process ----
call :progress stopping 85 "closing old app"
set /a _waited=0
:waitloop
tasklist /FI "PID eq %EXE_PID%" /NH 2>nul | findstr /I /C:"%EXE_NAME%" >nul
if errorlevel 1 goto wipe
set /a _waited+=1
if %_waited% geq 30 goto killit
timeout /t 1 /nobreak >nul
goto waitloop

:killit
call :log force-kill pid %EXE_PID%
taskkill /PID %EXE_PID% /F >nul 2>&1
timeout /t 2 /nobreak >nul

rem ---- step 5: swap (ONLY now may we delete) ----
:wipe
call :progress swapping 90 "replacing program files"
pushd "%INSTALL_DIR%"
if errorlevel 1 (
  call :fail "cannot enter INSTALL_DIR"
  exit /b 1
)
for /D %%D in (*) do (
  if /I not "%%D"=="data" rd /s /q "%%D" 2>nul
)
for %%F in (*) do (
  if /I not "%%F"=="data" del /q "%%F" 2>nul
)
popd

rem ---- step 6: copy staging -> install ----
xcopy "%EXTRACT_DIR%\*" "%INSTALL_DIR%\" /E /I /Y >nul
if errorlevel 1 (
  call :fail "copy to install dir failed"
  exit /b 1
)
if not exist "%INSTALL_DIR%\%EXE_NAME%" (
  call :fail "new BossAutoDelivery.exe missing after copy"
  exit /b 1
)

rem ---- step 7: done marker + relaunch on same port ----
(echo {"ok":true,"version":"%NEW_VERSION%","finished_at":0}) > "%WORK_DIR%\done.json"
call :progress done 100 "relaunching"
if defined APP_PORT (
  start "" "%INSTALL_DIR%\%EXE_NAME%" --port %APP_PORT% --no-browser
) else (
  start "" "%INSTALL_DIR%\%EXE_NAME%" --no-browser
)

rem ---- step 8: cleanup ONLY on success ----
timeout /t 1 /nobreak >nul
del /q "%ZIP_PATH%" >nul 2>&1
rd /s /q "%EXTRACT_DIR%" >nul 2>&1
call :log === update done ===
del /q "%~f0" >nul 2>&1
exit /b 0

:progress
>> "%PROG%" echo %~1^|%~2^|%~3
call :log [%~1 %~2] %~3
exit /b 0

:log
>> "%LOG%" echo %DATE% %TIME% %~1
exit /b 0

:fail
call :progress error 100 "%~1"
call :log FAIL %~1
echo.
echo Update failed: %~1
echo Log: %LOG%
echo Your data folder "data" was NOT deleted.
pause
exit /b 1
"""


def render_bat(**_kwargs: Any) -> str:
    """渲染 bat 正文（已带 CRLF）；参数全走环境变量，这里不接路径。

    cmd 对 LF-only 的 bat 里 `goto` / 标签兼容性很差，行尾必须是 `\\r\\n`。
    """
    return _BAT_TEMPLATE.replace("\r\n", "\n").replace("\n", "\r\n")


def write_bat(dest_dir: Path | None = None) -> Path:
    """把 bat 落到 %TEMP%（必须在安装目录外，否则待会儿被清掉）。

    Args:
        dest_dir: 目标目录；省略用更新工作目录。
    """
    directory = dest_dir or work_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "apply_update.bat"
    path.write_bytes(render_bat().encode("ascii"))
    return path


def spawn_bat(bat_path: Path, *, env_extra: dict[str, str]) -> None:
    """分离启动 bat；非 Windows 抛 `UpdateApplyError`。

    必须给它真实存在但没窗口的控制台（`CREATE_NO_WINDOW`）：`DETACHED_PROCESS`
    会让 bat 里管道永久挂死。

    Args:
        bat_path: bat 路径。
        env_extra: 额外环境变量（安装目录 / zip 路径 / 端口等）。
    """
    if sys.platform != "win32":
        raise UpdateApplyError("自动更新目前仅支持 Windows")
    env = os.environ.copy()
    env.update({k: str(v) for k, v in env_extra.items()})
    creationflags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags |= subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    try:
        subprocess.Popen(
            ["cmd", "/c", str(bat_path)],
            env=env,
            cwd=str(env_extra.get("INSTALL_DIR") or "."),
            close_fds=True,
            creationflags=creationflags,
        )
    except OSError as exc:
        raise UpdateApplyError(f"启动更新脚本失败：{exc}") from exc


_server: Any = None
_bound_port: int | None = None


def bind_server(server: Any) -> None:
    """挂上 uvicorn.Server，供退出时安全停下；顺带记下监听端口。

    Args:
        server: 服务实例；端口从 `server.config.port` 读。
    """
    global _server, _bound_port
    _server = server
    try:
        cfg = getattr(server, "config", None)
        port = getattr(cfg, "port", None)
        if isinstance(port, int) and port > 0:
            _bound_port = port
    except Exception:  # noqa: BLE001 - 记不到就靠调用方传
        logger.debug("从 uvicorn config 读端口失败", exc_info=True)


def request_exit(*, delay: float = 1.2, hard_exit_after: float = 8.0) -> None:
    """让服务自己退出：放完 WS 的 stopping 帧，再关 uvicorn，最后硬退兜底。

    Args:
        delay: 退出前的等待（秒）。
        hard_exit_after: 硬退兜底超时（秒）。
    """

    def _run() -> None:
        """后台线程体：设 `should_exit`，到点后 `os._exit(0)`。"""
        if delay > 0:
            time.sleep(delay)
        server = _server
        if server is not None:
            try:
                server.should_exit = True
            except Exception:  # noqa: BLE001 - 退不出去也得走硬退
                logger.warning("设置 should_exit 失败", exc_info=True)
        deadline = time.monotonic() + max(0.0, hard_exit_after)
        while time.monotonic() < deadline:
            if server is None or getattr(server, "should_exit", False):
                time.sleep(min(1.0, hard_exit_after))
                break
            time.sleep(0.2)
        os._exit(0)

    threading.Thread(target=_run, name="update-exit", daemon=True).start()


def _tail_progress(path: Path, *, interval: float = 0.2) -> None:
    """把 bat 写的 `progress.log` 逐行转成 WS 事件；stopping 时触发退出。

    Args:
        path: `progress.log` 路径。
        interval: 轮询间隔（秒）。
    """
    offset = 0
    while True:
        try:
            if path.is_file():
                with open(path, "rb") as fh:
                    fh.seek(offset)
                    chunk = fh.read()
                if chunk:
                    offset += len(chunk)
                    for raw in chunk.decode("utf-8", "replace").splitlines():
                        event = parse_progress_line(raw)
                        if event is None:
                            continue
                        publish(event)
                        if event.stage == "stopping":
                            request_exit()
                        if event.stage in ("done", "error"):
                            return
        except Exception:  # noqa: BLE001 - 转发挂了不影响 bat 干活
            logger.debug("转发更新进度失败", exc_info=True)
        time.sleep(interval)


def start_update_job(
    *,
    version: str,
    zip_url: str,
    zip_name: str = "",
    install_dir: Path | None = None,
    port: int | None = None,
) -> None:
    """后台跑完整更新：下载 → 校验 → 写 bat → 启动 → 转发进度。

    Args:
        version: 目标版本号。
        zip_url: 更新包直链。
        zip_name: 更新包文件名；空则从 URL 取。
        install_dir: 安装目录；省略用当前可执行文件目录。
        port: 重启监听端口；省略用启动时记下的端口。
    """
    thread = threading.Thread(
        target=_run_update_job,
        kwargs={
            "version": version,
            "zip_url": zip_url,
            "zip_name": zip_name,
            "install_dir": install_dir,
            "port": port,
        },
        name="update-apply",
        daemon=True,
    )
    thread.start()


def _run_update_job(
    *,
    version: str,
    zip_url: str,
    zip_name: str = "",
    install_dir: Path | None = None,
    port: int | None = None,
) -> None:
    """更新工作线程体；失败以 `error` 事件推给前端。"""
    try:
        from .. import runtime

        root = work_dir()
        root.mkdir(parents=True, exist_ok=True)
        extract_dir = root / "extract"
        progress_log = root / "progress.log"
        progress_log.unlink(missing_ok=True)

        def _on_progress(pct: int) -> None:
            """把下载进度转成一帧 WS 事件。"""
            publish(ProgressEvent(stage="download", percent=pct, text=f"正在下载更新包… {pct}%"))

        publish(ProgressEvent(stage="download", percent=0, text="正在下载更新包…"))
        zip_path = download_zip(
            zip_url, root, expected_name=zip_name, on_progress=_on_progress
        )

        publish(ProgressEvent(stage="validate", percent=25, text="正在校验更新包…"))
        validate_zip(zip_path)

        bat_path = write_bat(root)
        target_dir = Path(install_dir or runtime.executable_dir())
        listen_port = port if port else _bound_port
        spawn_bat(
            bat_path,
            env_extra={
                "INSTALL_DIR": str(target_dir),
                "ZIP_PATH": str(zip_path),
                "EXTRACT_DIR": str(extract_dir),
                "EXE_PID": str(os.getpid()),
                "APP_PORT": str(listen_port or ""),
                "NEW_VERSION": str(version or ""),
                "WORK_DIR": str(root),
            },
        )

        threading.Thread(
            target=_tail_progress, args=(progress_log,), name="update-progress", daemon=True
        ).start()
    except UpdateApplyError as exc:
        logger.warning("更新失败：%s", exc)
        publish(ProgressEvent(stage="error", percent=100, text=str(exc)))
    except Exception as exc:  # noqa: BLE001 - 兜住线程里的意外
        logger.exception("更新流程崩了")
        publish(ProgressEvent(stage="error", percent=100, text=f"更新失败：{exc}"))
