"""登录态持久化。

把 token / Cookie 落进状态库 ``data/boss.db`` 的 ``doc('session')`` 行，下次启动
可直接复用，避免频繁触发短信。按 ``__file__`` 定位项目根，跟当前工作目录无关
——从别处 ``python -m boss_login`` 也读写同一份。库文件建好后权限收紧到 600
（Windows 上忽略），见 :mod:`boss_db`。
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import boss_db

logger = logging.getLogger(__name__)

#: 项目根目录 = boss_login/ 的上一级。按模块位置算，不看 cwd。
PROJECT_ROOT = boss_db.PROJECT_ROOT

#: 状态库路径（登录态是里面的一行 ``doc('session')``）。环境变量 ``BOSS_DB`` 可覆盖。
DEFAULT_DB_PATH = boss_db.DEFAULT_DB_PATH


@dataclass
class StoredSession:
    """落盘的登录态。"""

    token: str = ""
    cookies: dict[str, str] = field(default_factory=dict)
    phone_masked: str = ""
    user: dict[str, Any] = field(default_factory=dict)
    saved_at: float = 0.0

    @property
    def is_empty(self) -> bool:
        return not self.token and not self.cookies

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.saved_at) if self.saved_at else 0.0


def save_session(
    session: StoredSession, path: Path | str | None = None
) -> Path:
    """写入登录态，返回状态库路径。"""
    payload = {
        "token": session.token,
        "cookies": session.cookies,
        "phone_masked": session.phone_masked,
        "user": session.user,
        "saved_at": session.saved_at or time.time(),
    }
    resolved = boss_db.doc_set(boss_db.DOC_SESSION, payload, path)
    logger.info("登录态已保存到 %s", resolved)
    return resolved


def load_session(path: Path | str | None = None) -> StoredSession:
    """读取登录态；库里没有、payload 坏了、库文件打不开，都按未登录处理。"""
    try:
        payload = boss_db.doc_get(boss_db.DOC_SESSION, path)
    except (OSError, sqlite3.Error) as exc:
        logger.warning("状态库打不开，按未登录处理：%s", exc)
        return StoredSession()
    if payload is None:
        return StoredSession()

    return StoredSession(
        token=str(payload.get("token") or ""),
        cookies={str(k): str(v) for k, v in (payload.get("cookies") or {}).items()},
        phone_masked=str(payload.get("phone_masked") or ""),
        user=payload.get("user") or {},
        saved_at=float(payload.get("saved_at") or 0.0),
    )


def clear_session(path: Path | str | None = None) -> bool:
    """删除本地登录态，返回是否真的删掉了东西。"""
    try:
        removed = boss_db.doc_delete(boss_db.DOC_SESSION, path)
    except (OSError, sqlite3.Error) as exc:
        logger.warning("状态库打不开，登录态没删成：%s", exc)
        return False
    if removed:
        logger.info("本地登录态已清除：%s", boss_db.resolve_db_path(path))
    return removed


def has_session_row(path: Path | str | None = None) -> bool:
    """库里有没有 ``doc('session')`` 这一行。

    跟 :func:`load_session` 的「内容是不是空」是两回事——whoami 报错时要分清
    「压根没落盘」和「落了但里面没有凭证」。打不开的库按「没有」处理。
    """
    try:
        return boss_db.doc_get_raw(boss_db.DOC_SESSION, path) is not None
    except (OSError, sqlite3.Error):
        return False
