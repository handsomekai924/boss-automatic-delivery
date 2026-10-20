"""用真 Chrome 经 CDP 让站点自算 `__zp_stoken__`，并落盘、判过期。

只读自拉 Chrome 调试口里的 Cookie，不解密；滑块/风控不在此处理。细节见 `notes/boss_jobs/cdp_stoken.md`。

Attributes:
    CHROME_PROFILE_ENV: 独立 user-data-dir；默认 profile 会被拒开调试口。
    CHROME_KEEP_ENV: 抓完是否留着 Chrome（`0`/`false` = 关掉）。
    CHROME_CLOSE_ON_EXIT_ENV: 进程退出时是否关掉自拉的 Chrome（`0`/`false` = 留着）。
    CHROME_MODES: 窗口档位：`visible` / `offscreen` / `hidden`。
    DEFAULT_CHROME_MODE: 默认 `hidden`；人工过滑块时改 `visible`。
    EXPIRY_MARGIN: 快过期就提前换新的余量（秒）。
    RENEW_COOLDOWN: 强制换新的冷却（秒），免得连环拉 Chrome 惹风控。
    ACQUIRE_TIMEOUT: 等站点写出 token 的超时（秒）。
    TOKEN_PAGE: 要 token 就得跑站点 JS 的那个页面。
    _NON_COOKIE_KEYS: 登录态里不需要往 Chrome 灌的键。
    _OFFSCREEN_POS: `offscreen`/`hidden` 把窗口摆到这儿（别用 Win32 保留值 `-32000`）。
    _LOOPBACK_HOSTS: 调试口两个 loopback 都要问，Chrome 被占时会退到 `[::1]`。
    _LOOPBACK_OPENER: 不走环境变量 HTTP 代理，免得梯子劫走 loopback。
    _launched: 本进程自拉的 Chrome 清单，改动要拿 `_launched_lock`。
    _FATAL_CONSOLE_EVENTS: 可放心在此收尾的控制台事件；不含 `CTRL_C_EVENT`。
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import boss_db

from . import config as C
from .stoken import STOKEN_MAX_AGE, StokenError

logger = logging.getLogger(__name__)

__all__ = [
    "ACQUIRE_TIMEOUT",
    "CDP_PORT_ENV",
    "CHROME_BIN_ENV",
    "CHROME_CLOSE_ON_EXIT_ENV",
    "CHROME_KEEP_ENV",
    "CHROME_MODE_ENV",
    "CHROME_MODES",
    "CHROME_PROFILE_ENV",
    "CdpClient",
    "CdpStokenProvider",
    "DEFAULT_CDP_PORT",
    "DEFAULT_CHROME_MODE",
    "DEFAULT_DB_PATH",
    "RENEW_COOLDOWN",
    "StokenRecord",
    "StokenStore",
    "TOKEN_PAGE",
    "close_launched_browsers",
    "connect_or_launch",
    "find_chrome",
    "launch_chrome",
    "probe_debug",
]

CDP_PORT_ENV: str = "BOSS_CDP_PORT"
DEFAULT_CDP_PORT: int = 9222

CHROME_BIN_ENV: str = "BOSS_CHROME_BIN"

CHROME_PROFILE_ENV: str = "BOSS_CHROME_PROFILE"
DEFAULT_CHROME_PROFILE: Path = C.PROJECT_ROOT / ".chrome_profile"

CHROME_KEEP_ENV: str = "BOSS_CDP_KEEP"

CHROME_CLOSE_ON_EXIT_ENV: str = "BOSS_CDP_CLOSE_ON_EXIT"

CHROME_MODE_ENV: str = "BOSS_CHROME_MODE"

CHROME_MODES: tuple[str, ...] = ("visible", "offscreen", "hidden")

DEFAULT_CHROME_MODE: str = "hidden"

DEFAULT_DB_PATH: Path = boss_db.DEFAULT_DB_PATH

EXPIRY_MARGIN: int = 5 * 60

RENEW_COOLDOWN: float = 5.0

ACQUIRE_TIMEOUT: float = 40.0

TOKEN_PAGE: str = f"{C.BASE_URL}/web/geek/jobs"

_NON_COOKIE_KEYS: frozenset[str] = frozenset({"__zp_stoken__"})

_CHROME_CANDIDATES: tuple[str, ...] = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
)


@dataclass(frozen=True)
class StokenRecord:
    """一枚 `__zp_stoken__` 及其有效期。

    Attributes:
        token: 令牌本体。
        minted_at: 拿到手的 Unix 时间戳（秒）。
        expires_at: 过期时间戳（秒）；站点给 Cookie `expires`，没有就按 max-age 从 `minted_at` 推。
        source: 怎么来的（`cdp` / `manual`）。
    """

    token: str
    minted_at: float
    expires_at: float
    source: str = "cdp"

    @classmethod
    def from_cookie(
        cls,
        cookie: Mapping[str, Any],
        *,
        minted_at: float | None = None,
        source: str = "cdp",
    ) -> "StokenRecord":
        """从 CDP 的 Cookie 结构建一条记录（认 `expires`，没有就按 max-age 推）。"""
        now = minted_at if minted_at is not None else time.time()
        raw = cookie.get("expires")
        try:
            expires_at = float(raw) if raw is not None else -1.0
        except (TypeError, ValueError):
            expires_at = -1.0
        if expires_at <= 0:
            expires_at = now + STOKEN_MAX_AGE
        return cls(
            token=str(cookie.get("value") or ""),
            minted_at=now,
            expires_at=expires_at,
            source=source,
        )

    @property
    def is_usable(self) -> bool:
        """有没有非空的令牌本体。"""
        return bool(self.token)

    def is_fresh(self, *, margin: int = EXPIRY_MARGIN) -> bool:
        """还没到该换新的时候吗？（留 `margin` 秒余量）"""
        return self.is_usable and time.time() < (self.expires_at - margin)

    @property
    def ttl_left(self) -> float:
        """距过期还剩多少秒（不会为负）。"""
        return max(0.0, self.expires_at - time.time())

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的 dict。"""
        return {
            "token": self.token,
            "minted_at": self.minted_at,
            "expires_at": self.expires_at,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "StokenRecord | None":
        """从落盘的 dict 还原；不像一条完整记录就返回 `None`。"""
        if not isinstance(payload, Mapping):
            return None
        token = payload.get("token")
        if not token:
            return None
        try:
            minted_at = float(payload.get("minted_at") or 0.0)
            expires_at = float(payload.get("expires_at") or 0.0)
        except (TypeError, ValueError):
            return None
        return cls(
            token=str(token),
            minted_at=minted_at,
            expires_at=expires_at,
            source=str(payload.get("source") or "cdp"),
        )


class StokenStore:
    """`doc('stoken')` 的读写：一次 fetch 一份过期账本。

    跟登录态分开放——`doc('session')` 的 `cookies` 是纯 `str → str`，
    塞不进过期时间。取出的 token 会另外镜像进 `doc('session')`。

    Args:
        path: 状态库路径；省略 = `BOSS_DB` = `data/boss.db`。
    """

    def __init__(self, path: Path | str | None = None) -> None:
        """初始化。"""
        self.path = boss_db.resolve_db_path(path)

    def load(self) -> StokenRecord | None:
        """读账本；没有、坏 JSON 或读盘失败都当没有。"""
        try:
            raw = boss_db.doc_get_raw(boss_db.DOC_STOKEN, self.path)
        except (OSError, sqlite3.Error) as exc:
            logger.warning("读 %s 的 %s 失败（当没有）：%s", self.path, boss_db.DOC_STOKEN, exc)
            return None
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
        except ValueError:
            logger.warning("状态库里的 %s 不是合法 JSON，丢弃重取", boss_db.DOC_STOKEN)
            return None
        return StokenRecord.from_dict(payload)

    def save(self, record: StokenRecord) -> None:
        """写账本；写失败只告警，不拦主流程。"""
        try:
            resolved = boss_db.doc_set(boss_db.DOC_STOKEN, record.to_dict(), self.path)
        except (OSError, sqlite3.Error) as exc:
            logger.warning("写 %s 失败（不拦主流程）：%s", self.path, exc)
        else:
            logger.debug(
                "已落盘 %s（%s，剩 %.0f 分钟）", resolved, record.source, record.ttl_left / 60
            )

    def clear(self) -> None:
        """删掉账本这一行；失败忽略。"""
        try:
            boss_db.doc_delete(boss_db.DOC_STOKEN, self.path)
        except (OSError, sqlite3.Error):
            pass


def find_chrome() -> str:
    """定位 Chrome（或 Chromium）。找不到就报错说清该设哪个环境变量。"""
    env = os.environ.get(CHROME_BIN_ENV, "").strip()
    if env:
        if Path(env).exists():
            return env
        raise StokenError(f"{CHROME_BIN_ENV}={env} 指的文件不存在")
    for path in _CHROME_CANDIDATES:
        if path and Path(path).exists():
            return path
    for name in ("chrome", "google-chrome", "google-chrome-stable", "chromium"):
        found = shutil.which(name)
        if found:
            return found
    raise StokenError(
        f"找不到 Chrome：装个 Google Chrome，或用 {CHROME_BIN_ENV} 指到 chrome 可执行文件。"
    )


def _port() -> int:
    """调试端口：读 `CDP_PORT_ENV`，非法或未设则用 `DEFAULT_CDP_PORT`。"""
    env = os.environ.get(CDP_PORT_ENV, "").strip()
    if env.isdigit():
        return int(env)
    return DEFAULT_CDP_PORT


def _profile_dir() -> Path:
    """Chrome 专属 user-data-dir：读 `CHROME_PROFILE_ENV`，否则用默认路径。"""
    env = os.environ.get(CHROME_PROFILE_ENV, "").strip()
    return Path(env) if env else DEFAULT_CHROME_PROFILE


def _keep_browser() -> bool:
    """抓完是否留着 Chrome（`CHROME_KEEP_ENV` 未显式关掉即为留）。"""
    return os.environ.get(CHROME_KEEP_ENV, "").strip().lower() not in {"0", "false", "no", "off"}


def _close_on_exit() -> bool:
    """进程退出时是否关掉自拉 Chrome（`CHROME_CLOSE_ON_EXIT_ENV` 未显式关即为关）。"""
    return os.environ.get(CHROME_CLOSE_ON_EXIT_ENV, "").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _mode() -> str:
    """窗口档位（`CHROME_MODE_ENV`）。写错了退回默认档位。"""
    env = os.environ.get(CHROME_MODE_ENV, "").strip().lower()
    if not env:
        return DEFAULT_CHROME_MODE
    if env not in CHROME_MODES:
        logger.warning(
            "%s=%s 不认识（可选 %s），按 %s 处理",
            CHROME_MODE_ENV,
            env,
            "/".join(CHROME_MODES),
            DEFAULT_CHROME_MODE,
        )
        return DEFAULT_CHROME_MODE
    return env


_OFFSCREEN_POS: int = -10000


def _mode_flags(mode: str) -> list[str]:
    """按档位给窗口相关的启动参数。"""
    if mode == "headless":
        return ["--headless=new"]
    if mode == "offscreen":
        return [f"--window-position={_OFFSCREEN_POS},{_OFFSCREEN_POS}"]
    if mode == "hidden":
        return [
            f"--window-position={_OFFSCREEN_POS},{_OFFSCREEN_POS}",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-background-timer-throttling",
        ]
    return []


def _disable_features(mode: str) -> str:
    """拼 `--disable-features` 的值；hidden 档位追加遮挡停画的解药。"""
    names = ["Translate", "MediaRouter"]
    if mode == "hidden":
        names.append("CalculateNativeWinOcclusion")
    return ",".join(names)


_SW_HIDE: int = 0


def _hide_windows(
    pid: int,
    *,
    timeout: float = 10.0,
    proc: subprocess.Popen | None = None,
) -> int:
    """把这台 Chrome 的顶层窗口 `SW_HIDE` 掉，返回藏了几个。

    只在 Windows 上有意义（别处返回 0）。Chrome 开窗口比开调试口晚一拍，
    所以找不到就等到 `timeout`。

    Args:
        pid: Chrome 进程 id。
        timeout: 最长等待秒数。
        proc: 启动时拿到的进程；它要是早退了就不再等窗口。
    """
    if os.name != "nt":
        return 0
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    found: list[int] = []

    def _visit(hwnd: int, _lparam: int) -> bool:
        """枚举回调：隐藏属于 `pid` 的可见顶层窗口。"""
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            user32.ShowWindow(hwnd, _SW_HIDE)
            found.append(hwnd)
        return True

    callback = enum_proc(_visit)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            logger.debug("pid %s 已经退了，不再等窗口", pid)
            return 0
        found.clear()
        user32.EnumWindows(callback, 0)
        if found:
            return len(found)
        time.sleep(0.2)
    return 0


@dataclass
class _Launched:
    """本进程拉起来的一台 Chrome。

    只登记自己拉的；复用现成调试口那台绝不入册，退出一律不碰。

    Attributes:
        proc: 启动进程。
        port: 调试端口。
        ws_url: browser 级 ws 地址；按尾巴 uuid 连回去，不会误伤后来占端口的别人。
    """

    proc: subprocess.Popen
    port: int
    ws_url: str = ""


_launched: list[_Launched] = []
_launched_lock = threading.Lock()
_exit_hooks_installed = False

_CTRL_C_EVENT = 0
_CTRL_BREAK_EVENT = 1
_CTRL_CLOSE_EVENT = 2
_CTRL_LOGOFF_EVENT = 5
_CTRL_SHUTDOWN_EVENT = 6
_console_handler: Any = None

_FATAL_CONSOLE_EVENTS: frozenset[int] = frozenset(
    (_CTRL_BREAK_EVENT, _CTRL_CLOSE_EVENT, _CTRL_LOGOFF_EVENT, _CTRL_SHUTDOWN_EVENT)
)


def _track_launch(proc: subprocess.Popen, port: int) -> None:
    """登记一台自拉的 Chrome，并挂上退出收尾钩子。"""
    with _launched_lock:
        _launched.append(_Launched(proc=proc, port=port))
    _install_exit_hooks()


def _track_ws(port: int, ws_url: str) -> None:
    """把刚连上的 ws 地址记到对应那台身上（按「还活着的那个同端口」认）。"""
    with _launched_lock:
        for item in reversed(_launched):
            if item.port == port and item.proc.poll() is None:
                item.ws_url = ws_url
                return


def _install_exit_hooks() -> None:
    """第一次拉 Chrome 时挂上退出钩子。

    不在 import 时挂：没拉过浏览器就没什么可收的。
    """
    global _exit_hooks_installed
    with _launched_lock:
        if _exit_hooks_installed:
            return
        _exit_hooks_installed = True
    if not _close_on_exit():
        return
    atexit.register(close_launched_browsers)
    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except (AttributeError, OSError, ValueError):
        pass
    _install_console_handler()


def _console_ctrl_handler(event: int) -> bool:
    """Win32 控制台事件回调：致命事件先收尾，返回 `False` 不拦默认动作。"""
    if event in _FATAL_CONSOLE_EVENTS:
        close_launched_browsers(timeout=3.0)
    return False


def _install_console_handler() -> None:
    """Windows：控制台硬杀进程的那几条路，也把 Chrome 收走。

    `CTRL_CLOSE_EVENT` / `CTRL_LOGOFF_EVENT` / `CTRL_SHUTDOWN_EVENT` /
    `CTRL_BREAK_EVENT` 走不到 `atexit`，在此抢最后一次机会。
    `CTRL_C_EVENT` 不碰，放行走 Python 自己的 `KeyboardInterrupt` → `atexit`。
    """
    global _console_handler  # noqa: PLW0603 - 得留住引用，不然回调会被 GC 掉
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    _console_handler = handler_type(_console_ctrl_handler)
    try:
        ctypes.WinDLL("kernel32", use_last_error=True).SetConsoleCtrlHandler(
            _console_handler, True
        )
    except OSError as exc:  # pragma: no cover - 没控制台也不该拦主流程
        logger.debug("挂控制台处理器失败（没控制台？）：%s", exc)


def _on_sigterm(signum: int, _frame: Any) -> None:
    """SIGTERM 收尾：关掉自拉 Chrome 后以 `128 + signum` 退出。"""
    close_launched_browsers(timeout=3.0)
    raise SystemExit(128 + signum)


def close_launched_browsers(*, timeout: float = 5.0) -> int:
    """把本进程拉起来的 Chrome 全关掉，返回关了几台。

    进程退出时由 `atexit` 自动调一次（`BOSS_CDP_CLOSE_ON_EXIT=0` 关掉这个行为）。
    先走 CDP `Browser.close` 让它自己退，没退就杀进程。复用现成调试口那种一律不碰。
    """
    with _launched_lock:
        pending, _launched[:] = list(_launched), []
    closed = 0
    for item in pending:
        if item.proc.poll() is not None:
            continue
        if _shutdown_one(item, timeout=timeout):
            closed += 1
    if closed:
        logger.debug("关掉 %d 台自拉 Chrome", closed)
    return closed


def _shutdown_one(item: _Launched, *, timeout: float) -> bool:
    """关掉一台自拉 Chrome；CDP 关不掉就杀进程。返回是否成功关掉。"""
    if not item.ws_url:
        return _kill(item.proc)
    client = None
    try:
        client = CdpClient(item.ws_url, recv_timeout=timeout)
        client.call("Browser.close", timeout=timeout)
    except StokenError as exc:
        logger.debug("CDP 关不掉 %s（%s），改杀进程", item.ws_url, exc)
    finally:
        if client is not None:
            client.close()
    try:
        item.proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        logger.debug("pid %s 没自己退，直接杀", item.proc.pid)
        return _kill(item.proc)


def _kill(proc: subprocess.Popen) -> bool:
    """杀掉子进程；失败记日志并返回 `False`。"""
    try:
        proc.kill()
    except OSError as exc:  # pragma: no cover - 退出路上，别让异常冒出去
        logger.debug("杀 pid %s 失败：%s", proc.pid, exc)
        return False
    return True


_LOOPBACK_HOSTS: tuple[str, ...] = ("127.0.0.1", "[::1]")

_LOOPBACK_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _debug_url(host: str, port: int, path: str) -> str:
    """拼调试口的 HTTP URL。"""
    return f"http://{host}:{port}{path}"


def probe_debug(port: int | None = None, *, timeout: float = 1.5) -> str:
    """问一下调试口在不在，在就返回 browser 级 WebSocket 地址。

    IPv4 / IPv6 两个 loopback 都问。空字符串 = 没连上。
    """
    port = port or _port()
    for host in _LOOPBACK_HOSTS:
        url = _debug_url(host, port, "/json/version")
        try:
            with _LOOPBACK_OPENER.open(url, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, OSError, ValueError):
            continue
        ws = str(data.get("webSocketDebuggerUrl") or "")
        if ws:
            return ws
        logger.debug("调试口在 %s，但没给 webSocketDebuggerUrl：%s", url, data)
    return ""


def _can_bind(host: str, port: int) -> bool:
    """该 loopback 地址上还能不能 bind 这个端口。"""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.bind((host, port))
    except OSError:
        return False
    return True


def _port_usable(port: int) -> bool:
    """Chrome 还能不能在这个端口上开调试口。

    任一 loopback 能 bind 就行；两个都被别的程序占着才值得换端口。
    """
    return _can_bind("127.0.0.1", port) or _can_bind("::1", port)


_CHROME_LOG_NAME = "chrome-stderr.log"
_CHROME_LOG_MAX = 1 << 20


def _open_chrome_log(profile: Path, port: int):
    """给这次启动开一个追加写的 Chrome 日志。开不了返回 `None`，不拦主流程。"""
    path = profile / _CHROME_LOG_NAME
    try:
        if path.exists() and path.stat().st_size > _CHROME_LOG_MAX:
            path.unlink()
        fp = path.open("ab")
    except OSError as exc:
        logger.debug("Chrome 日志打不开（%s），stderr 丢弃：%s", path, exc)
        return None
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        fp.write(f"\n--- {stamp} launch port={port} ---\n".encode("utf-8"))
        fp.flush()
    except OSError:  # pragma: no cover - 写日志失败不该拦启动
        pass
    return fp


def _chrome_log_hint(profile: Path) -> str:
    """从 Chrome 日志里挑出解释「调试口为什么没开」的几行。"""
    try:
        text = (profile / _CHROME_LOG_NAME).read_text("utf-8", "replace")
    except OSError:
        return ""
    keys = ("bind(", "policy", "devtools listening", "remote debugging", "debug port")
    hits = [
        line.strip()
        for line in text.splitlines()
        if any(key in line.lower() for key in keys)
    ]
    return " | ".join(hits[-3:])[:500]


def _profile_populated(profile: Path) -> bool:
    """Chrome 到底有没有起来过：正常启动会写 `Local State` / `Default`。"""
    return (profile / "Local State").exists() or (profile / "Default").is_dir()


def launch_chrome(
    *,
    chrome: str | None = None,
    port: int | None = None,
    profile_dir: Path | str | None = None,
    mode: str | None = None,
) -> subprocess.Popen:
    """用独立 user-data-dir 拉起一台带调试口的 Chrome。

    必须独立 profile：Chrome 会拒绝对默认 profile 开调试口。拉起来的这台
    会记进册子，进程退出时自动关掉。

    Args:
        chrome: Chrome 可执行文件；省略则 `find_chrome`。
        port: 调试端口；省略读 `CDP_PORT_ENV`。
        profile_dir: 独立 user-data-dir；省略读 `CHROME_PROFILE_ENV`。
        mode: 窗口档位，见 `CHROME_MODES`；省略读 `CHROME_MODE_ENV`。
    """
    chrome = chrome or find_chrome()
    port = port or _port()
    mode = mode or _mode()
    profile = Path(profile_dir) if profile_dir else _profile_dir()
    try:
        profile.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StokenError(f"建 Chrome 专属 profile 失败 {profile}：{exc}") from exc

    cmd = [
        chrome,
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        f"--disable-features={_disable_features(mode)}",
        "--window-size=1280,900",
        *_mode_flags(mode),
        "about:blank",
    ]
    logger.info("拉起 Chrome（调试口 %s，profile %s，窗口 %s）", port, profile, mode)
    log = _open_chrome_log(profile, port)
    try:
        proc = subprocess.Popen(  # noqa: S603 - 参数是自己拼的固定列表
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=log if log is not None else subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    except OSError as exc:
        raise StokenError(f"拉不起 Chrome（{chrome}）：{exc}") from exc
    finally:
        if log is not None:
            log.close()
    if mode == "hidden":
        hidden = _hide_windows(proc.pid, proc=proc)
        if hidden:
            logger.info("已隐藏 %d 个 Chrome 窗口（pid %s）", hidden, proc.pid)
        else:
            logger.warning("没找到 pid %s 的 Chrome 窗口可隐藏，窗口会露出来", proc.pid)
    _track_launch(proc, port)
    return proc


def connect_or_launch(
    *,
    chrome: str | None = None,
    port: int | None = None,
    profile_dir: Path | str | None = None,
    mode: str | None = None,
    timeout: float = 30.0,
) -> "CdpClient":
    """连上现成的调试口，连不上就自己拉一台再等它起来。

    端口被占时自动往后挪；每次换端口都换一个 user-data-dir，免得 Chrome
    把启动交给还活着的旧实例。`mode` 只影响自拉那一支。

    Args:
        chrome: Chrome 可执行文件；省略则 `find_chrome`。
        port: 起始调试端口；省略读 `CDP_PORT_ENV`。
        profile_dir: 基础 user-data-dir；换端口时会在旁边另开一份。
        mode: 窗口档位（`CHROME_MODES`）。
        timeout: 首轮等调试口就绪的秒数。
    """
    first_port = port or _port()
    base_profile = Path(profile_dir) if profile_dir else _profile_dir()
    last_note = ""
    for offset in range(3):
        p = first_port + offset

        ws_url = probe_debug(p)
        if ws_url:
            try:
                client = CdpClient(ws_url)
            except StokenError as exc:
                last_note = f"端口 {p} 上的不是我们那台：{exc}"
                continue
            logger.debug("复用已有 Chrome 调试口 %s", p)
            return client

        if not _port_usable(p):
            last_note = f"端口 {p} 被别的程序占着"
            continue
        profile = base_profile
        if offset:
            profile = base_profile.with_name(f"{base_profile.name}-{p}")

        proc = launch_chrome(chrome=chrome, port=p, profile_dir=profile, mode=mode)
        deadline = time.time() + (timeout if offset == 0 else 12.0)
        while time.time() < deadline:
            ws_url = probe_debug(p, timeout=1.0)
            if ws_url:
                try:
                    client = CdpClient(ws_url)
                except StokenError as exc:
                    last_note = f"端口 {p} 上的不是我们那台：{exc}"
                    break
                logger.debug("自拉 Chrome 调试口 %s 就绪", p)
                _track_ws(p, ws_url)
                return client
            if proc.poll() is not None:
                code = proc.returncode
                if _profile_populated(profile):
                    last_note = (
                        f"端口 {p} 的启动进程提前退出（退出码 {code}），"
                        f"profile 被别的 Chrome 占着"
                    )
                else:
                    last_note = (
                        f"端口 {p} 的 Chrome 启动后立刻退出（退出码 {code}），"
                        f"profile 目录还是空的——多半被安全软件拦了，或 Chrome 装得不对"
                    )
                break
            time.sleep(0.25)
        else:
            last_note = f"端口 {p} 一直没开"

        if proc.poll() is None:
            _kill(proc)
        hint = _chrome_log_hint(profile)
        if hint:
            last_note += f"；Chrome 日志：{hint}"

    raise StokenError(
        f"调试口没开（试过 {first_port}~{first_port + 2}）。{last_note}\n"
        f"常见原因：端口被其他程序占着，或安全软件拦了 Chrome 的调试口。\n"
        f"先在任务管理器里结束所有 chrome.exe 再试；还不行就设 {CDP_PORT_ENV} 换个端口。"
    )


class CdpClient:
    """一条 browser 级 CDP WebSocket 连接（同步，够用即可）。

    只用少数几个命令：`Storage.setCookies/getCookies`、
    `Target.getTargets/createTarget/attachToTarget`、`Page.navigate`。
    不碰 cookie 存储文件，也不注入页面跑脚本——令牌由站点自己写。

    Args:
        ws_url: browser 级 WebSocket 地址。
        recv_timeout: 单次收发的默认超时（秒）。
    """

    def __init__(self, ws_url: str, *, recv_timeout: float = 30.0) -> None:
        """初始化。"""
        try:
            import websocket
        except ImportError as exc:  # pragma: no cover
            raise StokenError(
                "要走 CDP 得装 websocket-client：pip install websocket-client"
            ) from exc
        self._ws_url = ws_url
        self._recv_timeout = recv_timeout
        self._id = 0
        try:
            self._ws = websocket.create_connection(
                ws_url, timeout=recv_timeout, suppress_origin=True
            )
        except Exception as exc:  # noqa: BLE001
            raise StokenError(f"连不上 CDP（{ws_url}）：{exc}") from exc

    def call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """发一条 CDP 命令，等到它自己的回包（按 `id` 匹配，跳过事件）。

        Args:
            method: CDP 方法名。
            params: 命令参数。
            session_id: 页面级命令（`Page.navigate` 等）要带；browser 级（`Storage.*` / `Target.*`）不用。
            timeout: 本次等待超时（秒）；省略用连接默认值。
        """
        self._id += 1
        mine = self._id
        msg: dict[str, Any] = {"id": mine, "method": method, "params": dict(params or {})}
        if session_id:
            msg["sessionId"] = session_id
        try:
            self._ws.send(json.dumps(msg))
        except Exception as exc:  # noqa: BLE001
            raise StokenError(f"CDP 发送失败（{method}）：{exc}") from exc

        deadline = time.time() + (timeout or self._recv_timeout)
        while time.time() < deadline:
            try:
                raw = self._ws.recv()
            except Exception as exc:  # noqa: BLE001 - 超时/断连
                raise StokenError(f"CDP 等回包失败（{method}）：{exc}") from exc
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            if data.get("id") != mine:
                continue
            if "error" in data:
                err = data["error"]
                raise StokenError(f"CDP {method} 报错：{err.get('message', err)}")
            result = data.get("result")
            return result if isinstance(result, dict) else {}
        raise StokenError(f"CDP {method} 超时（{timeout or self._recv_timeout}s）")

    def close(self) -> None:
        """关掉 WebSocket 连接；失败忽略。"""
        try:
            self._ws.close()
        except Exception:  # noqa: BLE001 # pragma: no cover
            pass

    def __enter__(self) -> "CdpClient":
        """返回自身。"""
        return self

    def __exit__(self, *exc: object) -> None:
        """退出时关连接。"""
        self.close()

    def get_cookie(self, name: str) -> dict[str, Any] | None:
        """按名字取一枚 Cookie（整份结构，含 `expires`）。"""
        for cookie in self.get_cookies():
            if cookie.get("name") == name:
                return cookie
        return None

    def get_cookies(self) -> list[dict[str, Any]]:
        """取浏览器里全部 Cookie（只保留 dict 项）。"""
        result = self.call("Storage.getCookies")
        cookies = result.get("cookies") or []
        return [c for c in cookies if isinstance(c, dict)]

    def set_login_cookies(
        self,
        cookies: Mapping[str, str],
        *,
        domain: str = ".zhipin.com",
        path: str = "/",
    ) -> int:
        """把登录 Cookie 灌进 Chrome（免手工登）；`__zp_stoken__` 不灌。

        Returns:
            实际灌进去的枚数。
        """
        items = [
            {"name": name, "value": str(value), "domain": domain, "path": path}
            for name, value in cookies.items()
            if name not in _NON_COOKIE_KEYS and value
        ]
        if not items:
            return 0
        self.call("Storage.setCookies", {"cookies": items})
        logger.debug("已灌 %d 枚登录 Cookie 进 Chrome", len(items))
        return len(items)

    def wait_for_cookie(
        self,
        name: str,
        *,
        timeout: float = ACQUIRE_TIMEOUT,
        interval: float = 0.35,
    ) -> dict[str, Any]:
        """轮询到站点把这枚 Cookie 写出来为止。"""
        deadline = time.time() + timeout
        last_note = 0.0
        while time.time() < deadline:
            cookie = self.get_cookie(name)
            if cookie and cookie.get("value"):
                logger.info("站点已写出 %s（长度 %d）", name, len(str(cookie.get("value"))))
                return cookie
            if time.time() - last_note > 5.0:
                logger.debug("还没等到 %s…", name)
                last_note = time.time()
            time.sleep(interval)
        raise StokenError(
            f"等了 {timeout:.0f}s 也没等到站点写出 {name}。"
            f"多半是登录态没灌进去（``doc('session')`` 过期？）或页面没跑起来。"
        )

    def open_page(self, url: str, *, timeout: float = 20.0) -> tuple[str, str]:
        """打开（或跳到）某个页面，返回 `(target_id, session_id)`。

        已经有 zhipin 的 target 就复用它 `Page.navigate`，否则新开一个。
        不等页面 `load`——等的是 Cookie，够快。
        """
        target_id = self._find_zhipin_target()
        if target_id:
            session_id = self._attach(target_id)
            self.call("Page.navigate", {"url": url}, session_id=session_id)
            logger.debug("复用 target %s → %s", target_id[:12], url)
            return target_id, session_id

        result = self.call("Target.createTarget", {"url": url}, timeout=timeout)
        target_id = str(result.get("targetId") or "")
        if not target_id:
            raise StokenError("CDP 建页面没给 targetId")
        session_id = self._attach(target_id)
        logger.debug("新开 target %s → %s", target_id[:12], url)
        return target_id, session_id

    def clear_cookie(
        self,
        name: str,
        *,
        session_id: str,
        domain: str = ".zhipin.com",
        path: str = "/",
    ) -> None:
        """在页面上下文里把这枚 Cookie 过期掉，逼站点重新算一枚。

        CDP 的 `Storage` 域只有「全清」没有「按名删」，全清会把登录态一起端掉，
        所以在站点页面里改 `document.cookie` 的到期时间。
        """
        script = (
            f"document.cookie = {json.dumps(name)} + '=;expires=Thu, 01 Jan 1970 00:00:00 GMT;"
            f"path={path};domain={domain}'; 'cleared'"
        )
        self.call("Runtime.evaluate", {"expression": script}, session_id=session_id)
        logger.debug("已清掉旧的 %s", name)

    def reload(self, *, session_id: str) -> None:
        """重载当前页面（让站点 JS 重新跑一遍、重新算令牌）。"""
        self.call("Page.reload", {}, session_id=session_id)

    def _find_zhipin_target(self) -> str:
        """找 zhipin 或空白页 target 的 id；没有则空串。"""
        result = self.call("Target.getTargets")
        for info in result.get("targetInfos") or []:
            if not isinstance(info, dict):
                continue
            if info.get("type") not in {"page", "tab"}:
                continue
            url = str(info.get("url") or "")
            if "zhipin.com" in url or url in {"about:blank", ""}:
                return str(info.get("targetId") or "")
        return ""

    def _attach(self, target_id: str) -> str:
        """附到 target 上，返回 flatten 会话 id。"""
        result = self.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        return str(result.get("sessionId") or "")


@dataclass
class CdpStokenProvider:
    """fetch 用的 `__zp_stoken__` 供给器：判过期，过期才拉 Chrome。

    Attributes:
        http: 带 `cookies` 的会话；里面的登录 Cookie 会被灌进 Chrome。
        store: 过期账本；不传就用 `BOSS_DB` 的 `doc('stoken')`。
        session_path: 镜像进 `doc('session')` 的库路径；`None` = 不镜像。
        page_url: 跑站点 JS 取令牌的页面。
        timeout: 等站点写出 token 的超时（秒）。
        acquire: 换新令牌的实现（测试里替换掉真 Chrome）。
        port: 调试端口。
        chrome: Chrome 可执行文件。
        profile_dir: 独立 user-data-dir。
        mode: 自拉 Chrome 时的窗口档位（`CHROME_MODES`）。
        _last_renew_at: 上次真去 Chrome 换新的时间戳，给 `RENEW_COOLDOWN` 用。
    """

    http: Any
    store: StokenStore | None = None
    session_path: Path | str | None = None
    page_url: str = TOKEN_PAGE
    timeout: float = ACQUIRE_TIMEOUT
    acquire: Any | None = None
    port: int | None = None
    chrome: str | None = None
    profile_dir: Path | str | None = None
    mode: str | None = None
    _last_renew_at: float = 0.0

    def __post_init__(self) -> None:
        """补上默认的 `store` 与 `acquire`。"""
        if self.store is None:
            self.store = StokenStore()
        if self.acquire is None:
            self.acquire = self._acquire_from_chrome

    def ensure(self, *, force: bool = False) -> str:
        """确保会话里有一枚没过期的 `__zp_stoken__`，并返回它。

        顺序：账本新鲜直接用；账本没有但会话里已有就先用着；其余
        （过期 / `force`）拉 Chrome 换新并落盘。已是同一枚时不再 `_persist`；
        `force` 也有 `RENEW_COOLDOWN` 冷却。

        Args:
            force: 无视过期强制换新。
        """
        record = self.store.load() if self.store else None
        if not force:
            if record and record.is_fresh():
                logger.debug(
                    "%s 还新鲜（剩 %.0f 分钟），不拉 Chrome",
                    C.STOKEN_COOKIE,
                    record.ttl_left / 60,
                )
                if _read_cookie(self.http, C.STOKEN_COOKIE) != record.token:
                    self._persist(record.token, record.expires_at, record.minted_at, record.source)
                return record.token
            if record is None:
                existing = _read_cookie(self.http, C.STOKEN_COOKIE)
                if existing:
                    logger.debug("会话里已有 %s（手工拷的），先用着", C.STOKEN_COOKIE)
                    return existing

        if force and self._last_renew_at and record is not None and record.is_usable:
            since = time.time() - self._last_renew_at
            if since < RENEW_COOLDOWN:
                logger.info(
                    "%s 换新冷却中（%.0fs 前刚换过），先用现有这枚顶一下",
                    C.STOKEN_COOKIE,
                    since,
                )
                if _read_cookie(self.http, C.STOKEN_COOKIE) != record.token:
                    self._persist(record.token, record.expires_at, record.minted_at, record.source)
                return record.token

        reason = "强制换新" if force else ("已过期" if record else "还没有")
        logger.info("%s %s，拉 Chrome（CDP）重取一枚", C.STOKEN_COOKIE, reason)
        new = self._to_record(self.acquire())
        if not new.is_usable:
            raise StokenError(f"Chrome 那边没取到 {C.STOKEN_COOKIE}")
        self._last_renew_at = time.time()
        self._persist(new.token, new.expires_at, new.minted_at, new.source)
        return new.token

    @staticmethod
    def _to_record(raw: Any) -> StokenRecord:
        """`acquire` 的三种回包：CDP 的 Cookie 结构 / 记录 / 光字符串。"""
        if isinstance(raw, StokenRecord):
            return raw
        if isinstance(raw, Mapping):
            return StokenRecord.from_cookie(raw)
        return StokenRecord(
            token=str(raw or ""),
            minted_at=time.time(),
            expires_at=time.time() + STOKEN_MAX_AGE,
            source="cdp",
        )

    def _acquire_from_chrome(self) -> dict[str, Any]:
        """拉 Chrome 算一枚，返回 CDP 的 Cookie 结构（含 `expires`）。

        先把旧的过期掉再重载，逼站点重算。
        """
        client = connect_or_launch(
            chrome=self.chrome, port=self.port, profile_dir=self.profile_dir, mode=self.mode
        )
        try:
            login = _session_cookies(self.http)
            client.set_login_cookies(login)
            _, session_id = client.open_page(self.page_url)
            client.clear_cookie(C.STOKEN_COOKIE, session_id=session_id)
            client.reload(session_id=session_id)
            cookie = client.wait_for_cookie(C.STOKEN_COOKIE, timeout=self.timeout)
            if not cookie.get("value"):
                raise StokenError(f"站点写了 {C.STOKEN_COOKIE} 但值是空的")
            return dict(cookie)
        finally:
            if _keep_browser():
                client.close()
            else:
                try:
                    client.call("Browser.close", timeout=5.0)
                except StokenError:  # pragma: no cover - 已经退了也算
                    pass
                finally:
                    client.close()

    def _persist(self, token: str, expires_at: float, minted_at: float, source: str) -> None:
        """把令牌写进账本、会话 Cookie，并镜像回 `doc('session')`。"""
        record = StokenRecord(
            token=token, minted_at=minted_at, expires_at=expires_at, source=source
        )
        if self.store is not None:
            self.store.save(record)
        self._apply_cookie(token, expires_at)
        self._mirror_session(token)

    def _apply_cookie(self, token: str, expires_at: float) -> None:
        """把令牌写进会话 cookiejar（带过期时间）。"""
        from .stoken import put_cookie

        put_cookie(self.http.cookies, C.STOKEN_COOKIE, token, expires=int(expires_at))

    def _mirror_session(self, token: str) -> None:
        """把 token 写回 `doc('session')`，让 `http_from_session` 老规矩继续生效。"""
        if not self.session_path:
            return
        try:
            from boss_login.session import StoredSession, load_session, save_session
        except ImportError:  # pragma: no cover
            return
        path = Path(self.session_path)
        try:
            stored = load_session(path)
            cookies = dict(stored.cookies)
            cookies[C.STOKEN_COOKIE] = token
            save_session(
                StoredSession(
                    token=stored.token,
                    cookies=cookies,
                    phone_masked=stored.phone_masked,
                    user=stored.user,
                    saved_at=stored.saved_at,
                ),
                path,
            )
        except Exception as exc:  # noqa: BLE001 - 镜像失败不拦主流程
            logger.debug("镜像 %s 到 %s 失败（忽略）：%s", C.STOKEN_COOKIE, path, exc)


def _read_cookie(http: Any, name: str) -> str:
    """从会话 cookiejar 取一枚 cookie 的值；取不到或异常都返回空串。"""
    jar = getattr(http, "cookies", None)
    if jar is None:
        return ""
    getter = getattr(jar, "get", None)
    if getter is None:
        return ""
    try:
        return str(getter(name) or "")
    except Exception:  # noqa: BLE001 - cookiejar 对非法字符会抛
        return ""


def _session_cookies(http: Any) -> dict[str, str]:
    """从会话 cookiejar 里把登录 Cookie 抠出来。"""
    jar = getattr(http, "cookies", None)
    if jar is None:
        return {}
    out: dict[str, str] = {}
    try:
        for cookie in jar:
            name = getattr(cookie, "name", None)
            value = getattr(cookie, "value", None)
            if name and value:
                out[str(name)] = str(value)
    except Exception:  # noqa: BLE001 # pragma: no cover
        return {}
    return out
