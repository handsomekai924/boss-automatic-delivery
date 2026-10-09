"""``__zp_stoken__`` 安全网关令牌：来源、算法、全自动获取。

来源（逆自 ``.saved_web`` 里的 app~2.1a6c0514.js，模块 49657）
--------------------------------------------------------------------------------
站点前端把 ``__zp_stoken__`` 当成安全网关 Cookie。**它不是服务端发的**，
是浏览器本地算出来再回传的。整条链路：

1. 拿着登录态打搜索类接口（如 ``/wapi/zpgeek/search/joblist.json``），
   没带 ``__zp_stoken__`` 时服务端回 ``code:37``「您的环境存在异常.」，
   并在 ``zpData`` 里下发**一次性挑战**::

       {"code":37,"message":"您的环境存在异常.",
        "zpData":{"seed":"Em6sUKm1q2j+...=","name":"e948d594","ts":1790759239936}}

2. 前端把这三样写进 Cookie ``__zp_sseed__`` / ``__zp_sname__`` / ``__zp_sts__``
   （生成完立刻清掉），再去拉 **按次下发的生成脚本**::

       GET /web/common/security-js/{name}.js        ← name 就是挑战里的 name

3. 这个脚本往 ``window`` 上挂构造函数 ``ABC``。生成一行：

       token = new ABC().z(seed, parseInt(ts) + (480 + getTimezoneOffset()) * 60000)

   （北京时间 ``getTimezoneOffset() === -480``，括号归零，所以本地直接传
   ``ts`` 也一样；本模块按原式算，不依赖时区。）

4. 写 Cookie ``__zp_stoken__=<token>``，``max-age=3840*60``（3840 分钟），
   ``domain=.zhipin.com`` ``path=/``，然后把三份辅助 Cookie 清掉。

算法（逆自 security-js 静态结构 + 运行时追踪）
--------------------------------------------------------------------------------
``{name}.js`` 是两段 IIFE：

* **第一段**：开源的 `js-md5`（MD5 / HMAC-MD5，含 ``module.exports`` 出口）。
* **第二段**：obfuscator.io 风格的控制流扁平化 VM（``p=NNNN`` 计算 goto +
  ``switch`` 位域状态机），出口就是 ``window.ABC``。
  ``ABC`` 本身是空构造函数，``ABC.prototype.z`` 只是
  ``function(){ return l.apply(this, [581].concat(...args)) }``
  —— 581 是 VM 的入口块号。

输出形态：``0138`` 固定前缀 + 一段 **58 字符子集的类 base64** 编码。
剥掉末位校验字符后按标准 base64 解，起手四字节恒为 ``d35dfc81``
（``0138gQ`` / ``0138gR`` 等都落在这个魔数上），长度随 seed/ts 在
约 130–470 字节之间浮动。**同 seed+ts 连算两次结果不同**——生成器内部带
随机量，不是纯哈希。

运行时追踪显示 ``z()`` 还会采一遍 **浏览器环境指纹** 并编进 token：

| 采集点 | 用途 |
|---|---|
| ``document.createElement('canvas')`` + 2D 上下文 | canvas 指纹 |
| ``navigator.webkitTemporaryStorage`` / ``deviceMemory`` / ``hardwareConcurrency`` | 硬件特征 |
| ``navigator.plugins`` / ``mimeTypes`` / ``webdriver`` | 插件与自动化标记 |
| ``navigator.languages`` / ``language`` | 语言 |
| ``screen.availWidth/availHeight/width/height`` | 屏幕 |
| ``localStorage.getItem('c5jbelwo')`` | 持久设备 id |
| ``document.createElement('iframe')`` | 环境探测 |

所以**同一段 JS 在「真浏览器」和「光板 Node」里算出来的 token 不一样**，
服务端按指纹校验。本模块默认在 Node 里跑（带浏览器外壳），
:func:`compute_stoken` 的结果格式正确、可重复调用；
要让服务端认，指纹得跟请求头自洽（见 :mod:`boss_jobs.config` 的
``DEFAULT_HEADERS``，UA / 平台要跟外壳对得上）。

全自动获取
--------------------------------------------------------------------------------
>>> from boss_jobs.stoken import StokenProvider
>>> p = StokenProvider(http=session)          # 带登录态
>>> token = p.ensure()                        # 打一次拿挑战 → 算 → 写 Cookie
>>> token[:4]
'0138'

也可以只算不发请求::

    from boss_jobs.stoken import compute_stoken, load_security_js
    js = load_security_js(http, "e948d594")
    token = compute_stoken(js, seed="...", ts=1790759239936)
"""

from __future__ import annotations

import datetime
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from . import config as C

logger = logging.getLogger(__name__)

#: ``{name}.js`` 的下载地址模板
SECURITY_JS_URL: str = "/web/common/security-js/{name}.js"

#: 令牌 Cookie 的有效期（秒）= 前端写的 3840 分钟
STOKEN_MAX_AGE: int = 3840 * 60

#: 生成脚本的 Node 入口（跟本文件同目录的 assets/）
RUNNER_PATH: Path = Path(__file__).resolve().parent / "assets" / "run_abc.js"

#: 挑战三元组在 Cookie 里的临时名字（前端写完令牌就清掉）
HELPER_COOKIES: tuple[str, ...] = ("__zp_sseed__", "__zp_sname__", "__zp_sts__")

#: security-js 的本地缓存目录（每个 name 一份，脚本内容按 name 换）
DEFAULT_CACHE_DIR: Path = Path(tempfile.gettempdir()) / "boss_security_js"

#: Node 可执行文件；空 = 从 PATH 里找
NODE_BIN_ENV: str = "BOSS_NODE_BIN"

__all__ = [
    "HELPER_COOKIES",
    "SAMPLE_CHALLENGE",
    "StokenChallenge",
    "StokenError",
    "StokenProvider",
    "compute_stoken",
    "find_node",
    "load_security_js",
    "mint_offline",
    "parse_challenge",
    "put_cookie",
    "security_js_path",
]


class StokenError(RuntimeError):
    """``__zp_stoken__`` 获取失败。"""


@dataclass(frozen=True)
class StokenChallenge:
    """服务端下发的一次性挑战（code 37 的 ``zpData``）。"""

    seed: str
    name: str
    ts: int

    @property
    def is_complete(self) -> bool:
        return bool(self.seed) and bool(self.name) and bool(self.ts)


#: 离线铸币用的样例挑战（算法演示/计时用，服务端不会认，别拿去打接口）
SAMPLE_CHALLENGE: StokenChallenge = StokenChallenge(
    seed="Em6sUKm1q2j+AAA=", name="e948d594", ts=1790759239936
)


def parse_challenge(payload: Mapping[str, Any] | None) -> StokenChallenge | None:
    """从响应体的 ``zpData`` 里抠出挑战 ``{seed,name,ts}``。

    三个字段缺一、或 ``ts`` 不是能用的正整数，就返回 ``None``
    （调用方据此报「接口没给挑战」）。业务码本身不看——挑战只认
    ``zpData`` 的形状，code 37 只是它最常见的出场方式。
    """
    if not isinstance(payload, Mapping):
        return None
    zp = payload.get("zpData")
    if not isinstance(zp, Mapping):
        return None
    seed = zp.get("seed")
    name = zp.get("name")
    ts = zp.get("ts")
    if not seed or not name or ts is None:
        return None
    try:
        ts_int = int(ts)
    except (TypeError, ValueError):
        return None
    if ts_int <= 0:
        return None
    return StokenChallenge(seed=str(seed), name=str(name), ts=ts_int)


def find_node() -> str:
    """定位 Node 可执行文件。优先 ``BOSS_NODE_BIN``，否则从 PATH 找。"""
    env = os.environ.get(NODE_BIN_ENV, "").strip()
    if env:
        return env
    found = shutil.which("node") or shutil.which("node.exe")
    if not found:
        raise StokenError(
            f"找不到 node：算 {C.STOKEN_COOKIE} 要跑 security-js。"
            f"装好 Node.js，或用 {NODE_BIN_ENV} 指到 node 可执行文件。"
        )
    return found


def security_js_path(name: str, cache_dir: Path | str | None = None) -> Path:
    """某个 ``name`` 对应的 security-js 本地缓存路径。"""
    base = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name) or "_"
    return base / f"{safe}.js"


def load_security_js(
    http: Any,
    name: str,
    *,
    base_url: str = C.BASE_URL,
    cache_dir: Path | str | None = None,
    timeout: float = C.DEFAULT_TIMEOUT,
    force: bool = False,
) -> str:
    """下载 ``/web/common/security-js/{name}.js``，按 name 缓存到本地。

    这份脚本是**按挑战里的 name 现下发**的，站点随时可以换名换实现，
    所以不做算法硬编码，每次都认服务端给的那份。
    """
    path = security_js_path(name, cache_dir)
    if path.exists() and not force:
        try:
            cached = path.read_text(encoding="utf-8")
        except OSError:
            cached = ""
        if cached.strip():
            logger.debug("security-js 命中缓存 %s", path)
            return cached

    url = f"{base_url.rstrip('/')}{SECURITY_JS_URL.format(name=name)}"
    logger.info("下载 security-js：%s", url)
    try:
        resp = http.get(url, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - requests.RequestException 等
        raise StokenError(f"下载 security-js 失败：{exc}") from exc

    status = getattr(resp, "status_code", 0)
    text = getattr(resp, "text", "") or ""
    if status != 200 or not text.strip():
        raise StokenError(f"security-js 拿不到：HTTP {status}，长度 {len(text)}")

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as exc:  # pragma: no cover - 缓存写失败不该拦住主流程
        logger.warning("security-js 缓存写失败（忽略）：%s", exc)
    return text


def compute_stoken(
    script_source: str,
    *,
    seed: str,
    ts: int,
    tz_offset_minutes: int | None = None,
    name: str = "",
    cache_dir: Path | str | None = None,
    node_bin: str | None = None,
    timeout: float = 30.0,
) -> str:
    """在 Node 里跑 ``new ABC().z(seed, ts_adj)``，返回 ``__zp_stoken__``。

    :param script_source: ``{name}.js`` 的全文
    :param seed: 挑战里的 ``seed``
    :param ts: 挑战里的 ``ts``（毫秒）
    :param tz_offset_minutes: JS 的 ``getTimezoneOffset()``；
        默认取本机当前时区（跟浏览器行为一致）
    :param name: 挑战里的 ``name``，只用来给缓存文件命名
    """
    if not seed or ts is None:
        raise StokenError(f"算 {C.STOKEN_COOKIE} 缺 seed/ts")

    cache = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
    cache.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name) or "inline"
    script_path = cache / f"{safe}.js"
    script_path.write_text(script_source, encoding="utf-8")

    if tz_offset_minutes is None:
        tz_offset_minutes = _local_js_timezone_offset()

    node = node_bin or find_node()
    cmd = [node, str(RUNNER_PATH), str(script_path), seed, str(int(ts)), str(int(tz_offset_minutes))]
    logger.debug("跑 security-js：%s", cmd[:3] + ["…"])
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise StokenError(f"跑不了 node（{node}）：{exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise StokenError(f"security-js 跑超时（{timeout}s）") from exc

    token = (proc.stdout or b"").decode("utf-8", "replace").strip()
    if proc.returncode != 0 or not token:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()
        raise StokenError(f"security-js 生成失败（rc={proc.returncode}）：{err[:300]}")
    return token


def _local_js_timezone_offset() -> int:
    """JS ``Date.getTimezoneOffset()``：UTC 比本地慢多少分钟（北京 = -480）。"""
    offset = datetime.datetime.now().astimezone().utcoffset() or datetime.timedelta(0)
    return int(-offset.total_seconds() // 60)


@dataclass
class StokenProvider:
    """一条龙：拿挑战 → 下脚本 → 算 token → 写进会话 Cookie。

    :param http: 带 ``get()`` / ``cookies`` 的会话（一般是 ``requests.Session``）
    :param base_url: 站点地址
    :param cache_dir: security-js 缓存目录
    :param node_bin: Node 可执行文件；不传就从 PATH 找
    :param probe_path: 用来触发挑战的接口路径
    :param probe_params: 该接口的查询参数
    """

    http: Any
    base_url: str = C.BASE_URL
    cache_dir: Path | str | None = None
    node_bin: str | None = None
    probe_path: str = C.ENDPOINTS["job_search"]
    probe_params: Mapping[str, str] | None = None
    _script_cache: dict[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self._script_cache is None:
            self._script_cache = {}

    # ------------------------------------------------------------------ #
    # 分步
    # ------------------------------------------------------------------ #

    def fetch_challenge(self) -> StokenChallenge:
        """打一次探针接口，从 code 37 的 ``zpData`` 里取挑战。"""
        params = dict(self.probe_params or {"query": "", "page": "1", "scene": "1"})
        url = f"{self.base_url.rstrip('/')}{self.probe_path}"
        try:
            resp = self.http.get(url, params=params, timeout=C.DEFAULT_TIMEOUT)
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise StokenError(f"取 {C.STOKEN_COOKIE} 挑战失败：{exc}") from exc

        challenge = parse_challenge(payload)
        if challenge is None:
            code = (payload or {}).get("code") if isinstance(payload, Mapping) else "?"
            raise StokenError(
                f"接口没给 {C.STOKEN_COOKIE} 挑战（code={code}）。"
                f"可能登录态失效（code 7）、或账号被风控（code 36 需人机验证）。"
            )
        logger.info(
            "拿到 %s 挑战 name=%s ts=%s seed=%s…",
            C.STOKEN_COOKIE,
            challenge.name,
            challenge.ts,
            challenge.seed[:12],
        )
        return challenge

    def script_for(self, name: str, *, force: bool = False) -> str:
        """取某个 name 的 security-js（先内存、再磁盘、最后下载）。"""
        if not force and name in self._script_cache:
            return self._script_cache[name]
        src = load_security_js(
            self.http,
            name,
            base_url=self.base_url,
            cache_dir=self.cache_dir,
            force=force,
        )
        self._script_cache[name] = src
        return src

    def mint(self, challenge: StokenChallenge, *, force_script: bool = False) -> str:
        """拿挑战 + 脚本，算出 token（**不写** Cookie）。"""
        src = self.script_for(challenge.name, force=force_script)
        return compute_stoken(
            src,
            seed=challenge.seed,
            ts=challenge.ts,
            name=challenge.name,
            cache_dir=self.cache_dir,
            node_bin=self.node_bin,
        )

    def apply(self, token: str) -> None:
        """把 token 写进会话 Cookie（跟前端 ``a.A.set`` 同一套属性）。

        前端是 ``max-age=3840*60``；``requests`` 的 cookiejar 认的是绝对过期
        时间 ``expires``，这里换算过去。
        """
        put_cookie(
            self.http.cookies,
            C.STOKEN_COOKIE,
            token,
            expires=int(time.time()) + STOKEN_MAX_AGE,
        )
        logger.debug("已写入 %s（长度 %d）", C.STOKEN_COOKIE, len(token))

    # ------------------------------------------------------------------ #
    # 一步到位
    # ------------------------------------------------------------------ #

    def ensure(self, *, force: bool = False) -> str:
        """确保会话里有一个可用的 ``__zp_stoken__``，返回它。

        已有就直接复用（除非 ``force=True``）；没有就走一遍
        「拿挑战 → 算 → 写 Cookie」。
        """
        if not force:
            existing = _read_cookie(self.http, C.STOKEN_COOKIE)
            if existing:
                return existing

        challenge = self.fetch_challenge()
        token = self.mint(challenge)
        self.apply(token)
        return token


def put_cookie(
    jar: Any,
    name: str,
    value: str,
    *,
    domain: str = ".zhipin.com",
    path: str = "/",
    expires: int | None = None,
) -> None:
    """把一枚 Cookie 稳稳地写进 cookiejar：**先清同名，再写**。

    ``requests`` 的 jar 允许同名不同 domain 共存，而 ``http_from_session``
    装配时是不带 domain 写的（domain=``''``）。要是不清就补一枚
    domain=``.zhipin.com`` 的，请求头里会同时出现新旧两枚
    ``__zp_stoken__``，服务端照着旧的那枚拒——实测就是这么吃的 code 37。
    """
    if jar is None or not hasattr(jar, "set"):
        raise StokenError("会话对象不支持 cookies.set，没法写入 " + name)
    try:
        for cookie in list(jar):
            if getattr(cookie, "name", None) == name:
                try:
                    jar.clear(cookie.domain or "", cookie.path or "/", name)
                except Exception:  # noqa: BLE001 - 清不掉就盖写，下面再试
                    pass
    except Exception:  # noqa: BLE001 # pragma: no cover - 假 jar 不可迭代
        pass
    kwargs: dict[str, Any] = {"domain": domain, "path": path}
    if expires is not None:
        kwargs["expires"] = int(expires)
    try:
        jar.set(name, value, **kwargs)
    except TypeError:  # 简易假 jar 只认 set(name, value, **ignored)
        jar.set(name, value)


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


def mint_offline(
    *,
    challenge: StokenChallenge | None = None,
    cache_dir: Path | str | None = None,
    node_bin: str | None = None,
    timeout: float = 30.0,
) -> str:
    """不打网络、只用本地缓存的 security-js 铸一枚令牌。

    账号被风控（code 36）拿不到真挑战时，用它验证「算法通了」并量出
    纯铸币耗时。结果**结构合法但服务端不会认**（challenge 是样例的），
    别拿去打接口。

    :raises StokenError: 本地没有对应 ``{name}.js`` 时
    """
    ch = challenge or SAMPLE_CHALLENGE
    path = security_js_path(ch.name, cache_dir)
    if not path.exists():
        raise StokenError(
            f"本地没有 security-js 缓存 {path}："
            f"先跑一次在线获取，或把站点下发的 {ch.name}.js 放进去。"
        )
    return compute_stoken(
        path.read_text(encoding="utf-8"),
        seed=ch.seed,
        ts=ch.ts,
        name=ch.name,
        cache_dir=cache_dir,
        node_bin=node_bin,
        timeout=timeout,
    )
