"""应用内自更新：下载 zip → 校验 → 交给 bat 停服/换文件/重启，全程推 WS 进度。

为什么要 bat：Windows 上运行中的 exe 和 ``_internal\\`` 里的 DLL 都被锁着，
**自己换不掉自己**。所以 Python 只负责「下载 + 校验 + 生成脚本 + 转发进度 +
退出」，替换动作交给独立的 cmd 进程。

与 bat 的约定（都放在 ``%TEMP%\\BossAutoDelivery-update\\``）：

===============  ==========  ==========================================
文件             写入方      格式
===============  ==========  ==========================================
``*.zip``        Python      更新包本体
``extract\\``    bat         解压暂存（确认有 exe 才动安装目录）
``progress.log`` bat         行式 ``stage|percent|text``（ASCII）
``update.log``   bat         人话诊断，失败时用户看这个
``done.json``    bat         成功回执 ``{"ok":true,"version":...}``，新进程读一次
===============  ==========  ==========================================

bat **先验证后破坏**：zip 不在/解压不出 exe 就直接退出，安装目录一个文件都不碰。
"""

from __future__ import annotations

import asyncio
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

#: 更新工作目录名（挂在 %TEMP% 下，不在安装目录里——安装目录待会儿要被清）
UPDATE_TEMP_DIR_NAME = "BossAutoDelivery-update"
#: 更新包大小上限。发行包 ~30MB，给 100 倍冗余防意外
MAX_ZIP_BYTES = 500 * 1024 * 1024
#: (连接超时, 读超时)
DOWNLOAD_TIMEOUT = (10.0, 300.0)
#: done.json 的有效期：太久远的回执不当真（免得旧标记误触发「更新完成」）
DONE_MARKER_MAX_AGE = 600.0

EXE_NAME = "BossAutoDelivery.exe"


class UpdateApplyError(Exception):
    """更新流程里可读的失败原因（界面直接摆这句话）。"""


# --------------------------------------------------------------------------- #
# 进度事件 + 广播
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProgressEvent:
    #: download / validate / extract / ready / stopping / swapping / done / error
    stage: str
    percent: int
    text: str
    update_done: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "percent": self.percent,
            "text": self.text,
            "update_done": self.update_done,
        }


#: bat 的 stage → 界面中文。bat 本体保持纯 ASCII（路径/中文塞进 bat 极易出事）
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
#: (loop, queue) 订阅表。WS 连上就挂一条，断开就摘
_subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
#: 最近一次进度（WS 连上先给一帧快照，免得页面错过开场）
_last_event: ProgressEvent = ProgressEvent(stage="idle", percent=0, text="")
#: 新进程的「更新完成」回执（读一次 done.json 后缓存在这，进程内谁问谁拿）
_done_cache: dict[str, Any] | None = None
_done_loaded = False


def subscribe(loop: asyncio.AbstractEventLoop) -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue(maxsize=64)
    with _lock:
        _subscribers.append((loop, queue))
    return queue


def unsubscribe(queue: asyncio.Queue) -> None:
    with _lock:
        _subscribers[:] = [(lp, q) for lp, q in _subscribers if q is not queue]


def publish(event: ProgressEvent) -> None:
    """线程安全：更新工作线程里调，WS 在事件循环里收。"""
    global _last_event
    with _lock:
        _last_event = event
        targets = list(_subscribers)
    for loop, queue in targets:
        def _put(q: asyncio.Queue = queue, ev: ProgressEvent = event) -> None:
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:  # pragma: no cover - 64 帧够宽
                pass

        try:
            loop.call_soon_threadsafe(_put)
        except RuntimeError:  # pragma: no cover - loop 已关
            pass


def snapshot_progress() -> dict[str, Any]:
    with _lock:
        payload = _last_event.to_payload()
    payload["update_done"] = consume_done_marker()
    return payload


def peek_progress() -> dict[str, Any]:
    """只看最近一次进度事件，**不动** ``done.json``。

    给「系统设置 → 版本更新」的诊断面板用：看一眼日志不该把更新完成的回执
    吃掉（那是 :func:`snapshot_progress` 的活，一次性）。回执字段一律置空——
    这里是「偷看」，不是消费。
    """
    with _lock:
        payload = _last_event.to_payload()
    payload["update_done"] = None
    return payload


def parse_progress_line(line: str) -> ProgressEvent | None:
    """``stage|percent|text`` → 事件；认不出回 ``None``。"""
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


# --------------------------------------------------------------------------- #
# 目录与完成回执
# --------------------------------------------------------------------------- #


def work_dir() -> Path:
    base = os.environ.get("TEMP") or os.environ.get("TMP") or tempfile.gettempdir()
    return Path(base) / UPDATE_TEMP_DIR_NAME


def done_marker_path() -> Path:
    return work_dir() / "done.json"


#: 诊断页默认读的尾部字节数；用户可在接口上再要大一点
LOG_TAIL_BYTES = 64 * 1024
LOG_TAIL_MAX = 256 * 1024
#: 要展示的过程日志（bat 用 ``>>`` 追加写，永不截断）
LOG_FILE_NAMES = ("update.log", "progress.log")


def read_update_logs(*, tail_bytes: int = LOG_TAIL_BYTES) -> tuple[list[dict[str, Any]], str | None]:
    """读 ``%TEMP%\\BossAutoDelivery-update\\`` 下的过程日志**尾部**，给网页直接看。

    用户不会开这个目录，所以日志要能就地看、出错也要能截图。约定：

    * **文件不存在不是错误**——源码跑 / 从没更新过就是没有，回 ``exists: False``
    * **读不出来也不报 500**——bat 正在写时文件可能被锁，塞进 ``read_error``
    * 超长只取尾部并丢掉残行（bat 是 ``>>`` 追加，越积越长）

    :return: ``(files, hint)``；``hint`` 是给空态的一句话，没有就 ``None``。
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
            # 砍掉开头的半行，免得日志第一行是半截乱码
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
    """读一次 ``done.json`` 并清掉文件；进程内缓存，多端问都能拿到同一份。

    超过 :data:`DONE_MARKER_MAX_AGE` 的旧回执当垃圾丢掉——否则几个月后打开
    页面可能撞上「更新完成」的幽灵弹窗。
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
    """给测试用。"""
    global _done_cache, _done_loaded
    with _lock:
        _done_cache = None
        _done_loaded = False


# --------------------------------------------------------------------------- #
# 下载 + 校验
# --------------------------------------------------------------------------- #


def download_zip(
    url: str,
    dest_dir: Path,
    *,
    expected_name: str = "",
    on_progress: Callable[[int], None] | None = None,
) -> Path:
    """流式下载更新包。先写 ``.part`` 再改名，半截文件不会被当成完整包。"""
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
    """更新包必须是真 zip、没有 zip-slip 路径、并且解得出 ``BossAutoDelivery.exe``。"""
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
    # 扁平布局（根上 exe）或外面再套一层目录都认
    has_exe = (EXE_NAME in names) or any(
        n.count("/") == 1 and n.endswith("/" + EXE_NAME) for n in names
    )
    if not has_exe:
        raise UpdateApplyError(f"更新包里找不到 {EXE_NAME}，已取消更新")


# --------------------------------------------------------------------------- #
# bat：先验证后破坏
# --------------------------------------------------------------------------- #

#: 正文纯 ASCII。路径/端口/版本一律走环境变量——中文与空格路径写进 bat 是事故高发区。
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
    """渲染 bat 正文（**已带 CRLF**）。参数全走环境变量，这里不接路径。

    cmd 对 LF-only 的 bat 里 ``goto``/标签兼容性很差，行尾必须是 ``\\r\\n``。
    """
    return _BAT_TEMPLATE.replace("\r\n", "\n").replace("\n", "\r\n")


def write_bat(dest_dir: Path | None = None) -> Path:
    """把 bat 落到 %TEMP%（必须在安装目录**外**，否则待会儿被清掉）。

    用 ``write_bytes`` 原样落盘——任何换行翻译都可能把 CRLF 再叠一层。
    """
    directory = dest_dir or work_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "apply_update.bat"
    path.write_bytes(render_bat().encode("ascii"))
    return path


def spawn_bat(bat_path: Path, *, env_extra: dict[str, str]) -> None:
    """分离启动 bat。**留可见黑窗**——失败时要让人看见 ``pause`` 留下的原因。"""
    if sys.platform != "win32":
        raise UpdateApplyError("自动更新目前仅支持 Windows")
    env = os.environ.copy()
    env.update({k: str(v) for k, v in env_extra.items()})
    creationflags = 0
    if hasattr(subprocess, "DETACHED_PROCESS"):
        creationflags |= subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
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


# --------------------------------------------------------------------------- #
# 退出 + 进度转发
# --------------------------------------------------------------------------- #

_server: Any = None
#: 服务监听的端口。bat 重启时要按**原端口**拉起来，浏览器标签页才能重连上
_bound_port: int | None = None


def bind_server(server: Any) -> None:
    """挂上 uvicorn.Server，供 :func:`request_exit` 安全停下；顺带记下端口。"""
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
    """让服务自己退出：先放完 WS 的 stopping 帧，再关 uvicorn，最后硬退兜底。

    用 ``should_exit``（uvicorn 官方停止位）让监听端口干净释放，新进程才能绑回
    **同一端口**——浏览器标签页就是靠这个重连上来的。``os._exit`` 保证即使有
    请求挂着也一定会死，文件锁才能放开给 bat 换文件。
    """

    def _run() -> None:
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
    """把 bat 写的 ``progress.log`` 逐行转成 WS 事件；顺带在 stopping 时触发退出。"""
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


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def start_update_job(
    *,
    version: str,
    zip_url: str,
    zip_name: str = "",
    install_dir: Path | None = None,
    port: int | None = None,
) -> None:
    """后台跑完整更新：下载 → 校验 → 写 bat → 启动 → 转发进度。

    HTTP 侧立即返回，进度全走 WS；失败以 ``error`` 事件推给前端。
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
    try:
        from .. import runtime

        root = work_dir()
        root.mkdir(parents=True, exist_ok=True)
        extract_dir = root / "extract"
        progress_log = root / "progress.log"
        progress_log.unlink(missing_ok=True)

        def _on_progress(pct: int) -> None:
            publish(ProgressEvent(stage="download", percent=pct, text=f"正在下载更新包… {pct}%"))

        publish(ProgressEvent(stage="download", percent=0, text="正在下载更新包…"))
        zip_path = download_zip(
            zip_url, root, expected_name=zip_name, on_progress=_on_progress
        )

        publish(ProgressEvent(stage="validate", percent=25, text="正在校验更新包…"))
        validate_zip(zip_path)

        bat_path = write_bat(root)
        target_dir = Path(install_dir or runtime.executable_dir())
        # 端口优先用调用方给的，其次启动时记下的——一定要传给 bat，
        # 否则新进程会另挑一个端口，浏览器那个标签页就再也连不回来了。
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

        # bat 解压阶段旧服务还活着，进度从 progress.log 转发到 WS
        threading.Thread(
            target=_tail_progress, args=(progress_log,), name="update-progress", daemon=True
        ).start()
    except UpdateApplyError as exc:
        logger.warning("更新失败：%s", exc)
        publish(ProgressEvent(stage="error", percent=100, text=str(exc)))
    except Exception as exc:  # noqa: BLE001 - 兜住线程里的意外
        logger.exception("更新流程崩了")
        publish(ProgressEvent(stage="error", percent=100, text=f"更新失败：{exc}"))
