"""运行环境探测：开发模式与打包成 exe 两种形态的路径差异收在一处。

只依赖标准库；环境变量名延迟导入，免得拖进 `boss_jobs` 整条依赖链。
打包形态必须先调 `bootstrap` 再碰 store / Chrome。明细见 `notes/boss_web/runtime.md`。

Attributes:
    DATA_DIR_NAME: 数据目录名。
    APP_DIR_NAME: 退回用户目录时用的子目录名。
    APP_DATA_ENV: 覆盖数据目录的环境变量名。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

DATA_DIR_NAME = "data"

APP_DIR_NAME = "BossAutoDelivery"

APP_DATA_ENV = "BOSS_APP_DATA"


def is_frozen() -> bool:
    """是不是 PyInstaller 打出来的可执行文件。"""
    return bool(getattr(sys, "frozen", False))


def force_utf8_streams() -> None:
    """把 stdout/stderr 切成 UTF-8；必须在第一次 `print` 之前调用。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:  # pragma: no branch
            reconfigure(encoding="utf-8", errors="replace")


def resource_root() -> Path:
    """只读资源的根目录：开发态为仓库根，打包态为 `sys._MEIPASS`。"""
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        return Path(meipass) if meipass else Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def executable_dir() -> Path:
    """exe 所在目录；开发态退回仓库根。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return resource_root()


def app_data_dir() -> Path:
    """期望的数据目录：打包态为 exe 同目录下的 `data/`，开发态为仓库 `data/`。

    只算路径、不建目录、不保证可写；真正落地用 `ensure_writable`。
    """
    env = os.environ.get(APP_DATA_ENV, "").strip()
    if env:
        return Path(env)
    return executable_dir() / DATA_DIR_NAME


def fallback_data_dir() -> Path:
    """exe 同目录写不了时的备选（`%LOCALAPPDATA%\\BossAutoDelivery\\data`）。"""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / APP_DIR_NAME / DATA_DIR_NAME
    return Path.home() / f".{APP_DIR_NAME.lower()}" / DATA_DIR_NAME


def temp_data_dir() -> Path:
    """最后的兜底：临时目录。能跑，但退出后不保证还在。"""
    return Path(tempfile.gettempdir()) / APP_DIR_NAME / DATA_DIR_NAME


def is_writable(folder: Path) -> bool:
    """真写一个探针文件再删掉；比 `os.access` 可靠。"""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def ensure_writable(folder: Path) -> Path:
    """`folder` 可写就用它，否则依次退到用户目录、临时目录；都写不了时抛 `RuntimeError`。"""
    for candidate in (folder, fallback_data_dir(), temp_data_dir()):
        if is_writable(candidate):
            return candidate
    raise RuntimeError(f"找不到可写的数据目录，试过：{folder}")


def is_temp_run() -> bool:
    """exe 是不是在临时目录里跑（多半是直接从压缩包预览双击）。"""
    if not is_frozen():
        return False
    try:
        exe_dir = executable_dir()
        temp = Path(tempfile.gettempdir()).resolve()
        return exe_dir == temp or temp in exe_dir.parents
    except OSError:  # pragma: no cover - 路径解析失败不该拦住启动
        return False


def _env_names() -> tuple[str, str]:
    """`(库路径环境变量, Chrome profile 环境变量)`，延迟导入取自各包常量。"""
    from boss_db import DB_ENV
    from boss_jobs.cdp_stoken import CHROME_PROFILE_ENV

    return DB_ENV, CHROME_PROFILE_ENV


def bootstrap(*, force: bool = False) -> dict[str, str]:
    """把数据目录钉进环境变量；返回一份可打日志 / 可给界面看的摘要。

    开发态什么都不设，除非显式 `force=True` 或设了 `APP_DATA_ENV`；
    打包态一律设为 `<exe 同目录>/data/`，写不进去就退到用户目录。
    用 `setdefault`：用户（或测试）显式设过的环境变量优先。
    """
    expected = app_data_dir()
    info: dict[str, str] = {
        "frozen": "1" if is_frozen() else "0",
        "expected_data_dir": str(expected),
    }

    if not (is_frozen() or force or os.environ.get(APP_DATA_ENV, "").strip()):
        info["data_dir"] = str(expected)
        return info

    data = ensure_writable(expected)
    db_env, profile_env = _env_names()
    os.environ.setdefault(db_env, str(data / "boss.db"))
    os.environ.setdefault(profile_env, str(data / "chrome_profile"))
    (data / "logs").mkdir(parents=True, exist_ok=True)

    info.update(
        {
            "data_dir": str(data),
            "db": os.environ[db_env],
            "chrome_profile": os.environ[profile_env],
            "portable": "1" if data == expected else "0",
        }
    )
    if is_temp_run():
        info["warning"] = "temp_run"
    return info


def log_dir() -> Path:
    """日志目录（`data/logs`）；顺手建出来。"""
    folder = app_data_dir() / "logs"
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:  # pragma: no cover - 日志目录建不出来不该拦住启动
        return temp_data_dir() / "logs"
    return folder
