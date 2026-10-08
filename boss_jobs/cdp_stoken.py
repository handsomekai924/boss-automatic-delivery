"""``__zp_stoken__`` 的**真浏览器**获取：Chrome CDP 拉起 → 站点自算 → 落盘 + 过期判断。

为什么不用 Node 硬算
--------------------------------------------------------------------------------
:mod:`boss_jobs.stoken` 那条「拿挑战 → 下 ``security-js`` → Node 跑 ``ABC.z``」
链路**算法是对的**，但服务端不认：``ABC.z`` 会顺手采一遍浏览器环境指纹
（canvas / plugins / screen / localStorage 设备 id …）编进 token，
光板 Node 的外壳跟真实 Chrome 对不上，打过去就是 ``code 37``。

所以这里换一条路：**让真 Chrome 自己把 token 算出来**。
Chrome 本来就是站点的原生环境，指纹、时区、UA 全自洽，不需要逆向、
不需要补指纹，更不需要碰 cookie 加密（Chrome 127+ 的
``app_bound_encrypted_key`` 那套也不用动）。

做法（CDP = Chrome DevTools Protocol）
--------------------------------------------------------------------------------
1. 找一台 Chrome（``BOSS_CHROME_BIN`` / 常见安装路径 / PATH）。
2. 先试连已有的调试端口 ``127.0.0.1:9222``；连不上就用**独立的 user-data-dir**
   拉起一台（``--remote-debugging-port`` 在默认 profile 上会被 Chrome 拒绝，
   这里专门开一个 ``.chrome_profile``，跟日常那台 Chrome 互不打架）。
3. ``Storage.setCookies`` 把 ``session.json`` 里的登录 Cookie 灌进去
   ——免得每次都要手工登一遍。
4. ``Target.createTarget`` 打开 ``https://www.zhipin.com/web/geek/jobs``，
   让站点自己的前端把 ``__zp_stoken__`` 算出来写进 Cookie（3840 分钟）。
5. 轮询 ``Storage.getCookies`` 把它读出来，连同 ``expires`` 一起落到
   ``stoken.json``。

⚠️ 边界
--------------------------------------------------------------------------------
* **只读 Cookie，不解密 Cookie。** 读靠 CDP 调试口，那台 Chrome 是自己拉起来的，
  用户知情。不去抠 Chrome 的 ``Cookies`` SQLite，更不去注入进程解
  ``app_bound_encrypted_key``。
* **拉起来的 Chrome 默认留着**（下次复用，秒连）。要它抓完就关，
  设 ``BOSS_CDP_KEEP=0``。
* **滑块/风控（code 36）不在这里处理。** 拿不到 token 就如实报错，
  让用户自己去浏览器过人机，客户端不去绕。

用法::

    from boss_jobs.cdp_stoken import CdpStokenProvider
    p = CdpStokenProvider(http=session)     # http 里带登录 Cookie
    token = p.ensure()                      # 有过期检查，过期才拉 Chrome
    token = p.ensure(force=True)            # 无视过期，强制换新
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from . import config as C
from .stoken import STOKEN_MAX_AGE, StokenError

logger = logging.getLogger(__name__)

__all__ = [
    "ACQUIRE_TIMEOUT",
    "CDP_PORT_ENV",
    "CHROME_BIN_ENV",
    "CHROME_KEEP_ENV",
    "CHROME_PROFILE_ENV",
    "CdpClient",
    "CdpStokenProvider",
    "DEFAULT_CDP_PORT",
    "DEFAULT_STORE_PATH",
    "StokenRecord",
    "StokenStore",
    "STOKEN_STORE_ENV",
    "TOKEN_PAGE",
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

#: 抓完是否留着 Chrome（``0``/``false`` = 关掉）。默认留着，下次秒连
CHROME_KEEP_ENV: str = "BOSS_CDP_KEEP"

#: 令牌落盘位置（含 minted_at / expires_at，用来判过期）。
#: ``BOSS_STOKEN_STORE`` 可覆盖——测试里就指到临时目录，别写真账本。
STOKEN_STORE_ENV: str = "BOSS_STOKEN_STORE"
DEFAULT_STORE_PATH: Path = C.PROJECT_ROOT / "stoken.json"

#: 快过期就提前换新的余量（秒）
EXPIRY_MARGIN: int = 5 * 60

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


# --------------------------------------------------------------------------- #
# 落盘：token + 过期时间
# --------------------------------------------------------------------------- #


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
    """``stoken.json`` 的读写：一次 fetch 一份过期账本。

    跟 ``session.json`` 分开放是有意的——``session.json`` 的 ``cookies``
    是纯 ``str → str``，塞不进过期时间；而过期判断恰恰是最关键的那一环。
    取出来的 token 会另外**镜像**一份进 ``session.json``，让
    :func:`boss_jobs.client.http_from_session` 老规矩继续生效。
    """

    def __init__(self, path: Path | str | None = None) -> None:
        if path is not None:
            self.path = Path(path)
        else:
            env = os.environ.get(STOKEN_STORE_ENV, "").strip()
            self.path = Path(env) if env else DEFAULT_STORE_PATH

    def load(self) -> StokenRecord | None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:  # pragma: no cover - 读盘失败不该拦主流程
            logger.warning("读 %s 失败（当没有）：%s", self.path, exc)
            return None
        try:
            payload = json.loads(raw)
        except ValueError:
            logger.warning("%s 不是合法 JSON，丢弃重取", self.path)
            return None
        return StokenRecord.from_dict(payload)

    def save(self, record: StokenRecord) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(record.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.path)
        except OSError as exc:  # pragma: no cover
            logger.warning("写 %s 失败（不拦主流程）：%s", self.path, exc)
        else:
            logger.debug("已落盘 %s（%s，剩 %.0f 分钟）", self.path, record.source, record.ttl_left / 60)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:  # pragma: no cover
            pass


# --------------------------------------------------------------------------- #
# Chrome / CDP
# --------------------------------------------------------------------------- #


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
) -> subprocess.Popen:
    """用独立 user-data-dir 拉起一台带调试口的 Chrome。

    ⚠️ 必须独立 profile：Chrome 136+ 直接拒绝对默认 profile 开
    ``--remote-debugging-port``。独立 profile 的登录态由本模块每次从
    ``session.json`` 灌进去，不用手工登。
    """
    chrome = chrome or find_chrome()
    port = port or _port()
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
        "--disable-features=Translate,MediaRouter",
        "--window-size=1280,900",
        "about:blank",
    ]
    logger.info("拉起 Chrome（调试口 %s，profile %s）", port, profile)
    try:
        proc = subprocess.Popen(  # noqa: S603 - 参数是自己拼的固定列表
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    except OSError as exc:
        raise StokenError(f"拉不起 Chrome（{chrome}）：{exc}") from exc
    return proc


def connect_or_launch(
    *,
    chrome: str | None = None,
    port: int | None = None,
    profile_dir: Path | str | None = None,
    timeout: float = 30.0,
) -> "CdpClient":
    """连上现成的调试口，连不上就自己拉一台再等它起来。

    端口被别的 Chrome 占着（握手还被拒）时，自动往后挪一两个端口，
    免得跟用户自己那台死磕。
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
        launch_chrome(chrome=chrome, port=p, profile_dir=profile_dir)
        deadline = time.time() + (timeout if offset == 0 else 12.0)
        while time.time() < deadline:
            ws_url = probe_debug(p, timeout=1.0)
            if ws_url:
                try:
                    logger.debug("自拉 Chrome 调试口 %s 就绪", p)
                    return CdpClient(ws_url)
                except StokenError as exc:
                    last_note = f"端口 {p} 上的不是我们那台：{exc}"
                    break  # 换下一个端口
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
            # suppress_origin：Chrome 111+ 会对陌生 Origin 回 403，
            # 调试口本来就是本地闭环，不发 Origin 头最干净。
            self._ws = websocket.create_connection(
                ws_url, timeout=recv_timeout, suppress_origin=True
            )
        except Exception as exc:  # noqa: BLE001
            raise StokenError(f"连不上 CDP（{ws_url}）：{exc}") from exc

    # ------------------------------------------------------------------ #
    # 基础：发一条命令等回包
    # ------------------------------------------------------------------ #

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

    # ------------------------------------------------------------------ #
    # Cookie
    # ------------------------------------------------------------------ #

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
        """把 ``session.json`` 里的登录 Cookie 灌进 Chrome（免手工登）。

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
            f"多半是登录态没灌进去（session.json 过期？）或页面没跑起来。"
        )

    # ------------------------------------------------------------------ #
    # 页面
    # ------------------------------------------------------------------ #

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


# --------------------------------------------------------------------------- #
# Provider：fetch 里那一环
# --------------------------------------------------------------------------- #


@dataclass
class CdpStokenProvider:
    """fetch 用的 ``__zp_stoken__`` 供给器：**判过期 → 过期才拉 Chrome**。

    跟 :class:`boss_jobs.stoken.StokenProvider` 同一个 ``ensure(force=)`` 接口，
    所以 :meth:`boss_jobs.client.JobClient.fetch_search_page` 的编排不用改。

    :param http: 带 ``cookies`` 的会话（``requests.Session``）。
        里面的登录 Cookie 会被灌进 Chrome，好让站点认得你。
    :param store: 过期账本；不传就用项目根 ``stoken.json``
    :param session_path: 镜像进 ``session.json`` 的路径；``None`` = 不镜像
    :param acquire: 换新令牌的实现（测试里替换掉真 Chrome）
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

    def __post_init__(self) -> None:
        if self.store is None:
            self.store = StokenStore()
        if self.acquire is None:
            self.acquire = self._acquire_from_chrome

    # ------------------------------------------------------------------ #
    # 一步到位
    # ------------------------------------------------------------------ #

    def ensure(self, *, force: bool = False) -> str:
        """确保会话里有一枚**没过期**的 ``__zp_stoken__``，并返回它。

        顺序：

        1. 账本里还新鲜 → 直接用（顺手写回 Cookie / ``session.json``）；
        2. 账本没有、但会话里已经有人（手工拷的）→ 信它，等撞 37 再说；
        3. 其余（过期 / ``force``）→ 拉 Chrome 换新，落盘。
        """
        record = self.store.load() if self.store else None
        if not force:
            if record and record.is_fresh():
                logger.debug(
                    "%s 还新鲜（剩 %.0f 分钟），不拉 Chrome",
                    C.STOKEN_COOKIE,
                    record.ttl_left / 60,
                )
                self._persist(record.token, record.expires_at, record.minted_at, record.source)
                return record.token
            if record is None:
                existing = _read_cookie(self.http, C.STOKEN_COOKIE)
                if existing:
                    logger.debug("会话里已有 %s（手工拷的），先用着", C.STOKEN_COOKIE)
                    return existing
        # 过期 / 强制 / 压根没有
        reason = "强制换新" if force else ("已过期" if record else "还没有")
        logger.info("%s %s，拉 Chrome（CDP）重取一枚", C.STOKEN_COOKIE, reason)
        new = self._to_record(self.acquire())
        if not new.is_usable:
            raise StokenError(f"Chrome 那边没取到 {C.STOKEN_COOKIE}")
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

    # ------------------------------------------------------------------ #
    # 真 Chrome 那一段
    # ------------------------------------------------------------------ #

    def _acquire_from_chrome(self) -> dict[str, Any]:
        """拉 Chrome 算一枚，返回 CDP 的 Cookie 结构（含 ``expires``）。"""
        client = connect_or_launch(
            chrome=self.chrome, port=self.port, profile_dir=self.profile_dir
        )
        try:
            login = _session_cookies(self.http)
            client.set_login_cookies(login)
            _, session_id = client.open_page(self.page_url)
            # 先把旧的过期掉再重载：否则站点看到 Cookie 还在就不重算，
            # force 换新时拿到的还是同一枚被拒过的令牌。
            client.clear_cookie(C.STOKEN_COOKIE, session_id=session_id)
            client.reload(session_id=session_id)
            cookie = client.wait_for_cookie(C.STOKEN_COOKIE, timeout=self.timeout)
            if not cookie.get("value"):
                raise StokenError(f"站点写了 {C.STOKEN_COOKIE} 但值是空的")
            return dict(cookie)
        finally:
            if _keep_browser():
                client.close()          # 只断开，Chrome 留着下次秒连
            else:
                try:
                    client.call("Browser.close", timeout=5.0)   # 让 Chrome 自己退
                except StokenError:  # pragma: no cover - 已经退了也算
                    pass
                finally:
                    client.close()

    # ------------------------------------------------------------------ #
    # 落盘
    # ------------------------------------------------------------------ #

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
        """把 token 写回 ``session.json``，让 ``http_from_session`` 老规矩继续生效。"""
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


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #


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
