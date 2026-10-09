"""用户级应用设置：状态库 ``doc('app_settings')``。

目前只有一项 ``chrome_mode``（Chrome 窗口档位）。为什么要它：``boss_jobs``
默认用 ``hidden``（屏幕外 + Win32 隐藏）拉 Chrome，用户不必被弹一脸窗口——
可一旦账号命中风控、**需要人工过滑块**，隐藏的窗口连任务栏里都没有，人是够不
着的。打包给非技术用户后必须有个界面开关能把它切成 ``visible``。

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

    def normalize(self) -> "AppSettings":
        """把不认识的值收成空串（= 用默认）。"""
        return AppSettings(chrome_mode=normalize_chrome_mode(self.chrome_mode))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self.normalize())


def normalize_chrome_mode(value: Any) -> str:
    """只接受 ``boss_jobs`` 认得的档位；其余一律回空串。"""
    from boss_jobs.cdp_stoken import CHROME_MODES

    text = str(value or "").strip().lower()
    return text if text in CHROME_MODES else ""


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
    return AppSettings(chrome_mode=data.get("chrome_mode", "")).normalize()


def save_settings(settings: AppSettings, path: Path | str | None = None) -> Path:
    resolved = boss_db.doc_set(boss_db.DOC_APP_SETTINGS, settings.normalize().to_dict(), path)
    logger.info("应用设置已保存到 %s", resolved)
    return resolved


def apply_chrome_mode(mode: str, path: Path | str | None = None) -> AppSettings:
    """存下档位**并立即生效**。``mode=""`` = 恢复跟随默认。"""
    from boss_jobs.cdp_stoken import CHROME_MODE_ENV

    normalized = normalize_chrome_mode(mode)
    settings = AppSettings(chrome_mode=normalized)
    save_settings(settings, path)

    if normalized:
        os.environ[CHROME_MODE_ENV] = normalized
    else:
        os.environ.pop(CHROME_MODE_ENV, None)
    return settings


def restore_on_startup(path: Path | str | None = None) -> AppSettings:
    """启动时把存过的档位写回环境变量。

    必须在任何 Chrome 启动调用之前跑（``cli.main`` 里紧跟 ``bootstrap()``）。
    """
    settings = load_settings(path)
    if settings.chrome_mode:
        from boss_jobs.cdp_stoken import CHROME_MODE_ENV

        os.environ[CHROME_MODE_ENV] = settings.chrome_mode
    return settings
