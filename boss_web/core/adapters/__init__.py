"""站点适配器包：装配点——`load()` 把各站点挂进注册表。

server 取适配器前会调一次（`registry._ensure_loaded`），测试 `reset_registry()`
之后重调即可原样挂回。新增站点 = 加一个 `xxx.py` + 这里多一行。
"""

from __future__ import annotations

from ..registry import register


def load() -> None:
    """幂等装配：缺哪个补哪个（同类型重复注册是原地替换，不报错）。"""
    from .boss import BossAdapter

    register("boss")(BossAdapter)


__all__ = ["load"]
