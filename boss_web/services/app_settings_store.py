"""用户级应用设置：状态库 `doc('app_settings')`。

三项：`chrome_mode`（Chrome 窗口档位）、`ignored_update_version`
（忽略更新的水位线版本号）、`greeting_requirement`（自定义招呼语约束，
空串 = 用 `greeting_prompt.GREETING_REQUIREMENT` 默认）。整包存 JSON、
读时归一化、坏值不报错只回默认。档位一改就要立刻同步到 `os.environ`，
否则要重启才生效。
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
    """用户级应用设置。

    Attributes:
        chrome_mode: 空串 = 跟随 `boss_jobs` 的默认档位（`hidden`）。
        ignored_update_version: 空串 = 没忽略过任何版本；否则是「忽略水位线」
            的版本号（不带 `v` 前缀）。连续忽略多个版本只留最高那一版，
            比它旧的忽略自动作废。
        greeting_requirement: 空串 = 用 `greeting_prompt.GREETING_REQUIREMENT`
            默认；非空 = 用户自定义的招呼语生成约束。
    """

    chrome_mode: str = ""
    ignored_update_version: str = ""
    greeting_requirement: str = ""

    def normalize(self) -> "AppSettings":
        """把不认识的值收成空串（= 用默认）。"""
        return AppSettings(
            chrome_mode=normalize_chrome_mode(self.chrome_mode),
            ignored_update_version=normalize_version(self.ignored_update_version),
            greeting_requirement=normalize_greeting_requirement(self.greeting_requirement),
        )

    def to_dict(self) -> dict[str, Any]:
        """归一化后转普通字典。"""
        return asdict(self.normalize())


def normalize_chrome_mode(value: Any) -> str:
    """只接受 `boss_jobs` 认得的档位；其余一律回空串。"""
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


GREETING_REQUIREMENT_MAX_LEN = 500


def normalize_greeting_requirement(value: Any) -> str:
    """自定义招呼语约束：去空白、超长截断；空串 = 用默认。"""
    text = str(value or "").strip()
    return text[:GREETING_REQUIREMENT_MAX_LEN]


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
        greeting_requirement=data.get("greeting_requirement", ""),
    ).normalize()


def save_settings(settings: AppSettings, path: Path | str | None = None) -> Path:
    """归一化后写回状态库。

    Args:
        settings: 待保存的设置。
        path: 状态库路径；省略 = `BOSS_DB` = `data/boss.db`。

    Returns:
        实际写入的库路径。
    """
    resolved = boss_db.doc_set(boss_db.DOC_APP_SETTINGS, settings.normalize().to_dict(), path)
    logger.info("应用设置已保存到 %s", resolved)
    return resolved


def _patch(patch: dict[str, Any], path: Path | str | None = None) -> AppSettings:
    """在已有设置上改一两个字段，别把其它字段冲掉。"""
    current = load_settings(path)
    merged = asdict(current) | {k: v for k, v in patch.items()}
    settings = AppSettings(**merged).normalize()
    save_settings(settings, path)
    return settings


def apply_chrome_mode(mode: str, path: Path | str | None = None) -> AppSettings:
    """存下档位并立即生效。`mode=""` = 恢复跟随默认。

    Args:
        mode: 目标档位；不认识的值会被收成空串。
        path: 状态库路径；省略 = `BOSS_DB` = `data/boss.db`。
    """
    from boss_jobs.cdp_stoken import CHROME_MODE_ENV

    settings = _patch({"chrome_mode": normalize_chrome_mode(mode)}, path)

    if settings.chrome_mode:
        os.environ[CHROME_MODE_ENV] = settings.chrome_mode
    else:
        os.environ.pop(CHROME_MODE_ENV, None)
    return settings


def apply_ignored_update_version(version: str, path: Path | str | None = None) -> str:
    """把忽略水位线抬到 `version`；空串 = 撤销忽略。

    Args:
        version: 要忽略到的版本号。
        path: 状态库路径；省略 = `BOSS_DB` = `data/boss.db`。

    Returns:
        归一化后的水位线版本号。
    """
    settings = _patch({"ignored_update_version": version}, path)
    return settings.ignored_update_version


def apply_greeting_requirement(text: str, path: Path | str | None = None) -> str:
    """存下自定义招呼语约束；空串 = 恢复出厂默认。

    Args:
        text: 自定义约束文本；超长截断到 500 字。
        path: 状态库路径；省略 = `BOSS_DB` = `data/boss.db`。

    Returns:
        归一化后的自定义文本（空串 = 用默认）。
    """
    settings = _patch({"greeting_requirement": text}, path)
    return settings.greeting_requirement


def restore_on_startup(path: Path | str | None = None) -> AppSettings:
    """启动时把存过的档位写回环境变量，必须在任何 Chrome 启动之前跑。

    Args:
        path: 状态库路径；省略 = `BOSS_DB` = `data/boss.db`。
    """
    settings = load_settings(path)
    if settings.chrome_mode:
        from boss_jobs.cdp_stoken import CHROME_MODE_ENV

        os.environ[CHROME_MODE_ENV] = settings.chrome_mode
    return settings
