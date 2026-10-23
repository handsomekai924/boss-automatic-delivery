"""简历上传 / LLM 解析 / 历史分析查看。

Attributes:
    MAX_BYTES: 纯文本简历大小上限（字节）。
    MAX_BINARY_BYTES: Word / PDF 大小上限（字节），比纯文本宽以免挡正常简历。
    _EXT_HINT: 扩展名不支持时的报错文案。
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from ..errors import NotFoundError, ValidationWebError
from ..services.document_text import (
    ALLOWED_EXT,
    DocumentTextError,
    ext_of,
    extract_text,
)
from ..services.llm_config_store import load_config
from ..services.resume_analyzer import analyze_tasks
from ..services.resume_parser import ResumeParseError, parse_and_stamp
from ..services.resume_revision import build_diff, markdown_to_text
from ..services.resume_revision_free import (
    get_preset_categories,
    get_presets_by_category,
)
from ..services.resume_revision_task import revision_tasks
from ..services.resume_section_parser import extract_sections
from ..services.resume_store import (
    REVISION_SAVED,
    delete_analysis,
    delete_resume,
    delete_revision,
    list_analyses,
    list_resumes,
    list_revisions,
    load_analysis,
    load_llm_parse,
    load_resume,
    load_revision,
    mark_revision_saved,
    revision_path,
    save_llm_parse,
    save_resume,
)

router = APIRouter()

MAX_BYTES = 2 * 1024 * 1024

MAX_BINARY_BYTES = 8 * 1024 * 1024

_EXT_HINT = "只支持 Word（.docx）、PDF（.pdf）、Markdown（.md）或纯文本（.txt）"


class AnalyzeBody(BaseModel):
    """启动批量人岗分析。"""

    resume_id: str
    job_ids: list[str] = Field(default_factory=list)
    top_k: int = Field(20, ge=1, le=50)


class ReviseStartBody(BaseModel):
    """简历优化（定向模式）

    Attributes:
        resume_id: 底稿简历 id。
        job_ids: 目标岗位 id；`crawl_task_id` / `all_repo` 非空时可为空。
        crawl_task_id: 非空 = 只吃该批岗位。
        all_repo: 为 `True` 时以库内全部岗位为目标（多岗位取上限内）。
    """

    resume_id: str
    job_ids: list[str] = Field(default_factory=list)
    crawl_task_id: str = ""
    all_repo: bool = False


class ReviseFreeStartBody(BaseModel):
    """简历优化（自由模式）

    Attributes:
        resume_id: 底稿简历 id。
        selected_sections: 需要优化的章节列表（空 = 全部）。
        optimization_prompt: 优化提示词（预设或自定义）。
    """

    resume_id: str
    selected_sections: list[str] = Field(default_factory=list)
    optimization_prompt: str


class ReviseSaveBody(BaseModel):
    """确认保存简历优化草稿。

    Attributes:
        format: `md` / `txt`。
        filename: 自定义文件名（空 = 默认）。
        title: 入库新简历标题（空 = 默认）。
    """

    format: str = Field("md", pattern="^(md|txt)$")
    filename: str = ""
    title: str = ""


def _pick_target_jobs(
    job_ids: list[str],
    crawl_task_id: str,
    *,
    all_repo: bool = False,
) -> tuple[list[Any], str, dict[str, Any]]:
    """按范围参数解析目标岗位集合。优先级：`crawl_task_id` > `all_repo` > `job_ids`。

    Args:
        job_ids: 显式勾选的岗位 id。
        crawl_task_id: 爬取批次 id；非空时只吃该批。
        all_repo: 为 `True` 时以库内全部岗位为目标。

    Returns:
        `(jobs, target_kind, target_json)`；岗位最多取 10，一个都没有时抛 `ValidationWebError`。
    """
    from boss_jobs.store import JobStore

    with JobStore() as store:
        if crawl_task_id:
            ids = store.crawl_task_job_ids(crawl_task_id, only_existing=True)
            target_kind = "crawl_task"
        elif all_repo:
            ids = [j.encrypt_job_id for j in store.list_jobs(limit=100000, offset=0)]
            target_kind = "all"
        else:
            ids = list(job_ids)
            target_kind = "jobs"
        ids = list(dict.fromkeys(ids))[:5]
        jobs = []
        for jid in ids:
            job = store.get_job(jid)
            if job is not None:
                jobs.append(job)
    if not jobs:
        raise ValidationWebError("没选目标岗位：勾选至少 1 个岗位，或选一个爬取批次 / 全部职位")
    return jobs, target_kind, {
        "kind": target_kind,
        "job_ids": [j.encrypt_job_id for j in jobs],
        "crawl_task_id": crawl_task_id,
        "count": len(jobs),
    }


@router.get("/list")
def resumes() -> dict[str, Any]:
    """简历列表。"""
    return {"items": list_resumes()}


@router.post("/upload")
async def upload(file: UploadFile = File(...)) -> dict[str, Any]:
    """上传简历原文；只存文本，解析由 `/item/{id}/parse` 完成。"""
    name = file.filename or "resume.md"
    if ext_of(name) not in ALLOWED_EXT:
        raise ValidationWebError(_EXT_HINT)
    raw = await file.read()
    limit = MAX_BINARY_BYTES if ext_of(name) in {".docx", ".pdf"} else MAX_BYTES
    if len(raw) > limit:
        raise ValidationWebError(f"文件超过 {limit // 1024 // 1024}MB，请精简后再传")

    try:
        extracted = extract_text(raw, name)
    except DocumentTextError as exc:
        raise ValidationWebError(str(exc)) from exc

    if extracted is not None:
        text = extracted
    else:
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
    """取简历原文与已有 LLM 解析。"""
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
    """LLM 固定模板解析（同步，超时按 `LLM_TIMEOUT`）；重跑会覆盖 `meta.llm`。"""
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
    """读已保存的 LLM 解析结果。"""
    try:
        llm = load_llm_parse(resume_id)
    except FileNotFoundError as exc:
        raise NotFoundError(f"简历不存在：{resume_id}") from exc
    return {"resume_id": resume_id, "llm": llm, "parsed": llm is not None}


@router.delete("/item/{resume_id}")
def remove_resume(resume_id: str) -> dict[str, Any]:
    """删除简历。"""
    if not delete_resume(resume_id):
        raise NotFoundError(f"简历不存在：{resume_id}")
    return {"deleted": True}


@router.post("/analyze")
def start_analyze(body: AnalyzeBody) -> dict[str, Any]:
    """启动批量人岗分析任务。"""
    task = analyze_tasks.start(
        resume_id=body.resume_id,
        job_ids=body.job_ids,
        top_k=body.top_k,
    )
    return task.snapshot()


@router.get("/analyze/status")
def analyze_status() -> dict[str, Any]:
    """当前分析任务快照。"""
    return analyze_tasks.snapshot()


@router.get("/analyze/{task_id}")
def analyze_task(task_id: str) -> dict[str, Any]:
    """按 id 取分析任务快照。"""
    return analyze_tasks.snapshot(task_id)


@router.post("/analyze/{task_id}/cancel")
def cancel_analyze(task_id: str) -> dict[str, Any]:
    """取消分析任务。"""
    task = analyze_tasks.cancel(task_id)
    return task.snapshot()


@router.get("/analyses")
def analyses() -> dict[str, Any]:
    """历史分析列表。"""
    return {"items": list_analyses()}


@router.get("/analyses/{analysis_id}")
def get_analysis(analysis_id: str) -> dict[str, Any]:
    """按 id 取历史分析。"""
    try:
        return load_analysis(analysis_id)
    except FileNotFoundError as exc:
        raise NotFoundError(f"分析结果不存在：{analysis_id}") from exc


@router.delete("/analyses/{analysis_id}")
def remove_analysis(analysis_id: str) -> dict[str, Any]:
    """删除一条历史分析。"""
    if not delete_analysis(analysis_id):
        raise NotFoundError(f"分析结果不存在：{analysis_id}")
    return {"deleted": True}


@router.post("/revise/start")
def revise_start(body: ReviseStartBody) -> dict[str, Any]:
    """启动一次简历优化（定向模式）。"""
    jobs, target_kind, target = _pick_target_jobs(
        body.job_ids, body.crawl_task_id, all_repo=body.all_repo
    )

    try:
        resume = load_resume(body.resume_id)
    except FileNotFoundError as exc:
        raise NotFoundError(f"简历不存在：{body.resume_id}") from exc

    llm_cfg = load_config()
    if not llm_cfg.configured:
        raise ValidationWebError(
            "LLM 还没配置，请先到「模型」页填好 API Key / Base URL / 模型名"
        )

    task = revision_tasks.start(
        resume_id=body.resume_id,
        resume_title=resume.title,
        jobs=jobs,
        target_kind=target_kind,
        target_json=target,
        crawl_task_id=body.crawl_task_id,
        mode="targeted",
    )
    return task.snapshot()


@router.post("/revise/free-start")
def revise_free_start(body: ReviseFreeStartBody) -> dict[str, Any]:
    """启动一次简历优化（自由模式）。"""
    try:
        resume = load_resume(body.resume_id)
    except FileNotFoundError as exc:
        raise NotFoundError(f"简历不存在：{body.resume_id}") from exc

    llm_cfg = load_config()
    if not llm_cfg.configured:
        raise ValidationWebError(
            "LLM 还没配置，请先到「模型」页填好 API Key / Base URL / 模型名"
        )

    if not body.optimization_prompt.strip():
        raise ValidationWebError("优化提示词不能为空")

    task = revision_tasks.start(
        resume_id=body.resume_id,
        resume_title=resume.title,
        jobs=[],
        target_kind="free",
        target_json={},
        crawl_task_id="",
        mode="free",
        selected_sections=body.selected_sections,
        optimization_prompt=body.optimization_prompt.strip(),
    )
    return task.snapshot()


@router.get("/item/{resume_id}/sections")
def get_resume_sections(resume_id: str) -> dict[str, Any]:
    """获取简历的章节列表（用于自由优化模式的章节选择）。"""
    try:
        resume = load_resume(resume_id)
    except FileNotFoundError as exc:
        raise NotFoundError(f"简历不存在：{resume_id}") from exc

    sections = extract_sections(resume.raw or "")
    return {"resume_id": resume_id, "sections": sections}


@router.get("/revise/presets")
def get_preset_prompts() -> dict[str, Any]:
    """获取预设提示词库（自由优化模式）。"""
    categories = get_preset_categories()
    presets = {cat: get_presets_by_category(cat) for cat in categories}
    return {"categories": categories, "presets": presets}


@router.get("/revise/status")
def revise_status() -> dict[str, Any]:
    """当前简历优化任务快照；空闲时为 idle。"""
    return revision_tasks.snapshot()


@router.get("/revise/{task_id}")
def revise_task(task_id: str) -> dict[str, Any]:
    """按 id 取简历优化任务快照。"""
    return revision_tasks.snapshot(task_id)


@router.post("/revise/{task_id}/cancel")
def cancel_revise(task_id: str) -> dict[str, Any]:
    """停止当前简历优化任务。"""
    task = revision_tasks.cancel(task_id)
    return task.snapshot()


@router.get("/revisions")
def revisions(
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """简历优化记录列表（分页摘要）。"""
    items, total = list_revisions(limit=limit, offset=offset)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/revisions/{revision_id}")
def revision_detail(revision_id: str) -> dict[str, Any]:
    """简历优化详情：原文 / 修改稿 / changes / diff 结构。"""
    try:
        data = load_revision(revision_id)
    except FileNotFoundError as exc:
        raise NotFoundError(f"简历优化记录不存在：{revision_id}") from exc
    data["diff"] = build_diff(data.get("original_md") or "", data.get("revised_md") or "")
    return data


@router.post("/revisions/{revision_id}/save")
def revision_save(revision_id: str, body: ReviseSaveBody) -> dict[str, Any]:
    """确认保存：写盘 txt/md + 入库新简历（原简历不覆盖）。

    已保存再调 → 409。写盘失败 → 409 且不入库。
    """
    try:
        rev = load_revision(revision_id)
    except FileNotFoundError as exc:
        raise NotFoundError(f"简历优化记录不存在：{revision_id}") from exc
    if rev.get("status") == REVISION_SAVED:
        raise HTTPException(status_code=409, detail="已保存过")
    if rev.get("status") not in ("draft", "done", "error"):
        raise HTTPException(status_code=409, detail="该记录当前不可保存")

    revised_md = rev.get("revised_md") or ""
    if not revised_md:
        raise ValidationWebError("还没有可保存的修改稿")

    fmt = body.format or rev.get("export_format") or "md"
    suffix = ".md" if fmt == "md" else ".txt"
    import datetime as _dt

    timestamp = _dt.datetime.now().strftime("%Y%m%d-%H%M")
    base_title = body.filename.strip() or rev.get("resume_title") or "resume"
    path = revision_path(base_title, suffix, stamp=timestamp)

    try:
        content = revised_md if fmt == "md" else markdown_to_text(revised_md)
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise HTTPException(
            status_code=409, detail=f"文件写入失败：{exc}（草稿仍在，可换路径重试）"
        ) from exc

    new_title = body.title.strip() or f"{rev.get('resume_title') or '简历'}-定向-{timestamp[-4:]}"
    draft = save_resume(
        revised_md,
        filename=f"{new_title}.md" if fmt == "md" else f"{new_title}.txt",
    )
    mark_revision_saved(
        revision_id,
        saved_path=str(path),
        saved_resume_id=draft.resume_id,
        export_format=fmt,
    )
    return {
        "ok": True,
        "saved_path": str(path),
        "saved_resume_id": draft.resume_id,
        "title": new_title,
    }


@router.post("/revisions/{revision_id}/regenerate")
def revision_regenerate(revision_id: str) -> dict[str, Any]:
    """原地重新生成（覆盖草稿，回到生成任务）；返回任务快照。

    若已有任务在跑则 409（同一时刻只允许一个生成任务）。
    """
    try:
        rev = load_revision(revision_id)
    except FileNotFoundError as exc:
        raise NotFoundError(f"简历优化记录不存在：{revision_id}") from exc
    if rev.get("status") == REVISION_SAVED:
        raise HTTPException(status_code=409, detail="已保存过的记录不可再生成")

    target = dict(rev.get("target") or {})
    crawl_task_id = rev.get("crawl_task_id") or ""
    job_ids = [str(i) for i in (target.get("job_ids") or []) if i]

    from boss_jobs.store import JobStore

    with JobStore() as store:
        jobs = []
        for jid in job_ids:
            job = store.get_job(jid)
            if job is not None:
                jobs.append(job)
    if not jobs:
        raise ValidationWebError("目标岗位已不在库中，无法重新生成")

    llm_cfg = load_config()
    if not llm_cfg.configured:
        raise ValidationWebError(
            "LLM 还没配置，请先到「模型」页填好 API Key / Base URL / 模型名"
        )

    task = revision_tasks.start(
        resume_id=rev.get("resume_id") or "",
        resume_title=rev.get("resume_title") or "",
        jobs=jobs,
        target_kind=rev.get("target_kind") or "jobs",
        target_json=target,
        crawl_task_id=crawl_task_id,
        revision_id=revision_id,
    )
    return task.snapshot()


@router.delete("/revisions/{revision_id}")
def revision_delete(revision_id: str) -> dict[str, Any]:
    """删一条简历优化记录（只删库内记录，不删已写盘文件）。"""
    if not delete_revision(revision_id):
        raise NotFoundError(f"简历优化记录不存在：{revision_id}")
    return {"deleted": True}


@router.get("/revisions/{revision_id}/download")
def revision_download(
    revision_id: str,
    format: str = Query("md", pattern="^(md|txt)$"),
) -> Any:
    """下载 txt/md；文件缺失时从 `revised_md` 现场重生成。"""
    from fastapi.responses import PlainTextResponse

    try:
        rev = load_revision(revision_id)
    except FileNotFoundError as exc:
        raise NotFoundError(f"简历优化记录不存在：{revision_id}") from exc
    md = rev.get("revised_md") or ""
    if not md:
        raise ValidationWebError("该记录没有修改稿可下载")

    content = md if format == "md" else markdown_to_text(md)
    base = rev.get("resume_title") or "resume"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", base)[:60] or "resume"
    ext = ".md" if format == "md" else ".txt"
    return PlainTextResponse(
        content,
        headers={
            "Content-Disposition": (
                f'attachment; filename="{safe}{ext}"; '
                f"filename*=UTF-8''{urllib.parse.quote(base)}{ext}"
            ),
            "Content-Type": (
                "text/markdown; charset=utf-8"
                if format == "md"
                else "text/plain; charset=utf-8"
            ),
        },
    )
