"""版本更新检测：对着 Gitee 发行版比一版号，有新的就提示用户去下。

只查 Gitee；GitHub 只提供下载入口，不参与版本判断。忽略版本记的是「水位线」
——比水位新的版本会重新弹出提示。检测触发时机：启动时后台预热一次、
用户在网页里手动刷新。

Attributes:
    GITEE_RELEASES_PAGE: 提示框上「去下载」按钮的落点。
    HTTP_TIMEOUT: 拉发行版列表的超时（秒）。
    CACHE_TTL: 同一次运行内结果的有效期（秒）。
    NOTES_LIMIT: 发行说明带进弹窗的最大字符数。
    _cache: 最近一次检测结果（含失败），`checked_at` 为 0 表示还没测过。
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
GITEE_RELEASES_PAGE = f"https://gitee.com/{GITEE_OWNER}/{GITEE_REPO}/releases"
GITHUB_RELEASES_PAGE = (
    f"https://github.com/{GITHUB_OWNER}/{GITEE_REPO}/releases"
)

HTTP_TIMEOUT = 8.0
CACHE_TTL = 1800.0
NOTES_LIMIT = 1200

_VERSION_RE = re.compile(r"^\D*(\d+(?:\.\d+)*).*$")


def parse_version(raw: Any) -> tuple[int, ...] | None:
    """`v1.2.3` / `1.2` / `V1.2.3-rc1` → `(1, 2, 3)`；认不出回 `None`。

    允许缺段（`1.2` 当 `1.2.0`）、允许 `v` 前缀、忽略预发布后缀。
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
    """比两串版本号：a>b → 1，a<b → -1，相等（或都认不出）→ 0。

    短的补零：`1.2` 与 `1.2.0` 算相等。
    """
    pa = parse_version(a) or ()
    pb = parse_version(b) or ()
    n = max(len(pa), len(pb))
    pa = pa + (0,) * (n - len(pa))
    pb = pb + (0,) * (n - len(pb))
    return (pa > pb) - (pa < pb)


def _normalize(raw: Any) -> str:
    """展示用的版本号统一成「不带 v」的原始形。"""
    text = str(raw or "").strip()
    return text[1:] if text[:1] in ("v", "V") else text


@dataclass(frozen=True)
class Asset:
    """发行版上的一个下载附件。

    Attributes:
        name: Gitee 上的附件名（`BossAutoDelivery-1.1.3.zip`），用来挑更新包。
        url: 下载直链。
    """

    name: str
    url: str


@dataclass(frozen=True)
class Release:
    """一条发行版信息。

    Attributes:
        version: 展示用版本号（去掉 `v` 前缀）。
        notes: 发行说明开头一段（Markdown 原文）。
        assets: Gitee 上的资产（名字 + 直链，可能为空）。
    """

    version: str
    notes: str
    assets: tuple[Asset, ...]


def pick_latest(releases: list[dict[str, Any]]) -> Release | None:
    """从 Gitee 列表里挑最高的正式版；没有可用条目回 `None`。

    跳过 `prerelease` / 认不出版本号的条目，不看列表顺序。

    Args:
        releases: Gitee 发行版列表。
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
            Asset(
                name=str(a.get("name") or ""),
                url=str(a.get("browser_download_url") or ""),
            )
            for a in (item.get("assets") or [])
            if isinstance(a, dict) and a.get("browser_download_url")
        )
        body = str(item.get("body") or "").strip()
        if len(body) > NOTES_LIMIT:
            body = body[:NOTES_LIMIT].rstrip() + "\n…"
        best = Release(version=_normalize(tag), notes=body, assets=assets)
    return best


def pick_zip_asset(assets: Any) -> Asset | None:
    """挑出更新用的 zip 附件；没有则回 `None`。

    优先 `BossAutoDelivery*.zip`，其次任意 `*.zip`。名字为空就看 URL 最后一段。

    Args:
        assets: `Asset` 或带 `name`/`url` 的字典序列。
    """
    items: list[Asset] = []
    for a in assets or []:
        if isinstance(a, Asset):
            items.append(a)
        elif isinstance(a, dict):
            items.append(Asset(name=str(a.get("name") or ""), url=str(a.get("url") or "")))

    def _name_of(a: Asset) -> str:
        """附件名；名字为空时取 URL 最后一段。"""
        if a.name:
            return a.name
        tail = (a.url or "").rstrip("/").rsplit("/", 1)[-1]
        return tail.split("?")[0]

    zips = [a for a in items if _name_of(a).lower().endswith(".zip")]
    if not zips:
        return None
    preferred = [a for a in zips if _name_of(a).lower().startswith("bossautodelivery")]
    return preferred[0] if preferred else zips[0]


def can_self_update() -> bool:
    """只有装在本地目录里的 exe 能自更新；源码跑或临时目录预览跑都回 `False`。"""
    from .. import runtime

    return bool(runtime.is_frozen()) and not runtime.is_temp_run()


def fetch_latest_release() -> Release:
    """去 Gitee 拉发行版列表并挑最新正式版；失败抛 `UpdateCheckError`。"""
    try:
        resp = requests.get(
            GITEE_RELEASES_API,
            timeout=HTTP_TIMEOUT,
            headers={"User-Agent": f"BossAutoDelivery/{APP_VERSION}"},
        )
        if resp.status_code in (403, 429):
            raise UpdateCheckError(
                "Gitee 接口被限流了，等一会儿再试；不影响程序其它功能。"
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
    """检测失败。不上抛到界面，只进 `error` 字段与日志。"""


_lock = threading.Lock()
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
    """缓存是否还在 TTL 内。"""
    checked_at = float(_cache.get("checked_at") or 0.0)
    if checked_at <= 0:
        return False
    ttl = CACHE_TTL if seconds is None else seconds
    return (time.time() - checked_at) < ttl


def refresh(*, force: bool = False) -> dict[str, Any]:
    """拿一份检测结果；缓存没过期就直接回，过期才真去打 Gitee。

    失败也缓存，避免用户每开一页都卡住等网络。触发检测：启动时后台预热、网页手动刷新。

    Args:
        force: 为真则忽略缓存强制重测。
    """
    if not force and _fresh():
        return _snapshot()

    with _lock:
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
    """缓存内容的副本。"""
    release: Release | None = _cache.get("release")
    return {
        "checked_at": float(_cache.get("checked_at") or 0.0),
        "release": release,
        "error": _cache.get("error"),
    }


def schedule_startup_check(on_done: Any = None) -> None:
    """启动时后台预热一次检测，开页面时就不用等网络。

    Args:
        on_done: 可选回调 `on_done(status)`，CLI 用它打印版本信息。
    """

    def _run() -> None:
        """后台线程体：预热一次并可选回调。"""
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


def cached_snapshot(*, path: Any = None) -> dict[str, Any]:
    """读缓存里的检测结论，绝不打网络。

    诊断页（系统设置 → 版本更新）用它；要立刻测请走
    `GET /api/system/update?force=true`。

    Args:
        path: 状态库路径，用来读忽略版本水位线。
    """
    snap = _snapshot()
    release: Release | None = snap["release"]
    ignored = load_ignored_version(path)

    current = _normalize(APP_VERSION)
    latest = release.version if release else None
    has_update = bool(latest) and compare_versions(latest, current) > 0
    suppressed = bool(ignored) and latest is not None and compare_versions(latest, ignored) <= 0

    assets = list(release.assets) if release else []
    zip_asset = pick_zip_asset(assets)

    return {
        "ok": True,
        "current": current,
        "latest": latest,
        "has_update": has_update,
        "should_prompt": has_update and not suppressed,
        "ignored": ignored or None,
        "notes": release.notes if release else "",
        "assets": [{"name": a.name, "url": a.url} for a in assets],
        "zip_asset": {"name": zip_asset.name, "url": zip_asset.url} if zip_asset else None,
        "can_self_update": can_self_update(),
        "checked_at": snap["checked_at"],
        "error": snap["error"],
    }


def load_ignored_version(path: Any = None) -> str:
    """用户点过「忽略此版本」的水位线；没点过回空串。

    存的是单个版本号：每次忽略把水位抬到那一版，比水位旧的忽略自动作废。

    Args:
        path: 状态库路径。
    """
    from .app_settings_store import load_settings

    try:
        return _normalize(load_settings(path).ignored_update_version)
    except Exception:  # noqa: BLE001 - 设置读不出来就当没忽略过
        logger.warning("读忽略版本失败，按没忽略处理", exc_info=True)
        return ""


def save_ignored_version(version: Any, path: Any = None) -> str:
    """记下要忽略的版本号；空串 = 撤销忽略。

    Args:
        version: 要忽略到的版本号。
        path: 状态库路径。
    """
    from .app_settings_store import apply_ignored_update_version

    return apply_ignored_update_version(_normalize(version), path)


def build_status(*, path: Any = None, force: bool = False) -> dict[str, Any]:
    """前端 `GET /api/system/update` 的完整载荷。

    `should_prompt` 是弹窗的唯一开关：有更新且没被忽略掉才为真。
    `can_self_update` 决定「立即更新」按钮露不露；`update_done` 是自更新
    跑完后新进程给重连页面的一次性回执。

    Args:
        path: 状态库路径，用来读忽略版本水位线。
        force: 为真则忽略缓存强制重测。
    """
    snap = refresh(force=force)
    release: Release | None = snap["release"]
    ignored = load_ignored_version(path)

    current = _normalize(APP_VERSION)
    latest = release.version if release else None
    has_update = bool(latest) and compare_versions(latest, current) > 0
    suppressed = bool(ignored) and latest is not None and compare_versions(latest, ignored) <= 0

    assets = list(release.assets) if release else []
    zip_asset = pick_zip_asset(assets)
    try:
        update_done = _load_done_marker()
    except Exception:  # noqa: BLE001 - 回执读不出来不该把状态接口搞挂
        update_done = None

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
        "assets": [{"name": a.name, "url": a.url} for a in assets],
        "zip_asset": {"name": zip_asset.name, "url": zip_asset.url} if zip_asset else None,
        "can_self_update": can_self_update(),
        "update_done": update_done,
        "checked_at": snap["checked_at"],
        "error": snap["error"],
    }


def _load_done_marker() -> dict[str, Any] | None:
    """更新完成回执（一次性）；import 放函数里避免 services 互相 import 成环。"""
    from .updater import consume_done_marker

    return consume_done_marker()


def format_console_lines(status: dict[str, Any]) -> list[str]:
    """控制台按行打印：当前版本 / 发行版本 / 是否需要更新。

    跟横幅左对齐（`键 13 列 + 值`）。检测失败也要把当前版本打全；有更新时
    追加两行下载地址。

    Args:
        status: `build_status` / `cached_snapshot` 的结果。

    Returns:
        可直接 `print` 的行列表。
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
