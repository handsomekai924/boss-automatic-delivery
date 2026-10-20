"""`__zp_stoken__` 的 CDP 取法：过期账本 / fetch 时自动续期。

真 Chrome 那一段（拉浏览器 → 灌登录 Cookie → 开页 → 读 Cookie）在
`boss_jobs.cdp_stoken` 里，单测不碰真浏览器（见 `conftest.py` 的
`isolated_db`），只验「什么时候该去换新、换了怎么落盘」。
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

import boss_db
import boss_jobs.cdp_stoken as cdp
from boss_jobs.cdp_stoken import (
    RENEW_COOLDOWN,
    CdpStokenProvider,
    StokenRecord,
    StokenStore,
    find_chrome,
)
from boss_jobs.stoken import STOKEN_MAX_AGE
from boss_jobs.stoken import StokenError


class FakeCookieJar:
    """Cookie 罐替身。"""

    def __init__(self) -> None:
        """初始化。"""
        self._data: dict[str, str] = {}
        self._meta: dict[str, dict] = {}

    def set(self, name, value, **kwargs):
        """写入 Cookie。"""
        self._data[name] = value
        self._meta[name] = kwargs

    def get(self, name, default=None):
        """读取 Cookie。"""
        return self._data.get(name, default)

    def __iter__(self):
        """返回迭代器。"""
        for name, value in self._data.items():
            yield type("C", (), {"name": name, "value": value})()


class FakeHttp:
    """假 HTTP 会话。"""

    def __init__(self, cookies: dict[str, str] | None = None) -> None:
        """初始化。"""
        self.cookies = FakeCookieJar()
        for name, value in (cookies or {}).items():
            self.cookies.set(name, value)


def test_记录_站点给了expires就用它():
    """验证记录就站点给了 expires 就用它。"""
    now = time.time()
    rec = StokenRecord.from_cookie(
        {"name": "__zp_stoken__", "value": "0138X", "expires": now + 3600},
        minted_at=now,
    )
    assert rec.token == "0138X"
    assert rec.expires_at == pytest.approx(now + 3600)
    assert rec.source == "cdp"


def test_记录_会话Cookie没有expires按max_age推():
    """CDP 读到的会话 Cookie `expires` 是 -1，这时按前端的 3840 分钟推。"""
    now = time.time()
    rec = StokenRecord.from_cookie(
        {"name": "__zp_stoken__", "value": "0138X", "expires": -1}, minted_at=now
    )
    assert rec.expires_at == pytest.approx(now + STOKEN_MAX_AGE)


def test_记录_过期判断留了余量():
    """验证记录就过期判断留了余量。"""
    now = time.time()
    fresh = StokenRecord(token="t", minted_at=now, expires_at=now + 600)
    assert fresh.is_fresh()
    almost = StokenRecord(token="t", minted_at=now, expires_at=now + 60)
    assert not almost.is_fresh()
    dead = StokenRecord(token="t", minted_at=now - 1000, expires_at=now - 1)
    assert not dead.is_fresh()


def test_记录_空token不算可用():
    """验证记录就空 token 不算可用。"""
    rec = StokenRecord(token="", minted_at=time.time(), expires_at=time.time() + 100)
    assert not rec.is_usable
    assert not rec.is_fresh()


def test_记录_字典往返():
    """验证记录就字典往返。"""
    rec = StokenRecord(token="0138A", minted_at=100.0, expires_at=200.0, source="manual")
    again = StokenRecord.from_dict(rec.to_dict())
    assert again == rec
    assert StokenRecord.from_dict(None) is None
    assert StokenRecord.from_dict({"token": ""}) is None


def test_账本_读写往返(tmp_path: Path):
    """验证账本就读写往返。"""
    db = tmp_path / "boss.db"
    store = StokenStore(db)
    assert store.load() is None
    rec = StokenRecord(token="0138S", minted_at=time.time(), expires_at=time.time() + 100)
    store.save(rec)
    again = store.load()
    assert again is not None and again.token == "0138S"
    assert store.path == db
    assert boss_db.doc_get_raw(boss_db.DOC_STOKEN, db) is not None


def test_账本_坏payload当没有(tmp_path: Path):
    """验证账本就坏 payload 当没有。"""
    db = tmp_path / "boss.db"
    boss_db.doc_set(boss_db.DOC_STOKEN, "不是 JSON", db)
    assert StokenStore(db).load() is None


def test_账本_非对象当没有(tmp_path: Path):
    """验证账本就非对象当没有。"""
    db = tmp_path / "boss.db"
    boss_db.doc_set(boss_db.DOC_STOKEN, "[1, 2]", db)
    assert StokenStore(db).load() is None


def test_账本_能清掉(tmp_path: Path):
    """验证账本就能清掉。"""
    store = StokenStore(tmp_path / "boss.db")
    store.save(StokenRecord(token="t", minted_at=time.time(), expires_at=time.time() + 10))
    store.clear()
    assert store.load() is None
    store.clear()


def provider(tmp_path: Path, http: FakeHttp | None = None, **kwargs) -> CdpStokenProvider:
    """提供可注入的假挑战提供方。"""
    store = StokenStore(tmp_path / "boss.db")
    http = http or FakeHttp()
    return CdpStokenProvider(http=http, store=store, acquire=lambda: "0138NEW", **kwargs)


def test_ensure_账本新鲜就直接用(tmp_path: Path):
    """没过期就不该拉 Chrome——这是 fetch 每次都会走的那一支。"""
    store = StokenStore(tmp_path / "boss.db")
    store.save(StokenRecord(token="0138OLD", minted_at=time.time(), expires_at=time.time() + 9999))
    calls: list[str] = []

    p = CdpStokenProvider(
        http=FakeHttp(),
        store=store,
        acquire=lambda: calls.append("acquired") or "0138NEW",
    )
    assert p.ensure() == "0138OLD"
    assert calls == []
    assert p.http.cookies.get("__zp_stoken__") == "0138OLD"


def test_ensure_过期了才换新(tmp_path: Path):
    """验证 `ensure` 过期了才换新。"""
    store = StokenStore(tmp_path / "boss.db")
    store.save(StokenRecord(token="0138OLD", minted_at=time.time() - 9999, expires_at=time.time() - 1))
    calls: list[str] = []

    p = CdpStokenProvider(
        http=FakeHttp(),
        store=store,
        acquire=lambda: calls.append("acquired") or "0138NEW",
    )
    assert p.ensure() == "0138NEW"
    assert calls == ["acquired"]
    assert store.load().token == "0138NEW"
    assert p.http.cookies.get("__zp_stoken__") == "0138NEW"


def test_ensure_强制换新(tmp_path: Path):
    """撞上 code 37 时会 `ensure(force=True)`，账本再新鲜也得换。"""
    store = StokenStore(tmp_path / "boss.db")
    store.save(StokenRecord(token="0138OLD", minted_at=time.time(), expires_at=time.time() + 9999))
    calls: list[str] = []

    p = CdpStokenProvider(
        http=FakeHttp(),
        store=store,
        acquire=lambda: calls.append("acquired") or "0138NEW",
    )
    assert p.ensure(force=True) == "0138NEW"
    assert calls == ["acquired"]


def test_ensure_令牌没变就别再写盘(tmp_path: Path):
    """`ensure()` 每条请求都会走；Cookie 里已经是这枚就别再 `_persist`——
    否则日志上就是一行行「登录态已保存」刷屏（fetch.log 那个循环重复）。
    """
    session_db = tmp_path / "session.db"
    store = StokenStore(tmp_path / "boss.db")
    store.save(StokenRecord(token="0138OLD", minted_at=time.time(), expires_at=time.time() + 9999))
    http = FakeHttp()
    p = CdpStokenProvider(http=http, store=store, acquire=lambda: "0138NEW", session_path=session_db)

    writes: list[str] = []
    original = p._persist

    def counting_persist(*args, **kwargs):
        """计数的落盘回调。"""
        writes.append(args[0] if args else kwargs.get("token"))
        return original(*args, **kwargs)

    p._persist = counting_persist  # type: ignore[method-assign]

    assert p.ensure() == "0138OLD"
    assert writes == ["0138OLD"]
    assert p.ensure() == "0138OLD"
    assert writes == ["0138OLD"]
    assert p.ensure() == "0138OLD"
    assert writes == ["0138OLD"]


def test_ensure_强制换新有冷却(tmp_path: Path):
    """刚换过就别连环拉 Chrome——37 有时只是太快，连环换新又慢又更容易撞风控。"""
    store = StokenStore(tmp_path / "boss.db")
    calls: list[str] = []
    p = CdpStokenProvider(
        http=FakeHttp(),
        store=store,
        acquire=lambda: calls.append("acquired") or "0138NEW",
    )
    assert p.ensure(force=True) == "0138NEW"
    assert calls == ["acquired"]

    assert p.ensure(force=True) == "0138NEW"
    assert calls == ["acquired"]

    p._last_renew_at = time.time() - RENEW_COOLDOWN - 1
    assert p.ensure(force=True) == "0138NEW"
    assert calls == ["acquired", "acquired"]


def test_ensure_冷却只认本进程换过的(tmp_path: Path):
    """`_last_renew_at` 是 0（本进程还没换过）时不该冷却——
    否则「真的强制换新」会被上次落盘的 `minted_at` 误杀。
    """
    store = StokenStore(tmp_path / "boss.db")
    store.save(StokenRecord(token="0138OLD", minted_at=time.time(), expires_at=time.time() + 9999))
    calls: list[str] = []
    p = CdpStokenProvider(
        http=FakeHttp(),
        store=store,
        acquire=lambda: calls.append("acquired") or "0138NEW",
    )
    assert p.ensure(force=True) == "0138NEW"
    assert calls == ["acquired"]


def test_ensure_账本没有但会话里有就信它(tmp_path: Path):
    """手工从浏览器拷进 `doc('session')` 的那枚：没有过期时间戳，先用着，
    撞 37 再说（别抢着覆盖用户的活）。
    """
    calls: list[str] = []
    p = CdpStokenProvider(
        http=FakeHttp({"__zp_stoken__": "0138MANUAL"}),
        store=StokenStore(tmp_path / "boss.db"),
        acquire=lambda: calls.append("acquired") or "0138NEW",
    )
    assert p.ensure() == "0138MANUAL"
    assert calls == []


def test_ensure_什么都没有就去取(tmp_path: Path):
    """验证 `ensure` 什么都没有就去取。"""
    calls: list[str] = []
    p = CdpStokenProvider(
        http=FakeHttp(),
        store=StokenStore(tmp_path / "boss.db"),
        acquire=lambda: calls.append("acquired") or "0138NEW",
    )
    assert p.ensure() == "0138NEW"
    assert calls == ["acquired"]


def test_ensure_取回来是空的就报错(tmp_path: Path):
    """验证 `ensure` 取回来是空的就报错。"""
    p = CdpStokenProvider(
        http=FakeHttp(),
        store=StokenStore(tmp_path / "boss.db"),
        acquire=lambda: "",
    )
    with pytest.raises(StokenError):
        p.ensure()


def test_ensure_会话对象不能写cookie就报错(tmp_path: Path):
    """验证 `ensure` 会话对象不能写 cookie 就报错。"""
    class NoCookieJar:
        """不支持 set 的 Cookie 罐。"""

        cookies = None

    p = CdpStokenProvider(
        http=NoCookieJar(),
        store=StokenStore(tmp_path / "boss.db"),
        acquire=lambda: "0138NEW",
    )
    with pytest.raises(StokenError):
        p.ensure()


def test_ensure_镜像进登录态(tmp_path: Path):
    """取到的 token 要写回 `doc('session')`，让 http_from_session 老规矩继续生效。"""
    from boss_login.session import StoredSession, load_session, save_session

    session_db = tmp_path / "session.db"
    save_session(
        StoredSession(
            token="tok",
            cookies={"wt2": "w"},
            phone_masked="138****0000",
            user={"userId": 1},
            saved_at=time.time(),
        ),
        session_db,
    )

    p = CdpStokenProvider(
        http=FakeHttp(),
        store=StokenStore(tmp_path / "boss.db"),
        acquire=lambda: "0138MIRROR",
        session_path=session_db,
    )
    assert p.ensure() == "0138MIRROR"

    stored = load_session(session_db)
    assert stored.cookies["__zp_stoken__"] == "0138MIRROR"
    assert stored.cookies["wt2"] == "w"


def test_不镜像时不碰登录态(tmp_path: Path):
    """session_path 不给就只落 `doc('stoken')`，别乱写登录态。"""
    session_db = tmp_path / "session.db"
    p = CdpStokenProvider(
        http=FakeHttp(),
        store=StokenStore(tmp_path / "boss.db"),
        acquire=lambda: "0138X",
    )
    p.ensure()
    assert boss_db.doc_get_raw(boss_db.DOC_SESSION, session_db) is None


def test_find_chrome_认环境变量(tmp_path: Path, monkeypatch):
    """验证 `find_chrome` 认环境变量。"""
    fake = tmp_path / "chrome.exe"
    fake.write_text("stub", encoding="utf-8")
    monkeypatch.setenv("BOSS_CHROME_BIN", str(fake))
    assert find_chrome() == str(fake)


def test_find_chrome_环境变量指空文件就报错(tmp_path: Path, monkeypatch):
    """验证 `find_chrome` 环境变量指空文件就报错。"""
    monkeypatch.setenv("BOSS_CHROME_BIN", str(tmp_path / "没这东西.exe"))
    with pytest.raises(StokenError):
        find_chrome()


def test_档位_默认是hidden():
    """默认不让窗口露出来。要人工过验证码才显式设 visible。"""
    assert cdp.DEFAULT_CHROME_MODE == "hidden"
    assert cdp._mode() == "hidden"


def test_档位_认环境变量(monkeypatch):
    """验证档位就认环境变量。"""
    monkeypatch.setenv("BOSS_CHROME_MODE", "VISIBLE")
    assert cdp._mode() == "visible"


def test_档位_写错了退回默认(monkeypatch):
    """验证档位就写错了退回默认。"""
    monkeypatch.setenv("BOSS_CHROME_MODE", "隐形")
    assert cdp._mode() == cdp.DEFAULT_CHROME_MODE


def test_档位_offscreen把窗口摆到屏幕外():
    """验证档位 offscreen 把窗口摆到屏幕外。"""
    flags = cdp._mode_flags("offscreen")
    assert any(f.startswith("--window-position=-") for f in flags)
    assert "--disable-backgrounding-occluded-windows" not in flags


def test_档位_hidden要关掉遮挡节流():
    """窗口被 SW_HIDE 掉之后 Chrome 会停画 + 冻结后台定时器，
    不关掉这三样站点 JS 可能就算不动令牌。"""
    flags = cdp._mode_flags("hidden")
    assert "--disable-backgrounding-occluded-windows" in flags
    assert "--disable-renderer-backgrounding" in flags
    assert "--disable-background-timer-throttling" in flags
    assert "CalculateNativeWinOcclusion" in cdp._disable_features("hidden")


def test_档位_headless不给窗口():
    """验证档位 headless 不给窗口。"""
    assert cdp._mode_flags("headless") == ["--headless=new"]


def test_档位_disable_features只出一个开关():
    """同名开关给两次 Chrome 只认最后一个，译文那个不能被顶掉。"""
    assert cdp._disable_features("visible") == "Translate,MediaRouter"
    assert cdp._disable_features("headless") == "Translate,MediaRouter"
    assert "Translate" in cdp._disable_features("hidden")


def test_档位_透传到launch_chrome(monkeypatch):
    """provider 上写的 mode 要一路走到 launch_chrome。"""
    seen: dict = {}

    class _Boom(Exception):
        """一拉起来就跳出 connect_or_launch，省得空转等超时。"""

    def fake_launch(**kwargs):
        """假浏览器拉起。"""
        seen.update(kwargs)
        raise _Boom

    monkeypatch.setattr(cdp, "launch_chrome", fake_launch)
    monkeypatch.setattr(cdp, "probe_debug", lambda *a, **k: "")
    monkeypatch.setattr(cdp, "_port_usable", lambda p: True)

    with pytest.raises(_Boom):
        cdp.connect_or_launch(mode="hidden")
    assert seen["mode"] == "hidden"


def test_档位_复用现成调试口时不管档位(monkeypatch):
    """端口上已经有一台时是直接复用，档位无从谈起——别去动它。"""
    monkeypatch.setattr(cdp, "probe_debug", lambda *a, **k: "ws://127.0.0.1:1/devtools/browser/x")
    monkeypatch.setattr(cdp, "CdpClient", lambda *a, **k: object())

    def boom(**kwargs):
        """抛异常的桩。"""
        raise AssertionError("复用时不该拉新 Chrome")

    monkeypatch.setattr(cdp, "launch_chrome", boom)
    assert cdp.connect_or_launch(mode="headless") is not None


class _Resp:
    """探测调试口时假造的 HTTP 响应，只提供 `read` / 上下文管理器。"""

    def __init__(self, body: bytes) -> None:
        """初始化。"""
        self.body = body

    def read(self) -> bytes:
        """返回响应体。"""
        return self.body

    def __enter__(self) -> "_Resp":
        """进入上下文。"""
        return self

    def __exit__(self, *exc) -> bool:
        """退出上下文，不吞异常。"""
        return False


def test_调试口_IPv4不通会问IPv6(monkeypatch):
    """Chrome 在 127.0.0.1 被占时会退到 [::1]；只探 IPv4 会误报「没开」。"""
    seen: list[str] = []

    class Opener:
        """假造的 opener：记录探过哪些 URL，只有 IPv6 那条给回包。"""

        def open(self, url, timeout=None):
            """按 URL 决定回包或拒绝。"""
            seen.append(url)
            if "[::1]" in url:
                return _Resp(
                    b'{"webSocketDebuggerUrl": "ws://[::1]:9222/devtools/browser/x"}'
                )
            raise OSError("connection refused")

    monkeypatch.setattr(cdp, "_LOOPBACK_OPENER", Opener())
    assert cdp.probe_debug(9222) == "ws://[::1]:9222/devtools/browser/x"
    assert len(seen) == 2 and seen[0].startswith("http://127.0.0.1:9222")


def test_调试口_两个协议都占着就换端口(launched, monkeypatch):
    """IPv4/IPv6 都被占的端口直接跳过，别浪费 30 秒等一个不可能开的调试口。"""
    launched_ports: list[int] = []
    ws = "ws://127.0.0.1:9223/devtools/browser/x"
    probes_9223 = iter(["", ws])

    monkeypatch.setattr(cdp, "_port_usable", lambda p: p != 9222)
    monkeypatch.setattr(
        cdp,
        "probe_debug",
        lambda p=None, **k: next(probes_9223, "") if p == 9223 else "",
    )
    monkeypatch.setattr(
        cdp,
        "launch_chrome",
        lambda **kw: launched_ports.append(kw["port"]) or FakeProc(),
    )

    assert isinstance(cdp.connect_or_launch(), FakeCdp)
    assert launched_ports == [9223]


def test_调试口_换端口时换一个profile(launched, monkeypatch):
    """同一个 user-data-dir 的第二台 Chrome 会把启动交给第一台然后自己退出——
    挪端口不换 profile 等于没挪。"""
    profiles: list[Path] = []
    procs: list[FakeProc] = []
    probes_9223 = iter(["", "ws://x"])

    monkeypatch.setattr(cdp, "_port_usable", lambda p: True)
    monkeypatch.setattr(
        cdp,
        "probe_debug",
        lambda p=None, **k: next(probes_9223, "") if p == 9223 else "",
    )

    def fake_launch(**kw):
        """假拉起：记下用过的 profile，并返回一台还活着的假进程。"""
        profiles.append(Path(kw["profile_dir"]))
        proc = FakeProc()
        procs.append(proc)
        return proc

    monkeypatch.setattr(cdp, "launch_chrome", fake_launch)

    assert cdp.connect_or_launch(timeout=0.01) is not None
    assert [p.name for p in profiles] == [
        profiles[0].name,
        f"{profiles[0].name}-9223",
    ]
    assert procs[0].killed is True


def test_调试口_启动进程提前退出就换下一轮(launched, monkeypatch):
    """启动进程秒退 = Chrome 把请求交给了已有实例，这个端口等也等不到。"""
    profiles: list[Path] = []
    procs = iter([FakeProc(alive=False), FakeProc()])
    probes_9223 = iter(["", "ws://x"])

    monkeypatch.setattr(cdp, "_port_usable", lambda p: True)
    monkeypatch.setattr(
        cdp,
        "probe_debug",
        lambda p=None, **k: next(probes_9223, "") if p == 9223 else "",
    )

    def fake_launch(**kw):
        """假拉起：记下 profile，进程从预置序列里取下一台。"""
        profiles.append(Path(kw["profile_dir"]))
        return next(procs)

    monkeypatch.setattr(cdp, "launch_chrome", fake_launch)
    assert cdp.connect_or_launch() is not None
    assert len(profiles) == 2
    assert profiles[1].name.endswith("-9223")


def test_调试口_下一轮能复用换端口拉起来的那台(launched, monkeypatch):
    """第一次回退到 9223 后 Chrome 会留着；下次调用要先探到它，别再拉一台。"""
    monkeypatch.setattr(cdp, "_port_usable", lambda p: p != 9222)
    monkeypatch.setattr(
        cdp, "probe_debug", lambda p=None, **k: "ws://x" if p == 9223 else ""
    )
    monkeypatch.setattr(cdp, "launch_chrome", lambda **kw: pytest.fail("不该拉新 Chrome"))

    assert isinstance(cdp.connect_or_launch(), FakeCdp)


def test_Chrome日志_只挑和调试口有关的行(tmp_path: Path):
    """验 `_chrome_log_hint` 只留调试口相关的行，无关噪声丢掉。"""
    (tmp_path / cdp._CHROME_LOG_NAME).write_text(
        "[1:2:ERROR:tcp_socket_win.cc] bind() returned an error: 地址已被占用\n"
        "DevTools listening on ws://[::1]:9222/devtools/browser/x\n"
        "[1:2:ERROR:gcm] unrelated noise\n",
        encoding="utf-8",
    )
    hint = cdp._chrome_log_hint(tmp_path)
    assert "bind()" in hint
    assert "[::1]" in hint
    assert "noise" not in hint


def test_Chrome日志_没有就不报错(tmp_path: Path):
    """验没有 Chrome 日志时 `_chrome_log_hint` 返回空串，不抛错。"""
    assert cdp._chrome_log_hint(tmp_path) == ""


def test_profile有没有起来过(tmp_path: Path):
    """Chrome 正常启动会写 Local State / Default；空目录 = 压根没跑起来。"""
    assert cdp._profile_populated(tmp_path) is False
    (tmp_path / "Local State").write_text("{}", encoding="utf-8")
    assert cdp._profile_populated(tmp_path) is True


def test_调试口_启动即退且profile为空要说明原因(launched, monkeypatch, tmp_path: Path):
    """Popen 成功不代表 Chrome 真的跑了：空 profile + 秒退要说「被拦了/装得不对」。"""
    monkeypatch.setattr(cdp, "_port_usable", lambda p: True)
    monkeypatch.setattr(cdp, "probe_debug", lambda *a, **k: "")
    monkeypatch.setattr(cdp, "launch_chrome", lambda **kw: FakeProc(alive=False))

    with pytest.raises(StokenError) as caught:
        cdp.connect_or_launch(profile_dir=tmp_path / "prof")
    assert "profile 目录还是空的" in str(caught.value)


class FakeProc:
    """够用的 Popen 替身：只看 poll / wait / kill。"""

    def __init__(self, *, alive: bool = True, exits: bool = True, pid: int = 4242):
        """初始化。"""
        self.pid = pid
        self.alive = alive
        self.exits = exits
        self.killed = False
        self.returncode = 0
        self.waits: list[float | None] = []

    def poll(self):
        """轮询退出码。"""
        return None if self.alive else 0

    def wait(self, timeout=None):
        """等待退出。"""
        self.waits.append(timeout)
        if self.exits:
            self.alive = False
            return 0
        raise subprocess.TimeoutExpired("chrome", timeout)

    def kill(self):
        """杀掉进程。"""
        self.killed = True
        self.alive = False


class FakeCdp:
    """假装是 CdpClient：只记「有没有发 Browser.close」。"""

    instances: list["FakeCdp"] = []
    fail_close = False

    def __init__(self, ws_url, **kwargs):
        """初始化。"""
        self.ws_url = ws_url
        self.kwargs = kwargs
        self.calls: list[str] = []
        self.closed = False
        FakeCdp.instances.append(self)

    def call(self, method, params=None, **kwargs):
        """发命令并返回结果。"""
        self.calls.append(method)
        if method == "Browser.close" and FakeCdp.fail_close:
            raise StokenError("连不上")
        return {}

    def close(self):
        """关闭。"""
        self.closed = True


@pytest.fixture
def launched(monkeypatch):
    """清空册子和钩子安装状态；顺手把真 atexit/signal/控制台钩子挡掉，别污染 pytest。"""
    monkeypatch.setattr(cdp.atexit, "register", lambda *a, **k: None)
    monkeypatch.setattr(cdp.signal, "signal", lambda *a, **k: None)
    monkeypatch.setattr(cdp, "_install_console_handler", lambda: None)
    monkeypatch.setattr(cdp, "CdpClient", FakeCdp)
    FakeCdp.instances.clear()
    FakeCdp.fail_close = False
    with cdp._launched_lock:
        cdp._launched.clear()
        cdp._exit_hooks_installed = False
    yield cdp._launched
    with cdp._launched_lock:
        cdp._launched.clear()
        cdp._exit_hooks_installed = False


def test_退出_登记自拉的浏览器(launched, monkeypatch):
    """验证退出就登记自拉的浏览器。"""
    monkeypatch.setattr(cdp, "_hide_windows", lambda pid, **k: 1)
    monkeypatch.setattr(cdp.subprocess, "Popen", lambda *a, **k: FakeProc())
    cdp.launch_chrome(chrome="C:/chrome.exe", mode="hidden")
    assert len(launched) == 1
    assert launched[0].port == cdp.DEFAULT_CDP_PORT


def test_退出_先走CDP关再等它退(launched):
    """验证退出就先走 CDP 关再等它退。"""
    proc = FakeProc()
    launched.append(cdp._Launched(proc=proc, port=9222, ws_url="ws://x/devtools/browser/abc"))

    assert cdp.close_launched_browsers() == 1
    assert FakeCdp.instances[0].calls == ["Browser.close"]
    assert FakeCdp.instances[0].closed is True
    assert proc.killed is False
    assert launched == []


def test_退出_CDP关不掉就杀进程(launched):
    """验证退出 CDP 关不掉就杀进程。"""
    FakeCdp.fail_close = True
    proc = FakeProc(exits=False)
    launched.append(cdp._Launched(proc=proc, port=9222, ws_url="ws://x/devtools/browser/abc"))

    assert cdp.close_launched_browsers(timeout=0.01) == 1
    assert proc.killed is True


def test_退出_没连上过就直接杀(launched):
    """launch_chrome 拉起来了但谁都没连上（ws 地址空着），别耗那几秒。"""
    proc = FakeProc()
    launched.append(cdp._Launched(proc=proc, port=9222))
    assert cdp.close_launched_browsers() == 1
    assert proc.killed is True
    assert proc.waits == []


def test_退出_早就退了的不算(launched):
    """验证退出就早就退了的不算。"""
    launched.append(cdp._Launched(proc=FakeProc(alive=False), port=9222, ws_url="ws://x"))
    assert cdp.close_launched_browsers() == 0
    assert FakeCdp.instances == []


def test_退出_册子空着就什么都不做(launched):
    """验证退出就册子空着就什么都不做。"""
    assert cdp.close_launched_browsers() == 0


def test_退出_复用那台不进册子(monkeypatch, launched):
    """端口上已经有 Chrome 时是复用，退出一律不碰它。"""
    monkeypatch.setattr(cdp, "probe_debug", lambda *a, **k: "ws://127.0.0.1:9222/devtools/browser/x")
    monkeypatch.setattr(
        cdp, "launch_chrome", lambda **k: pytest.fail("复用时不该拉新 Chrome")
    )
    assert cdp.connect_or_launch() is not None
    assert launched == []
    assert cdp.close_launched_browsers() == 0


def test_退出_连上才把ws地址记下来(monkeypatch, launched):
    """自拉那一支：调试口就绪后才把 ws 地址认到这台上。"""
    monkeypatch.setattr(cdp, "_hide_windows", lambda pid, **k: 1)
    monkeypatch.setattr(cdp.subprocess, "Popen", lambda *a, **k: FakeProc())
    probe = iter(["", "ws://127.0.0.1:9222/devtools/browser/x"])
    monkeypatch.setattr(cdp, "probe_debug", lambda *a, **k: next(probe, ""))
    monkeypatch.setattr(cdp, "_port_usable", lambda p: True)

    cdp.connect_or_launch()
    assert len(launched) == 1
    assert launched[0].ws_url == "ws://127.0.0.1:9222/devtools/browser/x"


def test_退出_钩子默认挂上(monkeypatch, launched):
    """验证退出就钩子默认挂上。"""
    hooked: list = []
    monkeypatch.setattr(cdp.atexit, "register", lambda fn: hooked.append(fn))
    monkeypatch.delenv("BOSS_CDP_CLOSE_ON_EXIT", raising=False)
    cdp._install_exit_hooks()
    assert hooked == [cdp.close_launched_browsers]


def test_退出_可以关掉这个行为(monkeypatch, launched):
    """验证退出就可以关掉这个行为。"""
    hooked: list = []
    monkeypatch.setattr(cdp.atexit, "register", lambda fn: hooked.append(fn))
    monkeypatch.setenv("BOSS_CDP_CLOSE_ON_EXIT", "0")
    cdp._install_exit_hooks()
    assert hooked == []


def test_退出_钩子只挂一次(monkeypatch, launched):
    """验证退出就钩子只挂一次。"""
    hooked: list = []
    monkeypatch.setattr(cdp.atexit, "register", lambda fn: hooked.append(fn))
    cdp._install_exit_hooks()
    cdp._install_exit_hooks()
    assert len(hooked) == 1


def test_退出_关终端窗口也要收尾(launched):
    """CTRL_CLOSE_EVENT 走不到 atexit（进程是被控制台直接干掉的），得在这儿收。"""
    proc = FakeProc()
    launched.append(cdp._Launched(proc=proc, port=9222, ws_url="ws://x/devtools/browser/abc"))

    assert cdp._console_ctrl_handler(cdp._CTRL_CLOSE_EVENT) is False
    assert proc.killed is False and proc.waits
    assert launched == []


def test_退出_CtrlC不抢答(launched):
    """CTRL_C 交给 Python 自己（抛 KeyboardInterrupt → atexit），别在这儿收。"""
    proc = FakeProc()
    launched.append(cdp._Launched(proc=proc, port=9222, ws_url="ws://x/devtools/browser/abc"))

    assert cdp._console_ctrl_handler(cdp._CTRL_C_EVENT) is False
    assert launched != []


def test_退出_Break也是硬退所以收掉(launched):
    """CTRL_BREAK 实测必死（Python 层收尾不跑），所以在这儿收。"""
    proc = FakeProc()
    launched.append(cdp._Launched(proc=proc, port=9222, ws_url="ws://x/devtools/browser/abc"))

    assert cdp._console_ctrl_handler(cdp._CTRL_BREAK_EVENT) is False
    assert launched == []


def test_退出_致命事件清单不含CtrlC():
    """验证退出就致命事件清单不含 CtrlC。"""
    assert cdp._CTRL_C_EVENT not in cdp._FATAL_CONSOLE_EVENTS
    assert set(cdp._FATAL_CONSOLE_EVENTS) == {
        cdp._CTRL_BREAK_EVENT,
        cdp._CTRL_CLOSE_EVENT,
        cdp._CTRL_LOGOFF_EVENT,
        cdp._CTRL_SHUTDOWN_EVENT,
    }


def test_退出_关掉开关就不挂控制台钩子(monkeypatch, launched):
    """验证退出就关掉开关就不挂控制台钩子。"""
    installed: list = []
    monkeypatch.setattr(cdp, "_install_console_handler", lambda: installed.append(1))
    monkeypatch.setenv("BOSS_CDP_CLOSE_ON_EXIT", "0")
    cdp._install_exit_hooks()
    assert installed == []


def test_落盘记录带过期时间(tmp_path: Path):
    """`doc('stoken')` 必须有 expires_at，否则下次 fetch 判不了过期。"""
    p = provider(tmp_path)
    p.ensure()
    payload = boss_db.doc_get(boss_db.DOC_STOKEN, tmp_path / "boss.db")
    assert payload is not None
    assert payload["token"] == "0138NEW"
    assert payload["expires_at"] > time.time()
    assert payload["source"] == "cdp"
    assert payload["expires_at"] - payload["minted_at"] == pytest.approx(STOKEN_MAX_AGE, abs=2)
