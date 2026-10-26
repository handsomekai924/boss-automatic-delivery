"""面试题相关 API。

按文档 `docs/interview-question-generation.md` 实现：
- POST   /api/interview/generate              生成面试题（整份分析 / 指定岗位 / 批量岗位）
- GET    /api/interview/tasks                 所有生成任务（数量 + 进度，页面顶部用）
- GET    /api/interview/task/current          当前任务状态
- GET    /api/interview/task/{task_id}         查询生成任务状态
- POST   /api/interview/task/{task_id}/cancel  取消生成任务
- GET    /api/interview/jobs                  出题页岗位列表（抓取记录筛选 + 职位名模糊匹配）
- GET    /api/interview/records               生成记录列表（一岗一套题）
- GET    /api/interview/context               按岗位找分析与出题状态（jobs 卡片入口用）
- GET    /api/interview/summary               一批岗位的出题状态（卡片徽章用）
- GET    /api/interview/list                  获取面试题列表（支持筛选 / 搜索 / 排序）
- PATCH  /api/interview/{question_id}         手动编辑题目
- GET    /api/interview/export                导出面试题（Markdown / JSON）
"""

from __future__ import annotations

import json
import time
import urllib.parse
from typing import Any

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, Field

import boss_db
from ..errors import NotFoundError, ValidationWebError
from ..services.interview_generation_task import interview_tasks

router = APIRouter()


class GenerateRequest(BaseModel):
    """生成面试题请求。

    Attributes:
        analysis_id: 分析 ID；空则按岗位各自找最近分析（都没有就裸按 JD 出题）。
        encrypt_job_id: 只生成这个岗位；空 = 分析里全部已匹配岗位。
        encrypt_job_ids: 批量岗位（与 encrypt_job_id 可同时给，自动去重）。
        resume_id: 没有分析时用哪份简历；不传则用库里最新一份。
        question_count: 题量。整套出题缺省按匹配度定（10-15）；
            补题时是用户填的条数（1-10，`SUPPLEMENT_MAX`）。
        question_type: 补题题型（技术深度/项目经历/场景/软技能）；
            非空 = 补题，只补这一型、不覆盖旧题、避开已有题目。
    """

    analysis_id: str = ""
    encrypt_job_id: str = ""
    encrypt_job_ids: list[str] = Field(default_factory=list)
    resume_id: str = ""
    question_count: int | None = Field(default=None, ge=1, le=30)
    question_type: str = ""


class UpdateQuestionRequest(BaseModel):
    """手动编辑单条面试题。

    Attributes:
        question_text: 题目正文；不传则不动。
        answer_hints: 答题思路 / 知识点 / 参考答案；不传则不动。
    """

    question_text: str | None = Field(default=None, max_length=500)
    answer_hints: dict[str, Any] | None = None


def _row_to_item(row: Any) -> dict[str, Any]:
    """一行 `interview_questions` → 出参条目。"""
    try:
        hints = json.loads(str(row["answer_hints"] or "{}"))
    except ValueError:
        hints = {}
    if not isinstance(hints, dict):
        hints = {}
    # 旧数据字段名兼容：reference_outline → reference_answer。
    if "reference_answer" not in hints and "reference_outline" in hints:
        hints = {**hints, "reference_answer": hints.get("reference_outline")}
        hints.pop("reference_outline", None)
    return {
        "id": int(row["id"]),
        "analysis_id": str(row["analysis_id"]),
        "site": str(row["site"] or "boss"),
        "encrypt_job_id": str(row["encrypt_job_id"]),
        "question_text": str(row["question_text"]),
        "question_type": str(row["question_type"]),
        "difficulty": str(row["difficulty"]),
        "is_frequent": bool(row["is_frequent"]),
        "answer_hints": hints,
        "created_at": float(row["created_at"] or 0),
        "updated_at": float(row["updated_at"] or 0),
    }


@router.post("/generate")
def generate(req: GenerateRequest) -> dict[str, Any]:
    """生成面试题（异步任务）。

    给了 `analysis_id` 就整批走那份分析；否则按岗位各自找最近分析，
    都没有就用 `resume_id`（或最新简历）裸按 JD 出题。
    给了 `question_type` 走补题：只补这一型、追加到已有题后、避开重复。

    Args:
        req: 生成请求。

    Returns:
        任务 ID 与快照。
    """
    job_ids: list[str] = []
    for jid in (req.encrypt_job_id, *(req.encrypt_job_ids or [])):
        j = str(jid or "").strip()
        if j and j not in job_ids:
            job_ids.append(j)
    if req.question_type:
        from ..services.interview_generator import SUPPLEMENT_MAX, QUESTION_TYPES

        if req.question_type not in QUESTION_TYPES:
            raise ValidationWebError(
                f"题型只能是：{' / '.join(QUESTION_TYPES)}"
            )
        if req.question_count is not None and req.question_count > SUPPLEMENT_MAX:
            raise ValidationWebError(f"补题单次最多 {SUPPLEMENT_MAX} 条")
    task = interview_tasks.start(
        analysis_id=req.analysis_id,
        job_ids=job_ids or None,
        resume_id=req.resume_id,
        question_count=req.question_count,
        question_type=req.question_type.strip(),
    )
    return task.snapshot()


@router.get("/tasks")
def task_list() -> dict[str, Any]:
    """所有生成任务：数量 + 各自进度（任务中心弹窗实时展示用）。"""
    return interview_tasks.list_snapshots()


@router.post("/tasks/cancel")
def task_cancel_all() -> dict[str, Any]:
    """取消所有在跑的生成任务。"""
    task = interview_tasks.cancel(None)
    return {"ok": True, "cancelled": task.task_id, **interview_tasks.list_snapshots()}


@router.delete("/task/{task_id}")
def task_delete(task_id: str) -> dict[str, Any]:
    """删掉一条任务记录（失败任务的「删除」；在跑的要先停）。"""
    task = interview_tasks.delete(task_id)
    return {"ok": True, "deleted": task.task_id}


@router.post("/task/{task_id}/retry")
def task_retry(task_id: str) -> dict[str, Any]:
    """按原参数重跑一条失败任务。"""
    task = interview_tasks.retry(task_id)
    return task.snapshot()


@router.get("/task/current")
def task_current() -> dict[str, Any]:
    """最近一个生成任务状态（空闲回 idle）。"""
    return interview_tasks.snapshot()


@router.get("/task/{task_id}")
def task_status(task_id: str) -> dict[str, Any]:
    """获取生成任务状态。"""
    return interview_tasks.snapshot(task_id)


@router.post("/task/{task_id}/cancel")
def task_cancel(task_id: str) -> dict[str, Any]:
    """取消生成任务。"""
    task = interview_tasks.cancel(task_id)
    return task.snapshot()


def _question_count_map(job_ids: list[str]) -> dict[str, int]:
    """一批职位 ID → 题数。"""
    if not job_ids:
        return {}
    marks = ",".join("?" for _ in job_ids)
    rows = boss_db.acquire().execute(
        f"SELECT encrypt_job_id, COUNT(*) AS n "
        f"FROM interview_questions WHERE encrypt_job_id IN ({marks}) "
        f"GROUP BY encrypt_job_id",
        job_ids,
    ).fetchall()
    return {str(r["encrypt_job_id"]): int(r["n"] or 0) for r in rows}


@router.get("/jobs")
def list_generation_jobs(
    crawl_task_id: str = Query(""),
    title: str = Query(""),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """出题页岗位列表：按抓取记录筛选 + 职位名模糊匹配。

    Args:
        crawl_task_id: 只看这次抓取里的岗位；空 = 全部岗位。
        title: 职位名称模糊匹配。
        limit: 每页条数。
        offset: 偏移。

    Returns:
        `items`（含出题状态）+ `total` + 分页信息。
    """
    where: list[str] = ["1=1"]
    args: list[Any] = []
    if crawl_task_id.strip():
        where.append(
            "j.encrypt_job_id IN (SELECT encrypt_job_id FROM crawl_task_jobs WHERE task_id = ?)"
        )
        args.append(crawl_task_id.strip())
    if title.strip():
        where.append("j.job_name LIKE ?")
        args.append(f"%{title.strip()}%")
    where_sql = " AND ".join(where)

    conn = boss_db.acquire()
    total = int(
        conn.execute(
            f"SELECT COUNT(*) AS n FROM jobs j WHERE {where_sql}", args
        ).fetchone()["n"]
        or 0
    )
    rows = conn.execute(
        f"SELECT j.encrypt_job_id, j.job_name, j.brand_name, j.location, j.salary_desc, "
        f"j.job_experience, j.job_degree, j.jd_status, j.fetched_at "
        f"FROM jobs j WHERE {where_sql} "
        f"ORDER BY j.fetched_at DESC, j.encrypt_job_id LIMIT ? OFFSET ?",
        [*args, int(limit), int(offset)],
    ).fetchall()
    counts = _question_count_map([str(r["encrypt_job_id"]) for r in rows])
    items = []
    for r in rows:
        jid = str(r["encrypt_job_id"])
        c = counts.get(jid, 0)
        items.append(
            {
                "encrypt_job_id": jid,
                "job_name": str(r["job_name"] or ""),
                "brand_name": str(r["brand_name"] or ""),
                "location": str(r["location"] or ""),
                "salary_desc": str(r["salary_desc"] or ""),
                "job_experience": str(r["job_experience"] or ""),
                "job_degree": str(r["job_degree"] or ""),
                "jd_status": str(r["jd_status"] or ""),
                "fetched_at": str(r["fetched_at"] or ""),
                "question_count": c,
            }
        )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/records")
def list_generation_records(
    title: str = Query(""),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """生成记录：一岗一套题，带题数 / 生成与更新时间。

    Args:
        title: 职位名称模糊匹配。
        limit: 每页条数。
        offset: 偏移。

    Returns:
        `items` + `total` + 分页信息。
    """
    where: list[str] = ["1=1"]
    args: list[Any] = []
    if title.strip():
        where.append("j.job_name LIKE ?")
        args.append(f"%{title.strip()}%")
    where_sql = " AND ".join(where)

    conn = boss_db.acquire()
    total = int(
        conn.execute(
            f"SELECT COUNT(*) AS n FROM (SELECT q.encrypt_job_id "
            f"FROM interview_questions q LEFT JOIN jobs j ON j.encrypt_job_id = q.encrypt_job_id "
            f"WHERE {where_sql} GROUP BY q.encrypt_job_id)",
            args,
        ).fetchone()["n"]
        or 0
    )
    rows = conn.execute(
        f"SELECT q.encrypt_job_id, MAX(q.analysis_id) AS analysis_id, "
        f"COALESCE(MAX(j.job_name), '') AS job_name, "
        f"COALESCE(MAX(j.brand_name), '') AS brand_name, "
        f"COUNT(*) AS question_count, "
        f"MIN(q.created_at) AS created_at, MAX(q.updated_at) AS updated_at "
        f"FROM interview_questions q "
        f"LEFT JOIN jobs j ON j.encrypt_job_id = q.encrypt_job_id "
        f"WHERE {where_sql} GROUP BY q.encrypt_job_id "
        f"ORDER BY MAX(q.updated_at) DESC LIMIT ? OFFSET ?",
        [*args, int(limit), int(offset)],
    ).fetchall()
    items = [
        {
            "encrypt_job_id": str(r["encrypt_job_id"]),
            "analysis_id": str(r["analysis_id"] or ""),
            "job_name": str(r["job_name"] or ""),
            "brand_name": str(r["brand_name"] or ""),
            "question_count": int(r["question_count"] or 0),
            "created_at": float(r["created_at"] or 0),
            "updated_at": float(r["updated_at"] or 0),
        }
        for r in rows
    ]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/context")
def context(encrypt_job_id: str = Query("")) -> dict[str, Any]:
    """按岗位找最近一条含它的分析，回出题状态（jobs 卡片入口用）。

    Args:
        encrypt_job_id: 职位 ID。

    Returns:
        没有分析时 `analysis_id` 为空串。
    """
    if not encrypt_job_id.strip():
        raise ValidationWebError("缺少 encrypt_job_id")
    conn = boss_db.acquire()
    rows = conn.execute(
        "SELECT analysis_id, payload FROM analysis ORDER BY created_at DESC"
    ).fetchall()
    analysis_id = ""
    match_item: dict[str, Any] = {}
    for row in rows:
        try:
            payload = json.loads(str(row["payload"]))
        except ValueError:
            continue
        for m in payload.get("matches") or []:
            if isinstance(m, dict) and str(m.get("encrypt_job_id") or "") == encrypt_job_id:
                analysis_id = str(row["analysis_id"])
                match_item = m
                break
        if analysis_id:
            break

    count_row = conn.execute(
        "SELECT COUNT(*) AS n FROM interview_questions WHERE encrypt_job_id = ?",
        (encrypt_job_id,),
    ).fetchone()
    running = interview_tasks.snapshot()
    return {
        "encrypt_job_id": encrypt_job_id,
        "analysis_id": analysis_id,
        "job_name": str(match_item.get("job_name") or ""),
        "brand_name": str(match_item.get("brand_name") or ""),
        "match_score": match_item.get("match_score") or 0,
        "question_count": int(count_row["n"] or 0),
        "task": running,
    }


@router.get("/summary")
def summary(encrypt_job_ids: str = Query("")) -> dict[str, Any]:
    """一批岗位的出题状态（jobs 卡片徽章用）。

    Args:
        encrypt_job_ids: 逗号分隔的职位 ID。

    Returns:
        `items`：职位 ID → {question_count}。
    """
    ids = [s.strip() for s in encrypt_job_ids.split(",") if s.strip()]
    if not ids:
        return {"items": {}}
    conn = boss_db.acquire()
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT encrypt_job_id, COUNT(*) AS n "
        f"FROM interview_questions WHERE encrypt_job_id IN ({marks}) GROUP BY encrypt_job_id",
        ids,
    ).fetchall()
    items = {str(r["encrypt_job_id"]): {"question_count": int(r["n"] or 0)} for r in rows}
    for jid in ids:
        items.setdefault(jid, {"question_count": 0})
    return {"items": items}


@router.get("/list")
def list_questions(
    analysis_id: str = Query(""),
    encrypt_job_id: str = Query(""),
    question_type: str = Query(""),
    difficulty: str = Query(""),
    q: str = Query(""),
    sort: str = Query("created", pattern="^(created|difficulty|frequent)$"),
) -> dict[str, Any]:
    """获取面试题列表。

    Args:
        analysis_id: 分析 ID（与 encrypt_job_id 至少给一个）。
        encrypt_job_id: 职位 ID 筛选。
        question_type: 题型筛选（技术深度/项目经历/场景/软技能）。
        difficulty: 难度筛选（基础/中级/高级）。
        q: 题目内容关键词。
        sort: 排序（created/difficulty/frequent）。

    Returns:
        题目列表与统计。
    """
    if not analysis_id.strip() and not encrypt_job_id.strip():
        raise ValidationWebError("analysis_id 和 encrypt_job_id 至少给一个")

    where: list[str] = []
    args: list[Any] = []
    if analysis_id.strip():
        where.append("analysis_id = ?")
        args.append(analysis_id.strip())
    if encrypt_job_id.strip():
        where.append("encrypt_job_id = ?")
        args.append(encrypt_job_id.strip())
    if question_type.strip():
        where.append("question_type = ?")
        args.append(question_type.strip())
    if difficulty.strip():
        where.append("difficulty = ?")
        args.append(difficulty.strip())
    if q.strip():
        where.append("question_text LIKE ?")
        args.append(f"%{q.strip()}%")

    order = {
        "created": "created_at DESC",
        "difficulty": "CASE difficulty WHEN '高级' THEN 3 WHEN '中级' THEN 2 ELSE 1 END DESC, created_at DESC",
        "frequent": "is_frequent DESC, created_at DESC",
    }[sort]

    conn = boss_db.acquire()
    rows = conn.execute(
        f"SELECT * FROM interview_questions WHERE {' AND '.join(where)} ORDER BY {order}",
        args,
    ).fetchall()
    items = [_row_to_item(r) for r in rows]

    # 统计口径不带题目筛选条件，只看归属范围——题量按整个岗位/分析算。
    stat_where: list[str] = []
    stat_args: list[Any] = []
    if analysis_id.strip():
        stat_where.append("analysis_id = ?")
        stat_args.append(analysis_id.strip())
    if encrypt_job_id.strip():
        stat_where.append("encrypt_job_id = ?")
        stat_args.append(encrypt_job_id.strip())
    stat_row = conn.execute(
        f"SELECT COUNT(*) AS n FROM interview_questions "
        f"WHERE {' AND '.join(stat_where)}",
        stat_args,
    ).fetchone()
    return {
        "questions": items,
        "total": len(items),
        "stats": {
            "question_count": int(stat_row["n"] or 0),
        },
    }


@router.patch("/{question_id}")
def update_question(question_id: int, req: UpdateQuestionRequest) -> dict[str, Any]:
    """手动编辑题目。

    Args:
        question_id: 题目 ID。
        req: 更新内容。

    Returns:
        改后的题目。
    """
    conn = boss_db.acquire()
    row = conn.execute(
        "SELECT * FROM interview_questions WHERE id = ?", (question_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"题目 {question_id} 不存在")

    sets: list[str] = []
    args: list[Any] = []
    if req.question_text is not None:
        text = req.question_text.strip()
        if not text:
            raise ValidationWebError("题目内容不能为空")
        sets.append("question_text = ?")
        args.append(text)
    if req.answer_hints is not None:
        sets.append("answer_hints = ?")
        args.append(json.dumps(req.answer_hints, ensure_ascii=False))
    if not sets:
        raise ValidationWebError("没有要更新的字段")

    sets.append("updated_at = ?")
    args.append(time.time())
    args.append(question_id)
    with conn:
        conn.execute(
            f"UPDATE interview_questions SET {', '.join(sets)} WHERE id = ?", args
        )
    row = conn.execute(
        "SELECT * FROM interview_questions WHERE id = ?", (question_id,)
    ).fetchone()
    return {"ok": True, "item": _row_to_item(row)}


def _load_for_export(
    analysis_id: str, encrypt_job_id: str
) -> list[dict[str, Any]]:
    """读导出用的题目（按岗位、难度聚组）。"""
    where: list[str] = []
    args: list[Any] = []
    if analysis_id.strip():
        where.append("analysis_id = ?")
        args.append(analysis_id.strip())
    if encrypt_job_id.strip():
        where.append("encrypt_job_id = ?")
        args.append(encrypt_job_id.strip())
    if not where:
        raise ValidationWebError("analysis_id 和 encrypt_job_id 至少给一个")
    conn = boss_db.acquire()
    rows = conn.execute(
        f"SELECT * FROM interview_questions WHERE {' AND '.join(where)} "
        "ORDER BY encrypt_job_id, CASE difficulty WHEN '高级' THEN 3 "
        "WHEN '中级' THEN 2 ELSE 1 END DESC, created_at",
        args,
    ).fetchall()
    return [_row_to_item(r) for r in rows]


def _job_title_map(job_ids: list[str]) -> dict[str, str]:
    """职位 ID → 「职位名 · 公司」（导出分节标题用）。"""
    if not job_ids:
        return {}
    conn = boss_db.acquire()
    marks = ",".join("?" for _ in job_ids)
    rows = conn.execute(
        f"SELECT encrypt_job_id, job_name, brand_name FROM jobs WHERE encrypt_job_id IN ({marks})",
        job_ids,
    ).fetchall()
    out = {str(r["encrypt_job_id"]): f"{r['job_name']} · {r['brand_name']}" for r in rows}
    for jid in job_ids:
        out.setdefault(jid, jid)
    return out


def _markdown(items: list[dict[str, Any]], *, title: str) -> str:
    """拼 Markdown 导出稿。"""
    lines: list[str] = [f"# {title}", ""]
    by_job: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        by_job.setdefault(it["encrypt_job_id"], []).append(it)
    titles = _job_title_map(list(by_job))

    for job_id, rows in by_job.items():
        if len(by_job) > 1:
            lines.append(f"## {titles.get(job_id, job_id)}")
            lines.append("")
        for it in rows:
            badge = " ⭐ 高频" if it["is_frequent"] else ""
            lines.append(f"### {it['question_text']}")
            lines.append("")
            lines.append(
                f"`{it['question_type']}` · `{it['difficulty']}`{badge}"
            )
            lines.append("")
            hints = it.get("answer_hints") or {}
            approach = str(hints.get("approach") or "").strip()
            if approach:
                lines.append(f"**答题思路**：{approach}")
                lines.append("")
            points = hints.get("key_points") or []
            if points:
                lines.append("**关键知识点**：")
                for p in points:
                    lines.append(f"- {p}")
                lines.append("")
            answer = str(hints.get("reference_answer") or "").strip()
            if answer:
                lines.append(f"**参考答案**：{answer}")
                lines.append("")
            lines.append("---")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


@router.get("/export")
def export_questions(
    analysis_id: str = Query(""),
    encrypt_job_id: str = Query(""),
    format: str = Query("markdown", pattern="^(markdown|json)$"),
) -> Response:
    """导出面试题（markdown / json 文件下载）。"""
    items = _load_for_export(analysis_id, encrypt_job_id)
    if not items:
        raise NotFoundError("未找到面试题")

    if format == "json":
        content = json.dumps(items, ensure_ascii=False, indent=2)
        media = "application/json; charset=utf-8"
        filename = "interview_questions.json"
    else:
        scope = encrypt_job_id.strip() or analysis_id.strip()
        content = _markdown(items, title=f"面试准备题目 · {scope}")
        media = "text/markdown; charset=utf-8"
        filename = "interview_questions.md"

    return Response(
        content=content,
        media_type=media,
        headers={
            "Content-Disposition": (
                f'attachment; filename="{filename}"; '
                f"filename*=UTF-8''{urllib.parse.quote(filename)}"
            )
        },
    )
