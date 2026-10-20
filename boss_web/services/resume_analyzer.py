"""简历 vs 职位的匹配分析：组装 prompt、串行调 LLM、聚合结果落盘。

Attributes:
    SYSTEM_PROMPT: 人岗匹配的系统提示词（含招呼语人称约定）。
    USER_TEMPLATE: 单条职位的用户提示词模板。
    STATUS_RUNNING: 任务状态：进行中。
    STATUS_DONE: 任务状态：完成。
    STATUS_ERROR: 任务状态：失败。
    STATUS_CANCELLED: 任务状态：已取消。
"""

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
from .greeting_prompt import GREETING_REQUIREMENT
from .llm_client import LLMClient, extract_json
from .llm_config_store import load_config
from .resume_store import ResumeDraft, load_resume, save_analysis

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "你是资深互联网猎头，擅长人岗匹配。"
    "只输出一个 JSON 对象，不要任何解释文字或代码围栏。"
    "注意立场："
    "pros / cons / advice 是给求职者看的评估，"
    "而 greeting 是求职者本人发给招聘方 HR 的一条私信，"
    "一律用求职者第一人称「我」写，绝不能写成招聘方/HR 的口吻。"
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
  "pros": ["亮点1", "亮点2"],
  "cons": ["短板1", "短板2"],
  "reasons": ["两三条理由"],
  "advice": "给求职者的改进建议",
  "greeting": "{GREETING_REQUIREMENT}"
}}
""".replace("{GREETING_REQUIREMENT}", GREETING_REQUIREMENT)

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ERROR = "error"
STATUS_CANCELLED = "cancelled"


class AnalyzeTask:
    """内存里的分析任务状态与事件流水。

    Attributes:
        task_id: 任务 id。
        resume_id: 简历 id。
        job_ids: 指定职位 id；空则取库内前 top_k 条。
        top_k: 未指定 job_ids 时取几条。
        status: 任务状态。
        created_at: 创建时间戳。
        ended_at: 结束时间戳。
        error: 错误文案。
        done: 已分析条数。
        total: 本批总条数。
        current_job: 当前职位名。
        matches: 逐条匹配结果。
        events: 最近事件流水。
        cancel_flag: 取消标记。
        lock: 状态锁。
        analysis_id: 落盘用的分析 id（与 task_id 同值）。
    """

    def __init__(self, resume_id: str, job_ids: Sequence[str], top_k: int) -> None:
        """初始化。"""
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
        """记一条事件。

        Args:
            name: 事件名。
            payload: 事件载荷。
        """
        with self.lock:
            self.events.append({"at": time.time(), "name": name, "payload": payload or {}})

    def snapshot(self) -> dict[str, Any]:
        """当前状态副本。

        Returns:
            含进度、matches 与最近事件的 dict。
        """
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
    """同一时刻只跑一个分析任务。

    Attributes:
        _lock: 任务槽锁。
        _task: 当前任务。
    """

    def __init__(self) -> None:
        """初始化。"""
        self._lock = threading.Lock()
        self._task: AnalyzeTask | None = None

    def current(self) -> AnalyzeTask | None:
        """当前任务；没有则 `None`。

        Returns:
            当前任务或 `None`。
        """
        with self._lock:
            return self._task

    def snapshot(self, task_id: str | None = None) -> dict[str, Any]:
        """查询进度快照。

        Args:
            task_id: 可选，校验是否就是当前任务。

        Returns:
            任务快照；空闲时为 idle。

        Raises:
            NotFoundError: 指定 task_id 不是当前任务。
        """
        task = self.current()
        if task is None:
            return {"task_id": None, "status": "idle"}
        if task_id and task.task_id != task_id:
            raise NotFoundError(f"分析任务不存在：{task_id}")
        return task.snapshot()

    def start(self, *, resume_id: str, job_ids: Sequence[str] | None = None, top_k: int | None = None) -> AnalyzeTask:
        """启动一次分析并起后台线程。

        Args:
            resume_id: 简历 id。
            job_ids: 指定职位；空则取库内前 top_k 条。
            top_k: 取几条职位。

        Returns:
            新建的分析任务。

        Raises:
            ConflictError: 已有分析任务在跑。
        """
        with self._lock:
            if self._task is not None and self._task.status == STATUS_RUNNING:
                raise ConflictError("已有分析任务在跑")
            task = AnalyzeTask(resume_id, job_ids or [], top_k or C.DEFAULT_ANALYZE_TOP_K)
            self._task = task

        thread = threading.Thread(target=self._run, args=(task,), daemon=True, name=task.task_id)
        thread.start()
        return task

    def cancel(self, task_id: str | None = None) -> AnalyzeTask:
        """请求取消当前任务。

        Args:
            task_id: 可选，校验是否就是当前任务。

        Returns:
            被取消的任务。

        Raises:
            NotFoundError: 没有在跑的分析任务。
        """
        task = self.current()
        if task is None or (task_id and task.task_id != task_id):
            raise NotFoundError("没有在跑的分析任务")
        task.cancel_flag = True
        return task

    def _run(self, task: AnalyzeTask) -> None:
        """线程体：串行分析各职位并落盘。

        Args:
            task: 分析任务。
        """
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
                    "pros": [],
                    "cons": [],
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
                **_safety_snapshot(job),
                **result,
            }
            with task.lock:
                task.matches.append(item)
                task.done += 1
            task.push("job_done", {"job_name": job.job_name, "score": item.get("match_score")})
            time.sleep(C.ANALYZE_INTERVAL)

        _finalize(task, resume, cfg)


def _pick_jobs(task: AnalyzeTask) -> list[Job]:
    """按 task.job_ids 或 top_k 取本批职位。

    Args:
        task: 分析任务。

    Returns:
        职位列表。
    """
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
    """拼提示词里的经历摘录。

    Args:
        resume: 简历骨架。

    Returns:
        摘录行；没有经历则空串。
    """
    work = resume.sections.get("工作经历") or resume.sections.get("项目经历") or ""
    work = work.strip()
    if len(work) > 500:
        work = work[:500] + "…"
    return f"- 经历摘录：\n{work}" if work else ""


def _safety_snapshot(job: Job) -> dict[str, Any]:
    """把职位当前安全状态拷进 match（与 match_task 同款字段）。

    老的 `/api/resume/analyze` 路径仍然可达；没有这些字段的分析
    在发送侧会被当成「缺少匹配时安全快照」，自动投递一律拒绝。

    Args:
        job: 职位。

    Returns:
        安全快照字段。
    """
    return {
        "is_blacklisted": bool(getattr(job, "is_blacklisted", False)),
        "company_blacklisted": bool(getattr(job, "company_blacklisted", False)),
        "jd_blacklisted": bool(getattr(job, "jd_blacklisted", False)),
        "blacklist_types": list(getattr(job, "blacklist_types", ()) or ()),
        "matched_rule_ids": list(getattr(job, "matched_rule_ids", ()) or ()),
        "matched_rule_snapshot": [dict(rule) for rule in (getattr(job, "matched_rule_snapshot", ()) or ())],
        "blacklist_checked_at": getattr(job, "blacklist_checked_at", ""),
        "jd_status": getattr(job, "jd_status", "") or "pending",
        "jd_rule_checked": bool(getattr(job, "jd_rule_checked", False)),
        "jd_rule_checked_at": getattr(job, "jd_rule_checked_at", ""),
        "auto_delivery_blocked": bool(getattr(job, "auto_delivery_blocked", False)),
    }


def _analyze_one(llm: LLMClient, resume: ResumeDraft, job: Job) -> dict[str, Any]:
    """单条职位跑一次 LLM 匹配。

    Args:
        llm: LLM 客户端。
        resume: 简历骨架。
        job: 职位。

    Returns:
        模板形状的匹配结果。

    Raises:
        ValueError: LLM 没回合法 JSON。
    """
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
        ],
        max_tokens=C.LLM_MATCH_MAX_TOKENS,
    )
    data = extract_json(raw)
    if data is None:
        raw = llm.chat(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user + "\n\n请严格只输出 JSON 对象。"},
            ],
            max_tokens=C.LLM_MATCH_MAX_TOKENS,
        )
        data = extract_json(raw)
    if data is None:
        raise ValueError("LLM 没回合法 JSON")

    return {
        "match_score": int(data.get("match_score") or 0),
        "matched_skills": [str(s) for s in (data.get("matched_skills") or [])][:12],
        "missing_skills": [str(s) for s in (data.get("missing_skills") or [])][:12],
        "verdict": str(data.get("verdict") or ""),
        "pros": [str(p) for p in (data.get("pros") or [])][:6],
        "cons": [str(c) for c in (data.get("cons") or [])][:6],
        "reasons": [str(r) for r in (data.get("reasons") or [])][:6],
        "advice": str(data.get("advice") or ""),
        "greeting": str(data.get("greeting") or ""),
    }


def _finalize(task: AnalyzeTask, resume: ResumeDraft, cfg: Any) -> None:
    """聚合排序、写 analysis 行并把任务置为完成。

    Args:
        task: 分析任务。
        resume: 简历骨架。
        cfg: LLM 配置。
    """
    gaps: Counter[str] = Counter()
    for item in task.matches:
        for skill in item.get("missing_skills") or []:
            gaps[str(skill)] += 1
    ranked = sorted(task.matches, key=lambda m: m.get("match_score") or 0, reverse=True)
    safety_counts = {
        "total": len(ranked),
        "company_blacklisted": sum(1 for m in ranked if m.get("company_blacklisted")),
        "jd_blacklisted": sum(1 for m in ranked if m.get("jd_blacklisted")),
        "jd_rule_unchecked": sum(1 for m in ranked if not m.get("jd_rule_checked", False)),
        "auto_delivery_blocked": sum(1 for m in ranked if m.get("auto_delivery_blocked")),
    }

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
        "blacklist_summary": safety_counts,
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
