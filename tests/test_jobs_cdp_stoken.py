"""``__zp_stoken__`` 的 CDP 取法：过期账本 / fetch 时自动续期。

真 Chrome 那一段（拉浏览器 → 灌登录 Cookie → 开页 → 读 Cookie）在
:mod:`boss_jobs.cdp_stoken` 里，单测不碰真浏览器（见 ``conftest.py`` 的
``no_real_chrome``），只验「什么时候该去换新、换了怎么落盘」。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from boss_jobs.cdp_stoken import (
    CdpStokenProvider,
    StokenRecord,
    StokenStore,
    find_chrome,
)
from boss_jobs.stoken import STOKEN_MAX_AGE
from boss_jobs.stoken import StokenError

# --------------------------------------------------------------------------- #
# 假会话
# --------------------------------------------------------------------------- #


class FakeCookieJar:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._meta: dict[str, dict] = {}

    def set(self, name, value, **kwargs):
        self._data[name] = value
        self._meta[name] = kwargs

    def get(self, name, default=None):
        return self._data.get(name, default)

    def __iter__(self):
        for name, value in self._data.items():
            yield type("C", (), {"name": name, "value": value})()


class FakeHttp:
    def __init__(self, cookies: dict[str, str] | None = None) -> None:
        self.cookies = FakeCookieJar()
        for name, value in (cookies or {}).items():
            self.cookies.set(name, value)


# --------------------------------------------------------------------------- #
# StokenRecord：过期账本的那条记录
# --------------------------------------------------------------------------- #


def test_记录_站点给了expires就用它():
    now = time.time()
    rec = StokenRecord.from_cookie(
        {"name": "__zp_stoken__", "value": "0138X", "expires": now + 3600},
        minted_at=now,
    )
    assert rec.token == "0138X"
    assert rec.expires_at == pytest.approx(now + 3600)
    assert rec.source == "cdp"


def test_记录_会话Cookie没有expires按max_age推():
    """CDP 读到的会话 Cookie ``expires`` 是 -1，这时按前端的 3840 分钟推。"""
    now = time.time()
    rec = StokenRecord.from_cookie(
        {"name": "__zp_stoken__", "value": "0138X", "expires": -1}, minted_at=now
    )
    assert rec.expires_at == pytest.approx(now + STOKEN_MAX_AGE)


def test_记录_过期判断留了余量():
    now = time.time()
    fresh = StokenRecord(token="t", minted_at=now, expires_at=now + 600)
    assert fresh.is_fresh()
    # 离过期只剩 60 秒（余量 300）→ 该换了
    almost = StokenRecord(token="t", minted_at=now, expires_at=now + 60)
    assert not almost.is_fresh()
    # 到期了
    dead = StokenRecord(token="t", minted_at=now - 1000, expires_at=now - 1)
    assert not dead.is_fresh()


def test_记录_空token不算可用():
    rec = StokenRecord(token="", minted_at=time.time(), expires_at=time.time() + 100)
    assert not rec.is_usable
    assert not rec.is_fresh()


def test_记录_字典往返():
    rec = StokenRecord(token="0138A", minted_at=100.0, expires_at=200.0, source="manual")
    again = StokenRecord.from_dict(rec.to_dict())
    assert again == rec
    assert StokenRecord.from_dict(None) is None
    assert StokenRecord.from_dict({"token": ""}) is None


# --------------------------------------------------------------------------- #
# StokenStore：stoken.json
# --------------------------------------------------------------------------- #


def test_账本_读写往返(tmp_path: Path):
    store = StokenStore(tmp_path / "stoken.json")
    assert store.load() is None  # 还没有
    rec = StokenRecord(token="0138S", minted_at=time.time(), expires_at=time.time() + 100)
    store.save(rec)
    again = store.load()
    assert again is not None and again.token == "0138S"
    assert store.path.exists()


def test_账本_脏文件当没有(tmp_path: Path):
    path = tmp_path / "stoken.json"
    path.write_text("不是 JSON", encoding="utf-8")
    assert StokenStore(path).load() is None


def test_账本_能清掉(tmp_path: Path):
    store = StokenStore(tmp_path / "stoken.json")
    store.save(StokenRecord(token="t", minted_at=time.time(), expires_at=time.time() + 10))
    store.clear()
    assert store.load() is None
    store.clear()  # 幂等


# --------------------------------------------------------------------------- #
# CdpStokenProvider.ensure：什么时候去换新
# --------------------------------------------------------------------------- #


def provider(tmp_path: Path, http: FakeHttp | None = None, **kwargs) -> CdpStokenProvider:
    store = StokenStore(tmp_path / "stoken.json")
    http = http or FakeHttp()
    return CdpStokenProvider(http=http, store=store, acquire=lambda: "0138NEW", **kwargs)


def test_ensure_账本新鲜就直接用(tmp_path: Path):
    """没过期就不该拉 Chrome——这是 fetch 每次都会走的那一支。"""
    store = StokenStore(tmp_path / "stoken.json")
    store.save(StokenRecord(token="0138OLD", minted_at=time.time(), expires_at=time.time() + 9999))
    calls: list[str] = []

    p = CdpStokenProvider(
        http=FakeHttp(),
        store=store,
        acquire=lambda: calls.append("acquired") or "0138NEW",
    )
    assert p.ensure() == "0138OLD"
    assert calls == []                      # 一次都没拉浏览器
    assert p.http.cookies.get("__zp_stoken__") == "0138OLD"


def test_ensure_过期了才换新(tmp_path: Path):
    store = StokenStore(tmp_path / "stoken.json")
    store.save(StokenRecord(token="0138OLD", minted_at=time.time() - 9999, expires_at=time.time() - 1))
    calls: list[str] = []

    p = CdpStokenProvider(
        http=FakeHttp(),
        store=store,
        acquire=lambda: calls.append("acquired") or "0138NEW",
    )
    assert p.ensure() == "0138NEW"
    assert calls == ["acquired"]
    # 换完要落盘，下次 fetch 就不用再拉
    assert store.load().token == "0138NEW"
    assert p.http.cookies.get("__zp_stoken__") == "0138NEW"


def test_ensure_强制换新(tmp_path: Path):
    """撞上 code 37 时会 ``ensure(force=True)``，账本再新鲜也得换。"""
    store = StokenStore(tmp_path / "stoken.json")
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
    """手工从浏览器拷进 session.json 的那枚：没有过期时间戳，先用着，
    撞 37 再说（别抢着覆盖用户的活）。"""
    calls: list[str] = []
    p = CdpStokenProvider(
        http=FakeHttp({"__zp_stoken__": "0138MANUAL"}),
        store=StokenStore(tmp_path / "stoken.json"),
        acquire=lambda: calls.append("acquired") or "0138NEW",
    )
    assert p.ensure() == "0138MANUAL"
    assert calls == []


def test_ensure_什么都没有就去取(tmp_path: Path):
    calls: list[str] = []
    p = CdpStokenProvider(
        http=FakeHttp(),
        store=StokenStore(tmp_path / "stoken.json"),
        acquire=lambda: calls.append("acquired") or "0138NEW",
    )
    assert p.ensure() == "0138NEW"
    assert calls == ["acquired"]


def test_ensure_取回来是空的就报错(tmp_path: Path):
    p = CdpStokenProvider(
        http=FakeHttp(),
        store=StokenStore(tmp_path / "stoken.json"),
        acquire=lambda: "",
    )
    with pytest.raises(StokenError):
        p.ensure()


def test_ensure_会话对象不能写cookie就报错(tmp_path: Path):
    class NoCookieJar:
        cookies = None

    p = CdpStokenProvider(
        http=NoCookieJar(),
        store=StokenStore(tmp_path / "stoken.json"),
        acquire=lambda: "0138NEW",
    )
    with pytest.raises(StokenError):
        p.ensure()


def test_ensure_镜像进session_json(tmp_path: Path):
    """取到的 token 要写回 session.json，让 http_from_session 老规矩继续生效。"""
    from boss_login.session import StoredSession, load_session, save_session

    session_path = tmp_path / "s.json"
    save_session(
        StoredSession(
            token="tok",
            cookies={"wt2": "w"},
            phone_masked="138****0000",
            user={"userId": 1},
            saved_at=time.time(),
        ),
        session_path,
    )

    p = CdpStokenProvider(
        http=FakeHttp(),
        store=StokenStore(tmp_path / "stoken.json"),
        acquire=lambda: "0138MIRROR",
        session_path=session_path,
    )
    assert p.ensure() == "0138MIRROR"

    stored = load_session(session_path)
    assert stored.cookies["__zp_stoken__"] == "0138MIRROR"
    assert stored.cookies["wt2"] == "w"          # 原来的登录态没被冲掉


def test_不镜像时不碰session_json(tmp_path: Path):
    """session_path 不给就只落 stoken.json，别乱写用户文件。"""
    session_path = tmp_path / "s.json"
    p = CdpStokenProvider(
        http=FakeHttp(),
        store=StokenStore(tmp_path / "stoken.json"),
        acquire=lambda: "0138X",
    )
    p.ensure()
    assert not session_path.exists()


# --------------------------------------------------------------------------- #
# find_chrome
# --------------------------------------------------------------------------- #


def test_find_chrome_认环境变量(tmp_path: Path, monkeypatch):
    fake = tmp_path / "chrome.exe"
    fake.write_text("stub", encoding="utf-8")
    monkeypatch.setenv("BOSS_CHROME_BIN", str(fake))
    assert find_chrome() == str(fake)


def test_find_chrome_环境变量指空文件就报错(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BOSS_CHROME_BIN", str(tmp_path / "没这东西.exe"))
    with pytest.raises(StokenError):
        find_chrome()


# --------------------------------------------------------------------------- #
# 落盘文件的形状
# --------------------------------------------------------------------------- #


def test_落盘文件带过期时间(tmp_path: Path):
    """stoken.json 必须有 expires_at，否则下次 fetch 判不了过期。"""
    p = provider(tmp_path)
    p.ensure()
    payload = json.loads((tmp_path / "stoken.json").read_text(encoding="utf-8"))
    assert payload["token"] == "0138NEW"
    assert payload["expires_at"] > time.time()
    assert payload["source"] == "cdp"
    # 有效期是前端写的 3840 分钟
    assert payload["expires_at"] - payload["minted_at"] == pytest.approx(STOKEN_MAX_AGE, abs=2)
