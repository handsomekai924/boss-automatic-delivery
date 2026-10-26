"""`boss_web` — BOSS 直聘可视化控制台。

把登录 / 抓取 / 筛选 / LLM / 简历分析收进一个网页里，能力层全部复用
`boss_login` / `boss_jobs` / `boss_filter`，本包只做编排与展示。

启动：

    python -m boss_web            # 默认 http://127.0.0.1:8787
"""

from __future__ import annotations

from typing import TYPE_CHECKING

__version__ = "1.2.4"

if TYPE_CHECKING:  # pragma: no cover - 只给类型检查器看
    from .app import create_app

__all__ = ["create_app", "__version__"]


def __getattr__(name: str):
    """惰性导出 `create_app`，避免导入期拉起 `boss_jobs` 依赖链。"""
    if name == "create_app":
        from .app import create_app as factory

        return factory
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
