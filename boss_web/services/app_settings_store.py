"""用户级应用设置：状态库 ``doc('app_settings')``。

目前两项：

- ``chrome_mode``——Chrome 窗口档位。为什么它：``boss_jobs`` 默认用
  ``hidden``（屏幕外 + Win32 隐藏）拉 Chrome，用户不必被弹一脸窗口——
  可一旦账号命中风控、**需要人工过滑块**，隐藏的窗口连任务栏里都没有，人是
  够不着的。打包给非技术用户后必须有个界面开关能把它切成 ``visible``。
- ``ignored_update_version``——用户点过「忽略此版本」的版本号，见
  :mod:`boss_web.services.update_check`。

做法照搬 :mod:`boss_web.services.deliver_config_store`：整包存 JSON、读时归一
化、坏值不报错只回默认。

**时效坑**：``boss_jobs.cdp_stoken._mode()`` 是**调用时**才读环境变量，所以这
里一改就得立刻同步到 ``os.environ``，否则要重启程序才生效——见
:func:`apply_chrome_mode`。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import boss_db

logger = logging.getLogger(__name__)


@dataclass
class AppSettings:
    #: 空串 = 跟随 ``boss_jobs`` 的默认档位（``hidden``）
    chrome_mode: str = ""
    #: 空串 = 没忽略过任何版本；否则是「忽略水位线」的版本号（不带 ``v`` 前缀）。
    #: 连续忽略多个版本只留最高那一版，比它旧的忽略自动作废。
    ignored_update_version: str = ""

    def normalize(self) -> "AppSettings":
        """把不认识的值收成空串（= 用默认）。"""
        return AppSettings(
            chrome_mode=normalize_chrome_mode(self.chrome_mode),
            ignored_update_version=normalize_version(self.ignored_update_version),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self.normalize())


def normalize_chrome_mode(value: Any) -> str:
    """只接受 ``boss_jobs`` 认得的档位；其余一律回空串。"""
    from boss_jobs.cdp_stoken import CHROME_MODES

    text = str(value or "").strip().lower()
    return text if text in CHROME_MODES else ""


def normalize_version(value: Any) -> str:
    """忽略的版本号统一成「不带 v 前缀」，坏值收成空串。"""
    from .update_check import parse_version

    text = str(value or "").strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    return text if parse_version(text) is not None else ""


def load_settings(path: Path | str | None = None) -> AppSettings:
    """读设置；没有 / 坏了 → 默认值（不报错）。"""
    try:
        raw = boss_db.doc_get_raw(boss_db.DOC_APP_SETTINGS, path)
    except (OSError, sqlite3.Error) as exc:
        logger.warning("状态库打不开，应用设置按默认处理：%s", exc)
        return AppSettings()
    if raw is None:
        return AppSettings()
    try:
        data = json.loads(raw)
    except ValueError as exc:
        logger.warning("应用设置读不出来，按默认处理：%s", exc)
        return AppSettings()
    if not isinstance(data, dict):
        return AppSettings()
    return AppSettings(
        chrome_mode=data.get("chrome_mode", ""),
        ignored_update_version=data.get("ignored_update_version", ""),
    ).normalize()


def save_settings(settings: AppSettings, path: Path | str | None = None) -> Path:
    resolved = boss_db.doc_set(boss_db.DOC_APP_SETTINGS, settings.normalize().to_dict(), path)
    logger.info("应用设置已保存到 %s", resolved)
    return resolved


def _patch(patch: dict[str, Any], path: Path | str | None = None) -> AppSettings:
    """在**已有**设置上改一两个字段，别把其它字段冲掉。"""
    current = load_settings(path)
    merged = asdict(current) | {k: v for k, v in patch.items()}
    settings = AppSettings(**merged).normalize()
    save_settings(settings, path)
    return settings


def apply_chrome_mode(mode: str, path: Path | str | None = None) -> AppSettings:
    """存下档位**并立即生效**。``mode=""`` = 恢复跟随默认。"""
    from boss_jobs.cdp_stoken import CHROME_MODE_ENV

    settings = _patch({"chrome_mode": normalize_chrome_mode(mode)}, path)

    if settings.chrome_mode:
        os.environ[CHROME_MODE_ENV] = settings.chrome_mode
    else:
        os.environ.pop(CHROME_MODE_ENV, None)
    return settings


def apply_ignored_update_version(version: str, path: Path | str | None = None) -> str:
    """把忽略**水位线**抬到 ``version``；空串 = 撤销忽略。返回归一化后的值。

    不是往列表里追加：忽略 ``1.2.0`` 再忽略 ``1.3.0`` 只留下 ``1.3.0``，
    ``≤ 1.3.0`` 全部静音。要更细粒度得存列表，但那会让「已经手滑忽略过一个
    更新的版本，中间的旧版又跳出来」这种事发生，不是用户要的。
    """
    settings = _patch({"ignored_update_version": version}, path)
    return settings.ignored_update_version


def restore_on_startup(path: Path | str | None = None) -> AppSettings:
    """启动时把存过的档位写回环境变量。

    必须在任何 Chrome 启动调用之前跑（``cli.main`` 里紧跟 ``bootstrap()``）。
    """
    settings = load_settings(path)
    if settings.chrome_mode:
        from boss_jobs.cdp_stoken import CHROME_MODE_ENV

        os.environ[CHROME_MODE_ENV] = settings.chrome_mode
    return settings
