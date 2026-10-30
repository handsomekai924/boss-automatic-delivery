"""HTTP 路由层。

路由分两类（docs/multiplatform-console-redesign.md 6.3）：

- **平台作用域**（`auth`/`filters`/`jobs`/`crawl`/`collection`/`deliver`/`settings`）：
  走 `/api/sites/{site}/…`；共存期旧 URL 原样保留，当 `site="boss"` 的**兼容别名**
  专供 legacy 前端，M3 随 legacy 一起删——新代码禁止走别名。
- **共用**（`resume`/`match`/`interview`/`tasks`/`llm`/`system`/`platforms`）：
  不带站点语义；列表接口用 `?site=` 过滤（过滤器，不是作用域）。

唯一的跨界是**批次账**（`crawl.batch_router`）：抓取在哪跑是站点操作，「这批带回了
哪些职位」是库账，所以 `/crawl/tasks…` 既有站点口（按 `{site}` 切列表）也有共用口
（不切）。详见 `crawl.py` 模块注释。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from ..core.registry import registered_sites
from ..errors import NotFoundError
from . import auth, collection, crawl, deliver, filters, interview, jobs, llm, match, platforms, resume, settings, system, tasks

#: 兼容别名的归属站点：legacy 前端是单站点（BOSS）壳，旧 URL 就是这个站点的。
COMPAT_SITE: str = "boss"

#: 平台作用域路由（模块、挂载前缀、标签）。同一个 router 挂两遍：新面带 `{site}`，
#: 旧面留别名。BOSS 私有入口（求职期望）由 adapter 的 `nav_extras` 声明，M3 接进来。
PLATFORM_SCOPED: tuple[tuple[Any, str, str], ...] = (
    (auth, "/auth", "auth"),
    (filters, "/filters", "filters"),
    (jobs, "/jobs", "jobs"),
    (crawl, "/crawl", "crawl"),
    (collection, "", "collection"),
    (deliver, "/deliver", "deliver"),
    (settings, "/settings", "settings"),
)

api_router = APIRouter()


def resolve_site(request: Request) -> str:
    """把路径里的 `{site}` 解析成已注册站点；别名没这段就落 `COMPAT_SITE`。

    站点没注册直接 404——前端拿到的站点清单来自 `/api/platforms`，
    打过来的也只该是清单里的 id。
    """
    site = str(request.path_params.get("site") or COMPAT_SITE)
    if site not in registered_sites():
        raise NotFoundError(f"没这个平台：{site}")
    request.state.site = site
    return site


# 批次账（`crawl.batch_router`）与抓取执行不是一类：批次账是库账，共用页拿它当
# 职位来源选择器；执行是站点操作（限速/并发通道都在站点上）。两边各挂一遍——
# 站点口按 `{site}` 切列表，共用口不切。
#
# **挂载顺序有讲究**：批次账必须排在 `crawl.router` 前面，否则 `/{task_id}`
# 会把 `tasks` 当任务号吃掉（`tests/test_crawl_tasks.py` 有守这条）。
api_router.include_router(
    crawl.batch_router,
    prefix=f"/sites/{{site}}/crawl",
    dependencies=[Depends(resolve_site)],
    tags=["crawl"],
)

for _mod, _prefix, _tag in PLATFORM_SCOPED:
    api_router.include_router(
        _mod.router,
        prefix=f"/sites/{{site}}{_prefix}",
        dependencies=[Depends(resolve_site)],
        tags=[_tag],
    )

api_router.include_router(llm.router, prefix="/llm", tags=["llm"])
api_router.include_router(resume.router, prefix="/resume", tags=["resume"])
api_router.include_router(match.router, prefix="/match", tags=["match"])
api_router.include_router(interview.router, prefix="/interview", tags=["interview"])
api_router.include_router(system.router, prefix="/system", tags=["system"])
api_router.include_router(tasks.router, prefix="", tags=["tasks"])
api_router.include_router(platforms.router, prefix="/platforms", tags=["platforms"])

# 共用批次账：**不带** `resolve_site`——共用页读批次不该被套上站点作用域
# （红线「共用页不带站点语义」）。要挑站点用 `?site=` 过滤器。同样排在别名循环前。
api_router.include_router(crawl.batch_router, prefix="/crawl", tags=["crawl"])

for _mod, _prefix, _tag in PLATFORM_SCOPED:
    api_router.include_router(
        _mod.router,
        prefix=_prefix,
        dependencies=[Depends(resolve_site)],
        tags=[_tag],
    )


@api_router.get("/health", tags=["system"])
def health() -> dict[str, str]:
    """健康检查探针。"""
    return {"ok": "true", "service": "boss_web"}
