"""版本更新检测：对着 Gitee 发行版比一版号，有新的就提示用户去下。

**只查 Gitee**——GitHub 那边只提供下载入口，不参与版本判断（两边发版节奏
可能不一致，以 Gitee 为准，见模块常量）。

为什么要自己存「忽略的版本」：用户点了「忽略此版本」后，只要最新版还停在被
忽略的那一版就别再弹；等发了**更**新的版本，提示要重新出现。只点「取消」则
什么都不记，下次打开页面照样弹。

Gitee 的 ``/releases/latest`` 不可信——按 ``created_at`` 挑，tag 顺序一乱就
返回旧版（实测 v1.1.0 已发布时它还回 v1.0.0）。所以走列表接口自己比大小。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

import requests

from .. import __version__ as APP_VERSION

logger = logging.getLogger(__name__)

GITHUB_OWNER = "handsomekai924"
GITEE_OWNER = "w-kev"
GITEE_REPO = "boss-automatic-delivery"
GITEE_RELEASES_API = (
    f"https://gitee.com/api/v5/repos/{GITEE_OWNER}/{GITEE_REPO}/releases?per_page=20"
)
#: 提示框上「去下载」按钮的落点
GITEE_RELEASES_PAGE = f"https://gitee.com/{GITEE_OWNER}/{GITEE_REPO}/releases"
GITHUB_RELEASES_PAGE = (
    f"https://github.com/{GITHUB_OWNER}/{GITEE_REPO}/releases"
)

#: 拉发行版列表的超时（秒）。用户开页面时同步调用，不能拖太久
HTTP_TIMEOUT = 8.0
#: 同一次运行内结果的有效期（秒）。开页面不会每次都撞 Gitee
CACHE_TTL = 1800.0
#: 发行说明只带开头一段进弹窗——Gitee 的 body 是整篇 Markdown，摆不下
NOTES_LIMIT = 1200

_VERSION_RE = re.compile(r"^\D*(\d+(?:\.\d+)*).*$")


# --------------------------------------------------------------------------- #
# 版本号比较
# --------------------------------------------------------------------------- #


def parse_version(raw: Any) -> tuple[int, ...] | None:
    """``v1.2.3`` / ``1.2`` / ``V1.2.3-rc1`` → ``(1, 2, 3)``；认不出回 ``None``。

    允许缺段（``1.2`` 当 ``1.2.0``）、允许 ``v`` 前缀、忽略预发布后缀——预发布
    在列表里另有 ``prerelease`` 标记，这里只管排序。
    """
    text = str(raw or "").strip()
    if not text:
        return None
    m = _VERSION_RE.match(text)
    if not m:
        return None
    parts = m.group(1).split(".")
    try:
        nums = tuple(int(p) for p in parts)
    except ValueError:  # pragma: no cover - 正则已保证是数字
        return None
    return nums or None


def compare_versions(a: Any, b: Any) -> int:
    """比两串版本号：a>b → 1，a<b → -1，相等（或都认不出）→ 0。"""
    pa = parse_version(a) or ()
    pb = parse_version(b) or ()
    # 短的补零：``1.2`` 与 ``1.2.0`` 算相等
    n = max(len(pa), len(pb))
    pa = pa + (0,) * (n - len(pa))
    pb = pb + (0,) * (n - len(pb))
    return (pa > pb) - (pa < pb)


def _normalize(raw: Any) -> str:
    """展示用的版本号统一成「不带 v」的原始形，存忽略版本时也好比。"""
    text = str(raw or "").strip()
    return text[1:] if text[:1] in ("v", "V") else text


# --------------------------------------------------------------------------- #
# 发行版数据
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Release:
    #: 展示用版本号（去掉 ``v`` 前缀）
    version: str
    #: 发行说明开头一段（Markdown 原文，前端按纯文本摆）
    notes: str
    #: Gitee 上的资产下载直链（可能为空列表）
    assets: tuple[str, ...]


def pick_latest(releases: list[dict[str, Any]]) -> Release | None:
    """从 Gitee 列表里挑**最高的正式版**。

    跳过 ``prerelease`` / 认不出版本号的条目；不看列表顺序——Gitee 返回顺序
    跟新旧无关。
    """
    best: Release | None = None
    for item in releases or []:
        if not isinstance(item, dict):
            continue
        if item.get("prerelease") or item.get("draft"):
            continue
        tag = item.get("tag_name") or item.get("name") or ""
        parsed = parse_version(tag)
        if parsed is None:
            continue
        if best is not None and compare_versions(tag, best.version) <= 0:
            continue
        assets = tuple(
            str(a.get("browser_download_url") or "")
            for a in (item.get("assets") or [])
            if isinstance(a, dict) and a.get("browser_download_url")
        )
        body = str(item.get("body") or "").strip()
        if len(body) > NOTES_LIMIT:
            body = body[:NOTES_LIMIT].rstrip() + "\n…"
        best = Release(version=_normalize(tag), notes=body, assets=assets)
    return best


def fetch_latest_release() -> Release:
    """去 Gitee 拉发行版列表并挑最新正式版。失败抛 :class:`UpdateCheckError`。"""
    try:
        resp = requests.get(
            GITEE_RELEASES_API,
            timeout=HTTP_TIMEOUT,
            headers={"User-Agent": f"BossAutoDelivery/{APP_VERSION}"},
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        raise UpdateCheckError("连不上 Gitee，稍后再试；不影响程序其它功能。") from exc
    except ValueError as exc:
        raise UpdateCheckError("Gitee 回的数据看不懂，稍后再试。") from exc

    if not isinstance(data, list):
        raise UpdateCheckError("Gitee 回的数据看不懂，稍后再试。")
    latest = pick_latest(data)
    if latest is None:
        raise UpdateCheckError("Gitee 上还没发过正式版本。")
    return latest


class UpdateCheckError(Exception):
    """检测失败。**不上抛到界面**，只进 ``error`` 字段与日志。"""


# --------------------------------------------------------------------------- #
# 缓存 + 忽略版本
# --------------------------------------------------------------------------- #

_lock = threading.Lock()
#: 最近一次检测结果（含失败），``checked_at`` 为 0 表示还没测过
_cache: dict[str, Any] = {
    "checked_at": 0.0,
    "release": None,
    "error": None,
}


def reset_cache() -> None:
    """清掉缓存。给测试用。"""
    with _lock:
        _cache.update(checked_at=0.0, release=None, error=None)


def _fresh(seconds: float | None = None) -> bool:
    checked_at = float(_cache.get("checked_at") or 0.0)
    if checked_at <= 0:
        return False
    ttl = CACHE_TTL if seconds is None else seconds
    return (time.time() - checked_at) < ttl


def refresh(*, force: bool = False) -> dict[str, Any]:
    """拿一份检测结果；缓存没过期就直接回，过期才真去打 Gitee。

    **失败也缓存**——Gitee 一次连不上时，别让用户每开一页都卡 8 秒。
    """
    if not force and _fresh():
        return _snapshot()

    with _lock:
        # 双检：等锁期间可能已经被别的线程测完了
        if not force and _fresh():
            return _snapshot()
        try:
            release = fetch_latest_release()
        except UpdateCheckError as exc:
            logger.warning("版本检测失败：%s", exc)
            _cache.update(checked_at=time.time(), release=None, error=str(exc))
        else:
            _cache.update(checked_at=time.time(), release=release, error=None)
        return _snapshot()


def _snapshot() -> dict[str, Any]:
    release: Release | None = _cache.get("release")
    return {
        "checked_at": float(_cache.get("checked_at") or 0.0),
        "release": release,
        "error": _cache.get("error"),
    }


def schedule_startup_check(on_done: Any = None) -> None:
    """启动时**后台**预热一次检测，开页面时就不用等网络。

    ``on_done(status)`` 是可选回调——CLI 拿它把「当前版本 / 发行版本 / 要不要
    更新」打到控制台。守护线程 + 完全吞异常：版本检测永远不该拦住服务起来。
    """

    def _run() -> None:
        try:
            status = build_status(force=True)
        except Exception:  # noqa: BLE001 - 预热失败静默，refresh 自己会记日志
            logger.debug("启动时的版本预热失败", exc_info=True)
            return
        if on_done is None:
            return
        try:
            on_done(status)
        except Exception:  # noqa: BLE001 - 打印失败更不该影响服务
            logger.debug("版本结果回调失败", exc_info=True)

    threading.Thread(target=_run, name="update-check", daemon=True).start()


# --------------------------------------------------------------------------- #
# 组装给前端的状态
# --------------------------------------------------------------------------- #


def load_ignored_version(path: Any = None) -> str:
    """用户点过「忽略此版本」的**水位线**；没点过回空串。

    存的是单个版本号而不是一串列表：每次忽略就是把水位抬到那一版，比水位
    旧的忽略自动作废。连续忽略 ``v1.2.0`` 再忽略 ``v1.3.0`` ⇒ 水位 ``1.3.0``，
    ``≤ 1.3.0`` 一律静音，``v1.4.0`` 才再弹。等价于「记一串忽略列表」，但：
    不会长大、也不用担心中间版本回头骚扰用户。
    """
    from .app_settings_store import load_settings

    try:
        return _normalize(load_settings(path).ignored_update_version)
    except Exception:  # noqa: BLE001 - 设置读不出来就当没忽略过
        logger.warning("读忽略版本失败，按没忽略处理", exc_info=True)
        return ""


def save_ignored_version(version: Any, path: Any = None) -> str:
    """记下要忽略的版本号。空串 = 撤销忽略。"""
    from .app_settings_store import apply_ignored_update_version

    return apply_ignored_update_version(_normalize(version), path)


def build_status(*, path: Any = None, force: bool = False) -> dict[str, Any]:
    """前端 ``GET /api/system/update`` 的完整载荷。

    ``should_prompt`` 是弹窗的唯一开关：有更新**且**没被忽略掉才为真。
    """
    snap = refresh(force=force)
    release: Release | None = snap["release"]
    ignored = load_ignored_version(path)

    current = _normalize(APP_VERSION)
    latest = release.version if release else None
    has_update = bool(latest) and compare_versions(latest, current) > 0
    # 忽略是**水位线**：最新版不比水位新就不弹，比水位新就照弹。
    # 连续忽略多个版本只留下最高那一版（见 load_ignored_version）。
    suppressed = bool(ignored) and latest is not None and compare_versions(latest, ignored) <= 0

    return {
        "ok": True,
        "current": current,
        "latest": latest,
        "has_update": has_update,
        "should_prompt": has_update and not suppressed,
        "ignored": ignored or None,
        "notes": release.notes if release else "",
        "gitee_url": GITEE_RELEASES_PAGE,
        "github_url": GITHUB_RELEASES_PAGE,
        "assets": list(release.assets) if release else [],
        "checked_at": snap["checked_at"],
        "error": snap["error"],
    }


def format_console_lines(status: dict[str, Any]) -> list[str]:
    """控制台按行打印：当前版本 / 发行版本 / 是否需要更新。

    跟横幅左对齐（``键 13 列 + 值``）。检测失败也要把当前版本打全——用户报问题
    时第一句就是「我这是哪一版」。有更新时追加两行下载地址。
    """
    current = _normalize(status.get("current") or APP_VERSION)
    latest = status.get("latest")
    error = status.get("error")

    released = f"v{_normalize(latest)}" if latest else "——"

    if not latest and error:
        need = f"没测到（{error}）"
    elif not latest:
        need = "没测到（Gitee 上还没发过正式版本）"
    elif status.get("has_update"):
        if status.get("ignored") and not status.get("should_prompt"):
            need = f"有新版本 v{_normalize(latest)}，但已忽略；要再提醒请点开网页里的更新窗"
        else:
            need = f"有新版本 v{_normalize(latest)}，建议更新"
    else:
        need = "否，已是最新"

    lines = [
        f"  当前版本     v{current}",
        f"  发行版本     {released}",
        f"  是否需要更新 {need}",
    ]
    if status.get("has_update"):
        lines.append(f"  Gitee 下载   {status.get('gitee_url') or GITEE_RELEASES_PAGE}")
        lines.append(f"  GitHub 下载  {status.get('github_url') or GITHUB_RELEASES_PAGE}")
    return lines
