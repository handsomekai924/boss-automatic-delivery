"""简历 vs 职位的匹配分析：组装 prompt、串行调 LLM、聚合结果落盘。"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import Counter, deque
from typing import Any, Sequence

from boss_jobs.models import Job

from .. import config as C
from ..errors import ConflictError, NotFoundError, ValidationWebError
from .llm_client import LLMClient, extract_json
from .llm_config_store import load_config
from .resume_store import ResumeDraft, load_resume, save_analysis

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "你是资深互联网猎头，擅长人岗匹配。"
    "只输出一个 JSON 对象，不要任何解释文字或代码围栏。"
)

USER_TEMPLATE = """# 简历摘要
- 目标岗位：{intent}
- 技能标签：{skills}
- 自我评价/亮点：{summary}
{work_brief}

# 目标职位
- 岗位：{job_name}
- 公司：{brand_name}（{brand_industry} / {brand_scale_name}）
- 地点：{location}
- 薪资：{salary_desc}
- 经验/学历：{job_experience} / {job_degree}
- 标签：{job_labels}

请评估人岗匹配度，输出 JSON：
{{
  "match_score": 0-100 的整数,
  "matched_skills": ["简历与职位都有的技能"],
  "missing_skills": ["职位要但简历没有的技能"],
  "verdict": "一句话结论",
  "reasons": ["两三条理由"],
  "advice": "给求职者的改进建议",
  "greeting": "给招聘方的打招呼语，60 字以内，自然具体不吹牛"
}}
"""

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ERROR = "error"
STATUS_CANCELLED = "cancelled"


class AnalyzeTask:
    def __init__(self, resume_id: str, job_ids: Sequence[str], top_k: int) -> None:
        self.task_id = "an_" + uuid.uuid4().hex[:10]
        self.resume_id = resume_id
        self.job_ids = list(job_ids)
        self.top_k = top_k
        self.status = STATUS_RUNNING
        self.created_at = time.time()
        self.ended_at: float | None = None
        self.error: str | None = None
        self.done = 0
        self.total = 0
        self.current_job = ""
        self.matches: list[dict[str, Any]] = []
        self.events: deque[dict[str, Any]] = deque(maxlen=100)
        self.cancel_flag = False
        self.lock = threading.Lock()
        self.analysis_id = self.task_id

    def push(self, name: str, payload: dict[str, Any] | None = None) -> None:
        with self.lock:
            self.events.append({"at": time.time(), "name": name, "payload": payload or {}})

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "task_id": self.task_id,
                "analysis_id": self.analysis_id,
                "status": self.status,
                "resume_id": self.resume_id,
                "created_at": self.created_at,
                "ended_at": self.ended_at,
                "error": self.error,
                "done": self.done,
                "total": self.total,
                "current_job": self.current_job,
                "matches": list(self.matches),
                "events": list(self.events)[-20:],
            }


class AnalyzeTaskManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._task: AnalyzeTask | None = None

    def current(self) -> AnalyzeTask | None:
        with self._lock:
            return self._task

    def snapshot(self, task_id: str | None = None) -> dict[str, Any]:
        task = self.current()
        if task is None:
            return {"task_id": None, "status": "idle"}
        if task_id and task.task_id != task_id:
            raise NotFoundError(f"分析任务不存在：{task_id}")
        return task.snapshot()

    def start(self, *, resume_id: str, job_ids: Sequence[str] | None = None, top_k: int | None = None) -> AnalyzeTask:
        with self._lock:
            if self._task is not None and self._task.status == STATUS_RUNNING:
                raise ConflictError("已有分析任务在跑")
            task = AnalyzeTask(resume_id, job_ids or [], top_k or C.DEFAULT_ANALYZE_TOP_K)
            self._task = task

        thread = threading.Thread(target=self._run, args=(task,), daemon=True, name=task.task_id)
        thread.start()
        return task

    def cancel(self, task_id: str | None = None) -> AnalyzeTask:
        task = self.current()
        if task is None or (task_id and task.task_id != task_id):
            raise NotFoundError("没有在跑的分析任务")
        task.cancel_flag = True
        return task

    # ------------------------------------------------------------------ #

    def _run(self, task: AnalyzeTask) -> None:
        try:
            resume = load_resume(task.resume_id)
        except FileNotFoundError as exc:
            task.status = STATUS_ERROR
            task.error = f"简历不存在：{task.resume_id}"
            task.ended_at = time.time()
            return

        cfg = load_config()
        if not cfg.configured:
            task.status = STATUS_ERROR
            task.error = "LLM 还没配置，请先到「模型」页填好 API Key / Base URL / 模型名"
            task.ended_at = time.time()
            return

        jobs = _pick_jobs(task)
        task.total = len(jobs)
        if not jobs:
            task.status = STATUS_ERROR
            task.error = "没有可分析的职位，请先抓取或指定 job_ids"
            task.ended_at = time.time()
            return

        llm = LLMClient(cfg)
        for job in jobs:
            if task.cancel_flag:
                task.status = STATUS_CANCELLED
                task.ended_at = time.time()
                return
            with task.lock:
                task.current_job = job.job_name
            task.push("job_start", {"job_name": job.job_name, "brand": job.brand_name})
            try:
                result = _analyze_one(llm, resume, job)
            except Exception as exc:  # noqa: BLE001 - 单个职位失败不拖垮整批
                logger.warning("分析职位 %s 失败：%s", job.job_name, exc)
                result = {
                    "match_score": 0,
                    "matched_skills": [],
                    "missing_skills": [],
                    "verdict": "分析失败",
                    "reasons": [str(exc)],
                    "advice": "",
                    "greeting": "",
                    "error": str(exc),
                }
            item = {
                "encrypt_job_id": job.encrypt_job_id,
                "job_name": job.job_name,
                "brand_name": job.brand_name,
                "location": job.location,
                "salary_desc": job.salary_desc,
                "job_experience": job.job_experience,
                "job_degree": job.job_degree,
                "brand_industry": job.brand_industry,
                "brand_scale_name": job.brand_scale_name,
                **result,
            }
            with task.lock:
                task.matches.append(item)
                task.done += 1
            task.push("job_done", {"job_name": job.job_name, "score": item.get("match_score")})
            time.sleep(C.ANALYZE_INTERVAL)

        _finalize(task, resume, cfg)


def _pick_jobs(task: AnalyzeTask) -> list[Job]:
    from boss_jobs.store import JobStore

    with JobStore() as store:
        if task.job_ids:
            jobs = []
            for jid in task.job_ids:
                job = store.get_job(jid)
                if job is not None:
                    jobs.append(job)
            return jobs
        return store.list_jobs(limit=task.top_k)


def _work_brief(resume: ResumeDraft) -> str:
    work = resume.sections.get("工作经历") or resume.sections.get("项目经历") or ""
    work = work.strip()
    if len(work) > 500:
        work = work[:500] + "…"
    return f"- 经历摘录：\n{work}" if work else ""


def _analyze_one(llm: LLMClient, resume: ResumeDraft, job: Job) -> dict[str, Any]:
    user = USER_TEMPLATE.format(
        intent=(resume.sections.get("求职意向") or "未填写").strip()[:120],
        skills="、".join(resume.skills[:20]) or "未填写",
        summary=(resume.summary or "未填写").strip()[:300],
        work_brief=_work_brief(resume),
        job_name=job.job_name,
        brand_name=job.brand_name,
        brand_industry=job.brand_industry or "-",
        brand_scale_name=job.brand_scale_name or "-",
        location=job.location or "-",
        salary_desc=job.salary_desc or "-",
        job_experience=job.job_experience or "-",
        job_degree=job.job_degree or "-",
        job_labels="、".join(job.job_labels[:12]) or "-",
    )
    raw = llm.chat(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]
    )
    data = extract_json(raw)
    if data is None:
        raw = llm.chat(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user + "\n\n请严格只输出 JSON 对象。"},
            ]
        )
        data = extract_json(raw)
    if data is None:
        raise ValueError("LLM 没回合法 JSON")

    return {
        "match_score": int(data.get("match_score") or 0),
        "matched_skills": [str(s) for s in (data.get("matched_skills") or [])][:12],
        "missing_skills": [str(s) for s in (data.get("missing_skills") or [])][:12],
        "verdict": str(data.get("verdict") or ""),
        "reasons": [str(r) for r in (data.get("reasons") or [])][:6],
        "advice": str(data.get("advice") or ""),
        "greeting": str(data.get("greeting") or ""),
    }


def _finalize(task: AnalyzeTask, resume: ResumeDraft, cfg: Any) -> None:
    gaps: Counter[str] = Counter()
    for item in task.matches:
        for skill in item.get("missing_skills") or []:
            gaps[str(skill)] += 1
    ranked = sorted(task.matches, key=lambda m: m.get("match_score") or 0, reverse=True)

    payload = {
        "analysis_id": task.analysis_id,
        "resume_id": resume.resume_id,
        "resume_title": resume.title,
        "created_at": task.created_at,
        "status": STATUS_DONE if task.status == STATUS_RUNNING else task.status,
        "llm": {"base_url": cfg.base_url, "model": cfg.model, "temperature": cfg.temperature},
        "resume_summary": {
            "skills": resume.skills[:20],
            "intent": (resume.sections.get("求职意向") or "")[:200],
            "summary": resume.summary,
        },
        "matches": ranked,
        "skill_gaps": [{"skill": k, "count": v} for k, v in gaps.most_common(15)],
        "top_recommendations": [
            m["encrypt_job_id"] for m in ranked if (m.get("match_score") or 0) >= 70
        ][:10],
        "errors": [m.get("error") for m in task.matches if m.get("error")],
    }
    save_analysis(payload)
    with task.lock:
        task.status = STATUS_DONE
        task.ended_at = time.time()
        task.matches = ranked
    task.push("finished", {"analysis_id": task.analysis_id})


analyze_tasks = AnalyzeTaskManager()
