"""简历上传 / LLM 解析 / 历史分析查看。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, UploadFile
from pydantic import BaseModel, Field

from ..errors import NotFoundError, ValidationWebError
from ..services.llm_config_store import load_config
from ..services.resume_analyzer import analyze_tasks
from ..services.resume_parser import ResumeParseError, parse_and_stamp
from ..services.resume_store import (
    delete_analysis,
    delete_resume,
    list_analyses,
    list_resumes,
    load_analysis,
    load_llm_parse,
    load_resume,
    save_llm_parse,
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
    """只存原文；解析由 ``POST /item/{id}/parse`` 完成（上传成功后前端自动调）。"""
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
        llm = load_llm_parse(resume_id)
    except FileNotFoundError as exc:
        raise NotFoundError(f"简历不存在：{resume_id}") from exc
    data = draft.to_dict(include_raw=True)
    data["llm"] = llm
    return data


@router.post("/item/{resume_id}/parse")
def parse_resume_item(resume_id: str) -> dict[str, Any]:
    """LLM 固定模板解析（同步，超时按 ``LLM_TIMEOUT``）。重跑会覆盖 ``meta.llm``。"""
    try:
        draft = load_resume(resume_id)
    except FileNotFoundError as exc:
        raise NotFoundError(f"简历不存在：{resume_id}") from exc

    cfg = load_config()
    if not cfg.configured:
        raise ValidationWebError(
            "LLM 还没配置，请先到「模型」页填好 API Key / Base URL / 模型名"
        )
    try:
        llm_payload = parse_and_stamp(draft.raw, model=cfg.model, config=cfg)
    except ResumeParseError as exc:
        raise ValidationWebError(str(exc)) from exc

    save_llm_parse(resume_id, llm_payload)
    return {"resume_id": resume_id, "llm": llm_payload}


@router.get("/item/{resume_id}/parse")
def get_parse(resume_id: str) -> dict[str, Any]:
    try:
        llm = load_llm_parse(resume_id)
    except FileNotFoundError as exc:
        raise NotFoundError(f"简历不存在：{resume_id}") from exc
    return {"resume_id": resume_id, "llm": llm, "parsed": llm is not None}


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
