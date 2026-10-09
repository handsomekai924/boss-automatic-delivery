"""HTTP 路由层。"""

from __future__ import annotations

from fastapi import APIRouter

from . import auth, crawl, deliver, filters, jobs, llm, match, resume, system

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(filters.router, prefix="/filters", tags=["filters"])
api_router.include_router(jobs.router, prefix="/jobs", tags=["jobs"])
api_router.include_router(crawl.router, prefix="/crawl", tags=["crawl"])
api_router.include_router(llm.router, prefix="/llm", tags=["llm"])
api_router.include_router(resume.router, prefix="/resume", tags=["resume"])
api_router.include_router(match.router, prefix="/match", tags=["match"])
api_router.include_router(deliver.router, prefix="/deliver", tags=["deliver"])
api_router.include_router(system.router, prefix="/system", tags=["system"])


@api_router.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"ok": "true", "service": "boss_web"}
