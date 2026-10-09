"""运行环境探测：把「开发模式」和「打包成 exe」两种形态的路径差异收在一处。

只依赖标准库，方便单测；对环境变量名的引用一律延迟导入，免得为了拿两个字符串
就把 ``boss_jobs``（连带 requests / websocket-client）整条依赖链拖进来。

两种形态的差别只有两点：

============  ==========================  ================================
             开发（``python -m boss_web``）  打包（双击 exe）
============  ==========================  ================================
只读资源      仓库根                        PyInstaller 解包目录 ``_MEIPASS``
可写数据      仓库 ``data/``                exe 同目录 ``data/``（便携）
============  ==========================  ================================

打包形态下**必须**调一次 :func:`bootstrap`（在任何 store / Chrome 调用之前），
否则 ``boss_db`` 会把库建到 ``_MEIPASS`` 里——那个目录进程一退就被清空，
用户的简历、登录态、职位全没。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

#: 数据目录名。与仓库既有的 ``data/`` 同名，README 里「拷走 data 文件夹就是备份」
#: 的说法在打包后依然成立。
DATA_DIR_NAME = "data"

#: 退回用户目录时用的子目录名
APP_DIR_NAME = "BossAutoDelivery"

#: 覆盖数据目录的钩子。设了它，开发态也能强制走打包态的路径逻辑（便于验证）。
APP_DATA_ENV = "BOSS_APP_DATA"


def is_frozen() -> bool:
    """是不是 PyInstaller 打出来的可执行文件。"""
    return bool(getattr(sys, "frozen", False))


def force_utf8_streams() -> None:
    """Windows 控制台默认 GBK，中文/符号会直接 ``UnicodeEncodeError``。

    必须在第一次 ``print`` 之前调用——横幅里全是中文。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:  # pragma: no branch
            reconfigure(encoding="utf-8", errors="replace")


def resource_root() -> Path:
    """只读资源的根目录。

    - 开发态：仓库根（``config.py`` 的上两级）
    - 打包态：``sys._MEIPASS``，即 PyInstaller 的解包目录

    显式用 ``_MEIPASS`` 而不是靠模块 ``__file__``，是因为后者在冻结环境下的
    行为依赖 PyInstaller 的实现细节。
    """
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        return Path(meipass) if meipass else Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def executable_dir() -> Path:
    """exe 所在目录；开发态退回仓库根。

    onefile 打出来的包，``sys.executable`` 指向**用户双击的那个 exe**（不是解包
    临时目录），所以拿它定位「exe 同目录」是对的。
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return resource_root()


def app_data_dir() -> Path:
    """**期望**的数据目录：打包态 = exe 同目录下的 ``data/``；开发态 = 仓库 ``data/``。

    只算路径、不建目录、不保证可写——真正落地用 :func:`ensure_writable`。
    """
    env = os.environ.get(APP_DATA_ENV, "").strip()
    if env:
        return Path(env)
    return executable_dir() / DATA_DIR_NAME


def fallback_data_dir() -> Path:
    """exe 同目录写不了时的备选（``%LOCALAPPDATA%\\BossAutoDelivery\\data``）。

    常见于 exe 被放进 ``C:\\Program Files`` 或只读盘。
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / APP_DIR_NAME / DATA_DIR_NAME
    return Path.home() / f".{APP_DIR_NAME.lower()}" / DATA_DIR_NAME


def temp_data_dir() -> Path:
    """最后的兜底：临时目录。能跑，但退出后不保证还在。"""
    return Path(tempfile.gettempdir()) / APP_DIR_NAME / DATA_DIR_NAME


def is_writable(folder: Path) -> bool:
    """真写一个探针文件再删掉——比 ``os.access`` 可靠（Windows 上它不看 ACL）。"""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def ensure_writable(folder: Path) -> Path:
    """``folder`` 可写就用它，否则依次退到用户目录、临时目录。

    :raises RuntimeError: 三个位置都写不了（磁盘满 / 权限全无）
    """
    for candidate in (folder, fallback_data_dir(), temp_data_dir()):
        if is_writable(candidate):
            return candidate
    raise RuntimeError(f"找不到可写的数据目录，试过：{folder}")


def is_temp_run() -> bool:
    """exe 是不是在临时目录里跑——多半是**直接从压缩包预览双击**。

    那种情况下 Windows 先解压到 ``%TEMP%`` 再运行，数据会写进临时目录，
    关掉就没。这个信号用来在界面上提示用户「先解压再用」。
    """
    if not is_frozen():
        return False
    try:
        exe_dir = executable_dir()
        temp = Path(tempfile.gettempdir()).resolve()
        return exe_dir == temp or temp in exe_dir.parents
    except OSError:  # pragma: no cover - 路径解析失败不该拦住启动
        return False


def _env_names() -> tuple[str, str]:
    """``(库路径环境变量, Chrome profile 环境变量)``，取自各包自己的常量。

    延迟导入：这几个模块会连带 requests / websocket-client，而 :func:`bootstrap`
    要在它们被导入之前跑完。
    """
    from boss_db import DB_ENV
    from boss_jobs.cdp_stoken import CHROME_PROFILE_ENV

    return DB_ENV, CHROME_PROFILE_ENV


def bootstrap(*, force: bool = False) -> dict[str, str]:
    """把数据目录钉进环境变量；返回一份可打日志 / 可给界面看的摘要。

    开发态什么都不设（继续用仓库 ``data/``，行为与改造前完全一致），除非显式
    ``force=True`` 或设了 :data:`APP_DATA_ENV`。打包态一律设为
    ``<exe 同目录>/data/``；写不进去就自动退到用户目录。

    用 ``setdefault``：用户（或测试）显式设过的环境变量优先。
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
            # 退到了备选目录就得让界面告诉用户「东西存哪了」，否则找不到备份
            "portable": "1" if data == expected else "0",
        }
    )
    if is_temp_run():
        info["warning"] = "temp_run"
    return info


def log_dir() -> Path:
    """日志目录（``data/logs``）；顺手建出来。"""
    folder = app_data_dir() / "logs"
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:  # pragma: no cover - 日志目录建不出来不该拦住启动
        return temp_data_dir() / "logs"
    return folder
