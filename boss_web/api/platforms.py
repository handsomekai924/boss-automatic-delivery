"""平台描述符接口：前端切换器/侧边栏/能力降级的唯一来源。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ..core.registry import all_adapters, build_descriptor

router = APIRouter()


@router.get("")
@router.get("/")
def list_platforms() -> dict[str, Any]:
    """列出全部已注册站点的描述符。

    由 server 启动时的注册表聚合各 adapter 的 `caps()` / `nav_extras` / 会话摘要，
    **前端不许写死平台清单**（docs/multiplatform-console-redesign.md 6.2）。

    Returns:
        `{"platforms": [PlatformDescriptor, ...]}`，形状与前端
        `apps/web/src/platform/types.ts` 的 `PlatformDescriptor` 同构。
    """
    return {"platforms": [build_descriptor(a) for a in all_adapters()]}
