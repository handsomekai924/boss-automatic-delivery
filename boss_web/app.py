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
    """组装 FastAPI 应用：挂 `/api` 路由、静态资源与异常壳。"""
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
        """业务错误回 JSON，并按状态码落一行日志。"""
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
        """取令牌失败翻成环境问题人话，回 503。"""
        logger.warning("取安全令牌失败：%s", exc)
        return JSONResponse(
            status_code=503,
            content={"ok": False, "code": "environment", "message": humanize(exc)},
        )

    @app.exception_handler(JobError)
    async def _job_error(_: Request, exc: JobError) -> JSONResponse:  # noqa: ANN202
        """`boss_jobs` 的异常翻成人话，回 502，不掉进兜底 500。"""
        logger.warning("职位/采集调用失败：%s", exc)
        return JSONResponse(
            status_code=502,
            content={"ok": False, "code": "upstream", "message": humanize(exc)},
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:  # noqa: ANN202
        """兜底：未预料异常回 500 人话，不甩 traceback。"""
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
            """静态与接口响应加禁缓存头，避免跨文件版本错配与旧登录态。"""
            response = await call_next(request)
            if request.url.path.endswith(".js"):
                response.headers["Content-Type"] = "application/javascript; charset=utf-8"
            if request.url.path.startswith("/static/") or request.url.path == "/":
                response.headers["Cache-Control"] = "no-cache, must-revalidate"
            elif request.url.path.startswith("/api/"):
                response.headers["Cache-Control"] = "no-store"
            return response

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:  # noqa: ANN202
            """回 SPA 首页壳。"""
            return FileResponse(static_dir / "index.html")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa_fallback(full_path: str) -> FileResponse:  # noqa: ANN202
            """未命中路径回 SPA 壳；真静态文件直接回文件。"""
            candidate = static_dir / full_path
            if full_path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(static_dir / "index.html")

    return app


def app_factory() -> FastAPI:
    """uvicorn `factory=True` 入口。"""
    return create_app()
