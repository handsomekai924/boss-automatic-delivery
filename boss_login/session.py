"""登录态持久化。

把 token / Cookie 落到本地文件，下次启动可直接复用，避免频繁触发短信。
默认路径是**项目根目录**下的 ``session.json``（按 ``__file__`` 定位，跟当前
工作目录无关——从别处 ``python -m boss_login`` 也读写同一个文件）。
写入时权限收紧到 600（Windows 上忽略）。
"""

from __future__ import annotations

import json
import logging
import os
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: 项目根目录 = boss_login/ 的上一级。按模块位置算，不看 cwd。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SESSION_PATH = PROJECT_ROOT / "session.json"


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


def save_session(session: StoredSession, path: Path | str = DEFAULT_SESSION_PATH) -> Path:
    """写入登录态，并把文件权限设为仅本人可读写。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "token": session.token,
        "cookies": session.cookies,
        "phone_masked": session.phone_masked,
        "user": session.user,
        "saved_at": session.saved_at or time.time(),
    }
    # 先写临时文件再替换，避免写到一半崩溃留下损坏文件
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _restrict_permissions(tmp)
    os.replace(tmp, path)
    logger.info("登录态已保存到 %s", path)
    return path


def load_session(path: Path | str = DEFAULT_SESSION_PATH) -> StoredSession:
    """读取登录态；文件不存在或损坏时返回空会话。"""
    path = Path(path)
    if not path.exists():
        return StoredSession()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("登录态文件无法解析，按未登录处理：%s", exc)
        return StoredSession()

    return StoredSession(
        token=str(payload.get("token") or ""),
        cookies={str(k): str(v) for k, v in (payload.get("cookies") or {}).items()},
        phone_masked=str(payload.get("phone_masked") or ""),
        user=payload.get("user") or {},
        saved_at=float(payload.get("saved_at") or 0.0),
    )


def clear_session(path: Path | str = DEFAULT_SESSION_PATH) -> bool:
    """删除本地登录态，返回是否真的删掉了文件。"""
    path = Path(path)
    if not path.exists():
        return False
    path.unlink()
    logger.info("本地登录态已清除：%s", path)
    return True


def _restrict_permissions(path: Path) -> None:
    """等价于 chmod 600；Windows 上 chmod 语义有限，失败不影响主流程。"""
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:  # pragma: no cover - 取决于运行平台
        logger.debug("收紧文件权限失败（可忽略）：%s", exc)
