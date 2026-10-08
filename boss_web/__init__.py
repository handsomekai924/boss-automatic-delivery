"""``boss_web`` — BOSS 直聘可视化控制台。

把登录 / 抓取 / 筛选 / LLM / 简历分析收进一个网页里，能力层全部复用
``boss_login`` / ``boss_jobs`` / ``boss_filter``，本包只做编排与展示。

启动::

    python -m boss_web            # 默认 http://127.0.0.1:8787
"""

from __future__ import annotations

__version__ = "0.1.0"

from .app import create_app

__all__ = ["create_app", "__version__"]
