"""简历上传 / 解析 / 匹配分析。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, UploadFile
from pydantic import BaseModel, Field

from ..errors import NotFoundError, ValidationWebError
from ..services.resume_analyzer import analyze_tasks
from ..services.resume_store import (
    delete_analysis,
    delete_resume,
    list_analyses,
    list_resumes,
    load_analysis,
    load_resume,
    save_resume,
)

router = APIRouter()

ALLOWED_EXT = {".md", ".markdown", ".txt"}
MAX_BYTES = 2 * 1024 * 1024


class AnalyzeBody(BaseModel):
    resume_id: str
    job_ids: list[str] = Field(default_factory=list)
    top_k: int = Field(20, ge=1, le=50)


@router.get("/list")
def resumes() -> dict[str, Any]:
    return {"items": list_resumes()}


@router.post("/upload")
async def upload(file: UploadFile = File(...)) -> dict[str, Any]:
    name = file.filename or "resume.md"
    if not any(name.lower().endswith(ext) for ext in ALLOWED_EXT):
        raise ValidationWebError("只支持 Markdown（.md）或纯文本（.txt）")
    raw = await file.read()
    if len(raw) > MAX_BYTES:
        raise ValidationWebError("文件超过 2MB，请精简后再传")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("gbk")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")

    draft = save_resume(text, filename=name)
    return draft.to_dict(include_raw=True)


@router.get("/item/{resume_id}")
def get_resume(resume_id: str) -> dict[str, Any]:
    try:
        draft = load_resume(resume_id)
    except FileNotFoundError as exc:
        raise NotFoundError(f"简历不存在：{resume_id}") from exc
    return draft.to_dict(include_raw=True)


@router.delete("/item/{resume_id}")
def remove_resume(resume_id: str) -> dict[str, Any]:
    if not delete_resume(resume_id):
        raise NotFoundError(f"简历不存在：{resume_id}")
    return {"deleted": True}


@router.post("/analyze")
def start_analyze(body: AnalyzeBody) -> dict[str, Any]:
    task = analyze_tasks.start(
        resume_id=body.resume_id,
        job_ids=body.job_ids,
        top_k=body.top_k,
    )
    return task.snapshot()


@router.get("/analyze/status")
def analyze_status() -> dict[str, Any]:
    return analyze_tasks.snapshot()


@router.get("/analyze/{task_id}")
def analyze_task(task_id: str) -> dict[str, Any]:
    return analyze_tasks.snapshot(task_id)


@router.post("/analyze/{task_id}/cancel")
def cancel_analyze(task_id: str) -> dict[str, Any]:
    task = analyze_tasks.cancel(task_id)
    return task.snapshot()


@router.get("/analyses")
def analyses() -> dict[str, Any]:
    return {"items": list_analyses()}


@router.get("/analyses/{analysis_id}")
def get_analysis(analysis_id: str) -> dict[str, Any]:
    try:
        return load_analysis(analysis_id)
    except FileNotFoundError as exc:
        raise NotFoundError(f"分析结果不存在：{analysis_id}") from exc


@router.delete("/analyses/{analysis_id}")
def remove_analysis(analysis_id: str) -> dict[str, Any]:
    if not delete_analysis(analysis_id):
        raise NotFoundError(f"分析结果不存在：{analysis_id}")
    return {"deleted": True}
