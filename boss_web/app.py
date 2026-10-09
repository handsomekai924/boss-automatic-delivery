"""FastAPI 应用工厂：挂路由、静态资源、异常壳。"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config as C
from .api import api_router
from .errors import WebError

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
    async def _web_error(_: Request, exc: WebError) -> JSONResponse:  # noqa: ANN202
        return JSONResponse(status_code=exc.status_code, content=exc.payload())

    static_dir = C.STATIC_DIR
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

        @app.middleware("http")
        async def _no_cache_static(request: Request, call_next):  # noqa: ANN202
            response = await call_next(request)
            # JS/CSS 是 ES module 依赖图的一部分，缓存过期会造成跨文件版本错配
            if request.url.path.startswith("/static/") or request.url.path == "/":
                response.headers["Cache-Control"] = "no-cache, must-revalidate"
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
