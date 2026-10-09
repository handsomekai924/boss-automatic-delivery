"""``boss_web`` — BOSS 直聘可视化控制台。

把登录 / 抓取 / 筛选 / LLM / 简历分析收进一个网页里，能力层全部复用
``boss_login`` / ``boss_jobs`` / ``boss_filter``，本包只做编排与展示。

启动::

    python -m boss_web            # 默认 http://127.0.0.1:8787
"""

from __future__ import annotations

from typing import TYPE_CHECKING

__version__ = "0.1.0"

if TYPE_CHECKING:  # pragma: no cover - 只给类型检查器看
    from .app import create_app

__all__ = ["create_app", "__version__"]


def __getattr__(name: str):
    """惰性导出 ``create_app``。

    这里**不能**在模块顶层 ``from .app import create_app``：那会把 app → api →
    后台任务 → ``boss_jobs`` 整条链拉起来，而打包后的入口脚本必须在这些模块被
    导入之前先跑 :func:`boss_web.runtime.bootstrap`（它负责把状态库钉到 exe
    同目录，晚一步库就建到 PyInstaller 的临时解包目录里去了）。
    """
    if name == "create_app":
        from .app import create_app as factory

        return factory
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
