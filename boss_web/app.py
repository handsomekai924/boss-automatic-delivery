"""FastAPI 应用工厂：挂路由、静态资源、异常壳。"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from boss_jobs.cdp_stoken import StokenError
from boss_jobs.errors import JobError

from . import config as C
from .api import api_router
from .errors import WebError
from .services.troubleshoot import humanize

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    app = FastAPI(
        title="BOSS 控制台",
        version=__import__("boss_web", fromlist=["__version__"]).__version__,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    app.include_router(api_router, prefix="/api")

    @app.exception_handler(WebError)
    async def _web_error(request: Request, exc: WebError) -> JSONResponse:  # noqa: ANN202
        # 以前只回 JSON 不留痕，浏览器 <img> 裂图这类失败完全无迹可循（exe 漏打
        # segno 就是这么瞎了很久）。至少落一行日志，5xx 当场醒目，4xx 记线索。
        level = logger.warning if exc.status_code >= 500 else logger.info
        level(
            "业务错误：%s %s -> %s %s",
            request.method,
            request.url.path,
            exc.status_code,
            exc.message,
        )
        return JSONResponse(status_code=exc.status_code, content=exc.payload())

    @app.exception_handler(StokenError)
    async def _stoken_error(_: Request, exc: StokenError) -> JSONResponse:  # noqa: ANN202
        """取令牌失败基本都是本机环境问题（没装 Chrome / 上个 Chrome 没关干净）。

        原文里带 ``ws://127.0.0.1:9222/devtools/browser/<uuid>`` 这种内容，
        直接摆给用户等于没提示，所以翻成人话再回。
        """
        logger.warning("取安全令牌失败：%s", exc)
        return JSONResponse(
            status_code=503,
            content={"ok": False, "code": "environment", "message": humanize(exc)},
        )

    @app.exception_handler(JobError)
    async def _job_error(_: Request, exc: JobError) -> JSONResponse:  # noqa: ANN202
        """``boss_jobs`` 的异常一律翻成人话，不许掉进兜底那个 500。

        这是**安全网**：路由层自己 catch 转 ``UpstreamError`` 是正路，漏一个
        就会像 ``GET /api/collection/expects`` 那样，没登录的用户拿到
        「程序内部出了点问题」——话没错，但完全指不到「去登录」这一步。
        """
        logger.warning("职位/采集调用失败：%s", exc)
        return JSONResponse(
            status_code=502,
            content={"ok": False, "code": "upstream", "message": humanize(exc)},
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:  # noqa: ANN202
        """兜底：没预料到的异常也不许把 traceback 甩到用户脸上。

        以前这类异常走 FastAPI 默认的 500，前端只会显示「HTTP 500」。
        """
        logger.exception("未处理的异常：%s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "code": "internal",
                "message": "程序内部出了点问题，已经记进日志了。"
                "关掉重开一次通常就好；还是不行的话，把「数据目录」里 logs\\boss.log 发给开发者。",
            },
        )

    static_dir = C.STATIC_DIR
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

        @app.middleware("http")
        async def _no_cache_static(request: Request, call_next):  # noqa: ANN202
            response = await call_next(request)
            # JS/CSS 是 ES module 依赖图的一部分，缓存过期会造成跨文件版本错配
            if request.url.path.startswith("/static/") or request.url.path == "/":
                response.headers["Cache-Control"] = "no-cache, must-revalidate"
            # 接口一律不进浏览器缓存：`/api/auth/status` 被缓存过一次「未登录」，
            # 用户登录后再打开首页就还挂着旧状态，得手动刷新才正确（真机踩过）。
            elif request.url.path.startswith("/api/"):
                response.headers["Cache-Control"] = "no-store"
            return response

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:  # noqa: ANN202
            return FileResponse(static_dir / "index.html")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa_fallback(full_path: str) -> FileResponse:  # noqa: ANN202
            # 前端 hash 路由，任意未命中路径都回壳；真静态资源已在 /static 下
            candidate = static_dir / full_path
            if full_path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(static_dir / "index.html")

    return app


def app_factory() -> FastAPI:
    """uvicorn ``factory=True`` 入口。"""
    return create_app()
