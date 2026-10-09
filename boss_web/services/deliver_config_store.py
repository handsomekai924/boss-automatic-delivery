"""投递配置存取：状态库 ``data/boss.db`` 的 ``doc('deliver_config')``。

目前只有一项——**一键投递的评分阈值** ``min_score``（默认 70）：匹配分 ≥ 它的
岗位才进「一键发送全部」。做法照搬 :mod:`boss_web.services.llm_config_store`：
整包存 JSON、读时归一化、坏值不报错只回默认。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import boss_db

from .. import config as C

logger = logging.getLogger(__name__)

#: 阈值的合法区间（0-100 分制）
MIN_SCORE_FLOOR = 0
MIN_SCORE_CEILING = 100


@dataclass
class DeliverConfig:
    min_score: int = C.DEFAULT_DELIVER_MIN_SCORE

    def normalize(self) -> "DeliverConfig":
        """把分数夹回 0-100：历史配置 / 手改坏的值都在这里收口。"""
        return DeliverConfig(min_score=clamp_min_score(self.min_score))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self.normalize())


def clamp_min_score(value: Any) -> int:
    """夹到 ``[0, 100]``；不是数字就回默认值。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return C.DEFAULT_DELIVER_MIN_SCORE
    return max(MIN_SCORE_FLOOR, min(MIN_SCORE_CEILING, n))


def load_config(path: Path | str | None = None) -> DeliverConfig:
    """读状态库里的投递配置；没有 / 坏了 → 默认配置（不报错）。

    :param path: 状态库路径；省略 = ``BOSS_DB`` = ``data/boss.db``
    """
    try:
        raw = boss_db.doc_get_raw(boss_db.DOC_DELIVER_CONFIG, path)
    except (OSError, sqlite3.Error) as exc:
        logger.warning("状态库打不开，投递配置按默认处理：%s", exc)
        return DeliverConfig()
    if raw is None:
        return DeliverConfig()
    try:
        data = json.loads(raw)
    except ValueError as exc:
        logger.warning("投递配置读不出来，按默认处理：%s", exc)
        return DeliverConfig()
    if not isinstance(data, dict):
        return DeliverConfig()
    return DeliverConfig(min_score=data.get("min_score", C.DEFAULT_DELIVER_MIN_SCORE)).normalize()


def save_config(cfg: DeliverConfig, path: Path | str | None = None) -> Path:
    """写回状态库；返回库路径。

    :param path: 状态库路径；省略 = ``BOSS_DB`` = ``data/boss.db``
    """
    resolved = boss_db.doc_set(boss_db.DOC_DELIVER_CONFIG, cfg.normalize().to_dict(), path)
    logger.info("投递配置已保存到 %s", resolved)
    return resolved


def update_config(payload: dict[str, Any], path: Path | str | None = None) -> DeliverConfig:
    """合并更新：``min_score`` 传空 / 非法则保持旧值。"""
    current = load_config(path)
    if payload.get("min_score") is not None:
        current = DeliverConfig(min_score=clamp_min_score(payload["min_score"]))
    save_config(current, path)
    return current.normalize()
