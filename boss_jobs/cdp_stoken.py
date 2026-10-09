"""``__zp_stoken__`` 的**真浏览器**获取：Chrome CDP 拉起 → 站点自算 → 落盘 + 过期判断。

:mod:`boss_jobs.stoken` 那条 Node 硬算**算法对但服务端不认**——``ABC.z`` 把浏览器
环境指纹编进 token，光板 Node 外壳对不上就是 ``code 37``。这里让真 Chrome 自己算：
指纹/时区/UA 全自洽，不逆向、不补指纹、不动 cookie 加密。

做法（CDP）：找 Chrome（``BOSS_CHROME_BIN``/常见路径/PATH）→ 先试连已有
``127.0.0.1:9222``，连不上就用**独立 user-data-dir** 拉一台（默认 profile 拒开调试口）
→ ``Storage.setCookies`` 灌登录 Cookie → 开 ``/web/geek/jobs`` 让站点自己算 →
轮询读出，连 ``expires`` 落到 ``doc('stoken')``。

边界：**只读 Cookie 不解密**（不碰 ``Cookies`` SQLite / ``app_bound_encrypted_key``）；
**复用的那台绝不动**；**滑块/code 36 不绕**，拿不到就如实报错。拉起来的 Chrome
本进程内复用（``BOSS_CDP_KEEP=0`` 抓完就关），退出时 :func:`close_launched_browsers`
统一收走（``BOSS_CDP_CLOSE_ON_EXIT=0`` 可跨进程留着）。

窗口档位（``BOSS_CHROME_MODE``）**默认 ``hidden``**。**要人工过滑块必须改
``visible``**——隐藏窗口人够不着。``headless`` **别用**：一开页就被重定向到
verify.html（``code=36``），等不到 token（风控挑环境，跟渲染无关）。非 ``visible``
窗口会被判「被遮挡」，取令牌不受影响但站点 JS 可能被停画/冻定时器，故 ``hidden``
额外挂 ``--disable-backgrounding-occluded-windows`` 等三条。

用法见 :class:`CdpStokenProvider`（``ensure()`` 有过期检查，``force=True`` 强制换新）。
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import shutil
import signal
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

#: 调试端口（``--remote-debugging-port``）
CDP_PORT_ENV: str = "BOSS_CDP_PORT"
DEFAULT_CDP_PORT: int = 9222

#: Chrome 可执行文件；不设就按 常见安装路径 → PATH 的顺序找
CHROME_BIN_ENV: str = "BOSS_CHROME_BIN"

#: 独立 user-data-dir。**必须独立**：Chrome 在默认 profile 上会拒开调试口
CHROME_PROFILE_ENV: str = "BOSS_CHROME_PROFILE"
DEFAULT_CHROME_PROFILE: Path = C.PROJECT_ROOT / ".chrome_profile"

#: 抓完是否留着 Chrome（``0``/``false`` = 关掉）。默认留着，**本进程内**下次秒连；
#: 进程一退还是会被 :func:`close_launched_browsers` 收走（见下一条）
CHROME_KEEP_ENV: str = "BOSS_CDP_KEEP"

#: 进程退出时是否把我们拉起来的 Chrome 一起关掉（``0``/``false`` = 留着）。
#: 默认关：不然程序跑完，桌面上（或屏幕外）会挂着一台没人管的 Chrome
CHROME_CLOSE_ON_EXIT_ENV: str = "BOSS_CDP_CLOSE_ON_EXIT"

#: 拉起来的 Chrome 窗口怎么摆（见 :data:`CHROME_MODES`）
CHROME_MODE_ENV: str = "BOSS_CHROME_MODE"

#: 窗口可见性档位：``visible``（照常显示）/ ``offscreen``（挪到屏幕外）/
#: ``hidden``（Win32 隐藏窗口 + 关掉遮挡节流）/ ``headless``（无头）
CHROME_MODES: tuple[str, ...] = ("visible", "offscreen", "hidden", "headless")

#: 默认 ``hidden``。**要人工过滑块/人机验证时必须改回 ``visible``**——隐藏的
#: 窗口连任务栏里都没有，人够不着（``BOSS_CHROME_MODE=visible``）
DEFAULT_CHROME_MODE: str = "hidden"

#: 状态库路径（令牌账本含 minted_at / expires_at，用来判过期）。
#: 走 ``BOSS_DB`` / 显式参数，见 :func:`boss_db.resolve_db_path`。
DEFAULT_DB_PATH: Path = boss_db.DEFAULT_DB_PATH

#: 快过期就提前换新的余量（秒）
EXPIRY_MARGIN: int = 5 * 60

#: 强制换新的冷却（秒）。撞 code 37 有时只是「请求太快」；连环拉 Chrome 既慢
#: （每次 ~3s）又更容易惹出风控，冷却期内只把现有那枚写回 Cookie 重试，不真换。
RENEW_COOLDOWN: float = 5.0

#: 等站点写出 token 的超时（秒）。真浏览器首次开页要拉 chunk，给宽点
ACQUIRE_TIMEOUT: float = 40.0

#: 要 token 就得跑站点 JS 的那个页面
TOKEN_PAGE: str = f"{C.BASE_URL}/web/geek/jobs"

#: 登录态里不需要往 Chrome 灌的键（它们是 boss_login 自己的元数据）
_NON_COOKIE_KEYS: frozenset[str] = frozenset({"__zp_stoken__"})

#: 常见的 Chrome 安装路径（Windows / macOS / Linux）
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
    """一枚 ``__zp_stoken__`` 及其有效期。

    :param token: 令牌本体
    :param minted_at: 拿到手的 Unix 时间戳（秒）
    :param expires_at: 过期时间戳（秒）。站点给的是 Cookie ``expires``；
        没给就按前端的 ``max-age=3840*60`` 从 ``minted_at`` 推
    :param source: 怎么来的（``cdp`` / ``manual``）
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
        """从 CDP 的 Cookie 结构建一条记录（认 ``expires``，没有就按 max-age 推）。"""
        now = minted_at if minted_at is not None else time.time()
        raw = cookie.get("expires")
        try:
            expires_at = float(raw) if raw is not None else -1.0
        except (TypeError, ValueError):
            expires_at = -1.0
        if expires_at <= 0:  # 会话 Cookie / 没给 → 照前端 max-age 推
            expires_at = now + STOKEN_MAX_AGE
        return cls(
            token=str(cookie.get("value") or ""),
            minted_at=now,
            expires_at=expires_at,
            source=source,
        )

    @property
    def is_usable(self) -> bool:
        return bool(self.token)

    def is_fresh(self, *, margin: int = EXPIRY_MARGIN) -> bool:
        """还没到该换新的时候吗？（留 ``margin`` 秒余量）"""
        return self.is_usable and time.time() < (self.expires_at - margin)

    @property
    def ttl_left(self) -> float:
        return max(0.0, self.expires_at - time.time())

    def to_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "minted_at": self.minted_at,
            "expires_at": self.expires_at,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "StokenRecord | None":
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
    """``doc('stoken')`` 的读写：一次 fetch 一份过期账本。

    跟登录态分开放是有意的——``doc('session')`` 的 ``cookies`` 是纯
    ``str → str``，塞不进过期时间；而过期判断恰恰是最关键的那一环。
    取出来的 token 会另外**镜像**一份进 ``doc('session')``，让
    :func:`boss_jobs.client.http_from_session` 老规矩继续生效。

    :param path: 状态库路径；省略 = ``BOSS_DB`` = ``data/boss.db``
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = boss_db.resolve_db_path(path)

    def load(self) -> StokenRecord | None:
        try:
            raw = boss_db.doc_get_raw(boss_db.DOC_STOKEN, self.path)
        except (OSError, sqlite3.Error) as exc:  # 读盘失败不该拦主流程
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
        try:
            resolved = boss_db.doc_set(boss_db.DOC_STOKEN, record.to_dict(), self.path)
        except (OSError, sqlite3.Error) as exc:
            logger.warning("写 %s 失败（不拦主流程）：%s", self.path, exc)
        else:
            logger.debug(
                "已落盘 %s（%s，剩 %.0f 分钟）", resolved, record.source, record.ttl_left / 60
            )

    def clear(self) -> None:
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
    env = os.environ.get(CDP_PORT_ENV, "").strip()
    if env.isdigit():
        return int(env)
    return DEFAULT_CDP_PORT


def _profile_dir() -> Path:
    env = os.environ.get(CHROME_PROFILE_ENV, "").strip()
    return Path(env) if env else DEFAULT_CHROME_PROFILE


def _keep_browser() -> bool:
    return os.environ.get(CHROME_KEEP_ENV, "").strip().lower() not in {"0", "false", "no", "off"}


def _close_on_exit() -> bool:
    return os.environ.get(CHROME_CLOSE_ON_EXIT_ENV, "").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _mode() -> str:
    """窗口档位（:data:`CHROME_MODE_ENV`）。写错了退回 ``visible``。"""
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


#: ``offscreen`` 把窗口摆到这儿（``-32000`` 是 Win32 的保留值，别用）
_OFFSCREEN_POS: int = -10000


def _mode_flags(mode: str) -> list[str]:
    """按档位给窗口相关的启动参数。窗口本身的事全在这儿了。

    ``hidden`` 的大头是拉起来之后 Win32 ``SW_HIDE``；这里先摆到屏幕外，免得藏之前
    闪一下。窗口一旦不可见 Chrome 会停画 + 冻结后台定时器，站点 JS 可能算不动令牌，
    后面三条是专门的解药。
    """
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
    """``--disable-features`` 的值。同名开关给两次 Chrome 只认最后一个，得拼一起。

    ``hidden`` 多关一个 ``CalculateNativeWinOcclusion``：Windows 上 Chrome 靠它判
    「窗口被别的窗口盖住了」→ 停画。
    """
    names = ["Translate", "MediaRouter"]
    if mode == "hidden":
        names.append("CalculateNativeWinOcclusion")
    return ",".join(names)


_SW_HIDE: int = 0


def _hide_windows(pid: int, *, timeout: float = 10.0) -> int:
    """把这台 Chrome 的顶层窗口 ``SW_HIDE`` 掉，返回藏了几个。

    只在 Windows 上有意义（别处返回 0）。Chrome 开窗口比开调试口晚一拍，
    所以找不到就等到 ``timeout``，别以为藏好了。
    """
    if os.name != "nt":
        return 0
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    found: list[int] = []

    def _visit(hwnd: int, _lparam: int) -> bool:
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            user32.ShowWindow(hwnd, _SW_HIDE)
            found.append(hwnd)
        return True

    callback = enum_proc(_visit)
    deadline = time.time() + timeout
    while time.time() < deadline:
        found.clear()
        user32.EnumWindows(callback, 0)
        if found:
            return len(found)
        time.sleep(0.2)
    return 0




@dataclass
class _Launched:
    """本进程拉起来的一台 Chrome。

    **只登记自己拉的**——复用现成调试口那台（用户自己开的）绝不入册，
    退出一律不碰。
    """

    proc: subprocess.Popen
    port: int
    #: browser 级 ws 地址。尾巴那截 uuid 是每个浏览器实例独有的，
    #: 按它连回去就不会误伤后来占了这个端口的别人
    ws_url: str = ""


#: 自拉的浏览器（可能被 web 那边的后台线程改，加锁）
_launched: list[_Launched] = []
_launched_lock = threading.Lock()
_exit_hooks_installed = False

#: Win32 控制台事件。只接「进程一定活不下来」的那几个，见 :func:`_install_console_handler`
_CTRL_C_EVENT = 0
_CTRL_BREAK_EVENT = 1
_CTRL_CLOSE_EVENT = 2
_CTRL_LOGOFF_EVENT = 5
_CTRL_SHUTDOWN_EVENT = 6
_console_handler: Any = None

#: 「进程一定活不下来」的控制台事件，可以放心在这儿收尾。``CTRL_C_EVENT`` 刻意
#: 不在里面：那条路 Python 自己抛 ``KeyboardInterrupt`` 交给 ``atexit`` 收，万一
#: 上层吃掉了没退，提前关掉热着的 Chrome 纯属白关。``CTRL_BREAK_EVENT`` 必死
#: （退出码 ``0xC000013A``，Python 层收尾一律不跑），所以在这儿收。
_FATAL_CONSOLE_EVENTS: frozenset[int] = frozenset(
    (_CTRL_BREAK_EVENT, _CTRL_CLOSE_EVENT, _CTRL_LOGOFF_EVENT, _CTRL_SHUTDOWN_EVENT)
)


def _track_launch(proc: subprocess.Popen, port: int) -> None:
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

    不在 import 时挂：没拉过浏览器就没什么可收的，别给人家进程加负担。
    SIGTERM 的默认动作是直接退、``atexit`` 不跑，所以接过来走正常收尾；SIGINT
    不用接，Python 自己抛 ``KeyboardInterrupt``，``atexit`` 照跑。
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
        pass  # 平台没有 / 不在主线程
    _install_console_handler()


def _console_ctrl_handler(event: int) -> bool:
    """Win32 控制台事件回调（见 :func:`_install_console_handler` 的说明）。

    只在该收尾的事件上收尾；返回 False = 不拦，默认动作照跑。
    """
    if event in _FATAL_CONSOLE_EVENTS:
        close_launched_browsers(timeout=3.0)
    return False


def _install_console_handler() -> None:
    """Windows：控制台把进程硬干掉的那几条路，也把 Chrome 收走。

    ``CTRL_CLOSE_EVENT``（用户点终端窗口的 X）、``CTRL_LOGOFF_EVENT``、
    ``CTRL_SHUTDOWN_EVENT``、``CTRL_BREAK_EVENT`` 这几条都走不到 ``atexit``
    ——控制台直接干掉进程，实测退出码 ``0xC000013A``，Python 层收尾一律不跑。
    所以在这儿抢最后一次机会（``_FATAL_CONSOLE_EVENTS``）。

    ⚠️ ``CTRL_C_EVENT`` **不碰**，一律返回 FALSE 放行：Ctrl+C 那条路 Python 自己有
    处理器（抛 ``KeyboardInterrupt`` → ``atexit``），在这儿抢答会把 Ctrl+C 的手感
    搞坏。返回 FALSE 也让默认动作接着跑——我们只是先收个尾，不改变进程怎么死。
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
    close_launched_browsers(timeout=3.0)
    raise SystemExit(128 + signum)


def close_launched_browsers(*, timeout: float = 5.0) -> int:
    """把本进程拉起来的 Chrome 全关掉，返回关了几台。

    进程退出时由 ``atexit`` 自动调一次（``BOSS_CDP_CLOSE_ON_EXIT=0`` 关掉
    这个行为）；想在流程里提前收尾也可以直接调。

    先走 CDP ``Browser.close`` 让它自己退（profile 收得干净），没退就杀进程
    ——那是我们自己的子进程，杀了不牵连谁。**复用现成调试口那种一律不碰。**
    """
    with _launched_lock:
        pending, _launched[:] = list(_launched), []
    closed = 0
    for item in pending:
        if item.proc.poll() is not None:
            continue  # 早就自己退了
        if _shutdown_one(item, timeout=timeout):
            closed += 1
    if closed:
        logger.debug("关掉 %d 台自拉 Chrome", closed)
    return closed


def _shutdown_one(item: _Launched, *, timeout: float) -> bool:
    """关一台：优先 CDP ``Browser.close`` 让它自己退，退不掉就杀进程。"""
    if not item.ws_url:
        # 从没连上过（多半压根没起来），不用给它体面
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
    try:
        proc.kill()
    except OSError as exc:  # pragma: no cover - 退出路上，别让异常冒出去
        logger.debug("杀 pid %s 失败：%s", proc.pid, exc)
        return False
    return True


def _debug_http(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def probe_debug(port: int | None = None, *, timeout: float = 1.5) -> str:
    """问一下调试口在不在，在就返回 browser 级 WebSocket 地址。

    空字符串 = 没连上（Chrome 没起，或起了但没开调试口）。
    """
    port = port or _port()
    url = f"{_debug_http(port)}/json/version"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return ""
    ws = str(data.get("webSocketDebuggerUrl") or "")
    if not ws:
        logger.debug("调试口在，但没给 webSocketDebuggerUrl：%s", data)
    return ws


def launch_chrome(
    *,
    chrome: str | None = None,
    port: int | None = None,
    profile_dir: Path | str | None = None,
    mode: str | None = None,
) -> subprocess.Popen:
    """用独立 user-data-dir 拉起一台带调试口的 Chrome。

    ⚠️ 必须独立 profile：Chrome 136+ 直接拒绝对默认 profile 开
    ``--remote-debugging-port``。独立 profile 的登录态由本模块每次从
    状态库 ``doc('session')`` 灌进去，不用手工登。

    :param mode: 窗口档位，见 :data:`CHROME_MODES`；``None`` = 读
        :data:`CHROME_MODE_ENV`，再默认 ``hidden``。

    拉起来的这台会记进册子，**进程退出时自动关掉**（见
    :func:`close_launched_browsers`，``BOSS_CDP_CLOSE_ON_EXIT=0`` 可关掉）。
    ``hidden`` 档位会试着把窗口藏起来，藏不藏得上都继续——顶多用户看见一个后台
    窗口，令牌该拿还是能拿，不为这个拦掉整条链路。
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
        # Chrome 111+ 默认拒掉陌生 Origin 的 WS 握手；调试口是本地闭环，放开它
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
    try:
        proc = subprocess.Popen(  # noqa: S603 - 参数是自己拼的固定列表
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    except OSError as exc:
        raise StokenError(f"拉不起 Chrome（{chrome}）：{exc}") from exc
    if mode == "hidden":
        hidden = _hide_windows(proc.pid)
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

    端口被别的 Chrome 占着（握手还被拒）时，自动往后挪一两个端口，
    免得跟用户自己那台死磕。

    :param mode: 窗口档位（:data:`CHROME_MODES`）；只影响**自拉**那一支，
        复用现成的调试口时无从谈起——那台的窗口长什么样是它自己启动时定的。
    """
    first_port = port or _port()
    ws_url = probe_debug(first_port)
    if ws_url:
        try:
            client = CdpClient(ws_url)
        except StokenError as exc:
            # 端口在但握手被拒（多半是那台 Chrome 没开 --remote-allow-origins）
            logger.info("复用已有调试口 %s 失败（%s），自己拉一台", first_port, exc)
        else:
            logger.debug("复用已有 Chrome 调试口 %s", first_port)
            return client

    last_note = ""
    for offset in range(3):
        p = first_port + offset
        launch_chrome(chrome=chrome, port=p, profile_dir=profile_dir, mode=mode)
        deadline = time.time() + (timeout if offset == 0 else 12.0)
        while time.time() < deadline:
            ws_url = probe_debug(p, timeout=1.0)
            if ws_url:
                try:
                    client = CdpClient(ws_url)
                except StokenError as exc:
                    last_note = f"端口 {p} 上的不是我们那台：{exc}"
                    break  # 换下一个端口
                logger.debug("自拉 Chrome 调试口 %s 就绪", p)
                _track_ws(p, ws_url)
                return client
            time.sleep(0.25)
        else:
            last_note = f"端口 {p} 一直没开"
    raise StokenError(
        f"Chrome 起了但调试口没开（试过 {first_port}~{first_port + 2}）。{last_note}\n"
        f"换个 {CDP_PORT_ENV} 试试，或看 Chrome 是不是卡在启动页。"
    )


class CdpClient:
    """一条 browser 级 CDP WebSocket 连接（同步，够用即可）。

    只用少数几个命令：``Storage.setCookies/getCookies``、
    ``Target.getTargets/createTarget/attachToTarget``、``Page.navigate``。
    不碰 cookie 存储文件，也不注入页面跑脚本——令牌由站点自己写。
    连接不发 Origin 头（Chrome 111+ 会对陌生 Origin 回 403；调试口本来就是本地
    闭环，不发最干净）。
    """

    def __init__(self, ws_url: str, *, recv_timeout: float = 30.0) -> None:
        try:
            import websocket  # 延迟导入：没装也只是拿不到 CDP，不影响别的
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
        """发一条 CDP 命令，等到它自己的回包（按 ``id`` 匹配，跳过事件）。

        :param session_id: 页面级命令（``Page.navigate`` 等）要带；
            browser 级（``Storage.*`` / ``Target.*``）不用。
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
                continue  # 事件（Page.loadEventFired 之类），丢掉
            if "error" in data:
                err = data["error"]
                raise StokenError(f"CDP {method} 报错：{err.get('message', err)}")
            result = data.get("result")
            return result if isinstance(result, dict) else {}
        raise StokenError(f"CDP {method} 超时（{timeout or self._recv_timeout}s）")

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:  # noqa: BLE001 # pragma: no cover
            pass

    def __enter__(self) -> "CdpClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


    def get_cookie(self, name: str) -> dict[str, Any] | None:
        """按名字取一枚 Cookie（整份结构，含 ``expires``）。"""
        for cookie in self.get_cookies():
            if cookie.get("name") == name:
                return cookie
        return None

    def get_cookies(self) -> list[dict[str, Any]]:
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
        """把 ``doc('session')`` 里的登录 Cookie 灌进 Chrome（免手工登）。

        只灌真正的登录态，``__zp_stoken__`` 不灌——那枚要让站点自己算，
        否则指纹还是对不上。
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
        """打开（或跳到）某个页面，返回 ``(target_id, session_id)``。

        已经有 zhipin 的 target 就复用它 ``Page.navigate``，否则新开一个。
        不等页面 ``load``——等的是 Cookie，够快。
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

        CDP 的 ``Storage`` 域只有「全清」没有「按名删」，全清会把登录态
        一起端掉；所以在站点页面里改 ``document.cookie`` 的到期时间。
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
        result = self.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        return str(result.get("sessionId") or "")




@dataclass
class CdpStokenProvider:
    """fetch 用的 ``__zp_stoken__`` 供给器：**判过期 → 过期才拉 Chrome**。

    跟 :class:`boss_jobs.stoken.StokenProvider` 同一个 ``ensure(force=)`` 接口，
    所以 :meth:`boss_jobs.client.JobClient.fetch_search_page` 的编排不用改。

    :param http: 带 ``cookies`` 的会话（``requests.Session``）。
        里面的登录 Cookie 会被灌进 Chrome，好让站点认得你。
    :param store: 过期账本；不传就用 ``BOSS_DB`` 的 ``doc('stoken')``
    :param session_path: 镜像进 ``doc('session')`` 的库路径；``None`` = 不镜像
    :param acquire: 换新令牌的实现（测试里替换掉真 Chrome）
    :param mode: 自拉 Chrome 时的窗口档位（:data:`CHROME_MODES`）；
        ``None`` = 读 :data:`CHROME_MODE_ENV`
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
    #: 上次真去 Chrome 换新的时间戳（秒），给 :data:`RENEW_COOLDOWN` 用
    _last_renew_at: float = 0.0

    def __post_init__(self) -> None:
        if self.store is None:
            self.store = StokenStore()
        if self.acquire is None:
            self.acquire = self._acquire_from_chrome


    def ensure(self, *, force: bool = False) -> str:
        """确保会话里有一枚**没过期**的 ``__zp_stoken__``，并返回它。

        顺序：

        1. 账本里还新鲜 → 直接用（Cookie 里还不是这枚才写回去）；
        2. 账本没有、但会话里已经有人（手工拷的）→ 信它，等撞 37 再说；
        3. 其余（过期 / ``force``）→ 拉 Chrome 换新，落盘。

        两处**刻意不动**，免得日志/状态库被刷屏：

        - 已经是这枚令牌时不再 ``_persist``（否则每条请求都写一遍
          ``doc('session')``，日志上就是一行行「登录态已保存」）；
        - ``force`` 也有冷却（:data:`RENEW_COOLDOWN`）——37 有时只是「太快了」，
          连环拉 Chrome 既慢又更容易撞风控。冷却起点刻意取**本进程**的
          ``_last_renew_at`` 而非 ``record.minted_at``：后者是落盘时间，手工拷的
          或上次进程留下的都算，会把「真强制换新」也误杀掉。
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
        """``acquire`` 的三种回包：CDP 的 Cookie 结构 / 记录 / 光字符串。"""
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
        """拉 Chrome 算一枚，返回 CDP 的 Cookie 结构（含 ``expires``）。

        打开页面后**先把旧的过期掉再重载**：否则站点看到 Cookie 还在就不重算，
        ``force`` 换新时拿到的还是同一枚被拒过的令牌。
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
                client.close()          # 只断开，Chrome 留着本进程内下次秒连
            else:
                try:
                    client.call("Browser.close", timeout=5.0)   # 让 Chrome 自己退
                except StokenError:  # pragma: no cover - 已经退了也算
                    pass
                finally:
                    client.close()


    def _persist(self, token: str, expires_at: float, minted_at: float, source: str) -> None:
        record = StokenRecord(
            token=token, minted_at=minted_at, expires_at=expires_at, source=source
        )
        if self.store is not None:
            self.store.save(record)
        self._apply_cookie(token, expires_at)
        self._mirror_session(token)

    def _apply_cookie(self, token: str, expires_at: float) -> None:
        from .stoken import put_cookie  # 延迟导入

        put_cookie(self.http.cookies, C.STOKEN_COOKIE, token, expires=int(expires_at))

    def _mirror_session(self, token: str) -> None:
        """把 token 写回 ``doc('session')``，让 ``http_from_session`` 老规矩继续生效。"""
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
    """从会话 cookiejar 里把 ``.zhipin.com`` 的登录 Cookie 抠出来。"""
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
