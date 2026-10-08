"""匹配任务：简历 vs 全库/勾选岗位，串行调 LLM，出匹配度 + 优缺点 + 招呼语。

跟老的 :mod:`boss_web.services.resume_analyzer` **共用输出模板**（含 ``pros`` /
``cons``），差别在这边：

- 简历侧优先吃 **LLM 固定模板解析结果**（``meta.llm.data`` 的 summary / skills /
  work / intent），没解析过才退回规则切章节的摘要；
- 职位侧注入 **JD 正文**（``job_desc``），没抓到就用标签/技能兜底；
- 范围是**全库全部岗位**或勾选的 job_id（C4 已拍板，不再 top_k 封顶）；
- 结果写 ``analysis`` 表，每条 match 带 ``encrypt_job_id``，招呼语可后改。

串行 + ``ANALYZE_INTERVAL`` 节流，可随时取消；单条失败只记流水，不拖垮整批。
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
from ..errors import ConflictError, NotFoundError
from .llm_client import LLMClient, extract_json
from .llm_config_store import load_config
from .resume_store import load_llm_parse, load_resume, save_analysis

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "你是资深互联网猎头，擅长人岗匹配。"
    "只输出一个 JSON 对象，不要任何解释文字或代码围栏。"
    "注意立场："
    "pros / cons / advice 是给求职者看的评估，"
    "而 greeting 是求职者本人发给招聘方 HR 的一条私信，"
    "一律用求职者第一人称「我」写，绝不能写成招聘方/HR 的口吻。"
)

USER_TEMPLATE = """# 简历（结构化摘要）
{resume_brief}

# 目标职位
- 岗位：{job_name}
- 公司：{brand_name}（{brand_industry} / {brand_scale_name}）
- 地点：{location}
- 薪资：{salary_desc}
- 经验/学历：{job_experience} / {job_degree}
- 标签：{job_labels}
- 技能要求：{job_skills}

# 职位描述（JD）
{job_desc}

请评估人岗匹配度，输出 JSON：
{{
  "match_score": 0-100 的整数,
  "matched_skills": ["简历与职位都有的技能"],
  "missing_skills": ["职位要但简历没有的技能"],
  "verdict": "一句话结论",
  "pros": ["求职者相对这个岗位的亮点，2-4 条"],
  "cons": ["相对这个岗位的短板，2-4 条"],
  "advice": "给求职者的建议",
  "greeting": "求职者本人（第一人称『我』）发给招聘方 HR 的打招呼私信，60 字以内，自然具体不吹牛。必须站在求职者立场自我推荐，禁止以招聘方/HR 口吻说话——不要出现『我们公司』『我们团队』『我们正在招』『欢迎投递』『期待你的加入』这类话，也不要介绍公司或岗位"
}}
"""

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ERROR = "error"
STATUS_CANCELLED = "cancelled"


class MatchTask:
    def __init__(self, resume_id: str, job_ids: Sequence[str]) -> None:
        self.task_id = "mt_" + uuid.uuid4().hex[:10]
        self.resume_id = resume_id
        self.job_ids = list(job_ids)
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


class MatchTaskManager:
    """同一时刻只跑一个匹配任务。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._task: MatchTask | None = None

    def current(self) -> MatchTask | None:
        with self._lock:
            return self._task

    def snapshot(self, task_id: str | None = None) -> dict[str, Any]:
        task = self.current()
        if task is None:
            return {"task_id": None, "status": "idle"}
        if task_id and task.task_id != task_id:
            raise NotFoundError(f"匹配任务不存在：{task_id}")
        return task.snapshot()

    def start(self, *, resume_id: str, job_ids: Sequence[str] | None = None) -> MatchTask:
        with self._lock:
            if self._task is not None and self._task.status == STATUS_RUNNING:
                raise ConflictError("已有匹配任务在跑")
            task = MatchTask(resume_id, job_ids or [])
            self._task = task

        thread = threading.Thread(target=self._run, args=(task,), daemon=True, name=task.task_id)
        thread.start()
        return task

    def cancel(self, task_id: str | None = None) -> MatchTask:
        task = self.current()
        if task is None or (task_id and task.task_id != task_id):
            raise NotFoundError("没有在跑的匹配任务")
        task.cancel_flag = True
        return task

    # ------------------------------------------------------------------ #

    def _run(self, task: MatchTask) -> None:
        try:
            resume = load_resume(task.resume_id)
        except FileNotFoundError:
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

        # 简历侧优先吃 LLM 固定模板结果，没解析过才退回规则摘要
        try:
            llm_parse = load_llm_parse(task.resume_id)
        except FileNotFoundError:
            llm_parse = None
        resume_brief = _resume_brief(resume, llm_parse)

        jobs = _pick_jobs(task)
        task.total = len(jobs)
        if not jobs:
            task.status = STATUS_ERROR
            task.error = "没有可匹配的职位，请先抓取或指定 job_ids"
            task.ended_at = time.time()
            return

        llm = LLMClient(cfg)
        for job in jobs:
            if task.cancel_flag:
                task.status = STATUS_CANCELLED
                task.ended_at = time.time()
                _finalize(task, resume, cfg, llm_parse)
                return
            with task.lock:
                task.current_job = job.job_name
            task.push("job_start", {"job_name": job.job_name, "brand": job.brand_name})
            try:
                result = _match_one(llm, resume_brief, job)
            except Exception as exc:  # noqa: BLE001 - 单个职位失败不拖垮整批
                logger.warning("匹配职位 %s 失败：%s", job.job_name, exc)
                result = {
                    "match_score": 0,
                    "matched_skills": [],
                    "missing_skills": [],
                    "verdict": "匹配失败",
                    "pros": [],
                    "cons": [],
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
                # 发送要用的（C6），顺手带上，免得到时候再翻 raw_json
                "security_id": job.security_id,
                "lid": job.lid,
                **result,
            }
            with task.lock:
                task.matches.append(item)
                task.done += 1
            task.push("job_done", {"job_name": job.job_name, "score": item.get("match_score")})
            time.sleep(C.ANALYZE_INTERVAL)

        _finalize(task, resume, cfg, llm_parse)


match_tasks = MatchTaskManager()


# --------------------------------------------------------------------------- #
# 内部
# --------------------------------------------------------------------------- #


def _pick_jobs(task: MatchTask) -> list[Job]:
    from boss_jobs.store import JobStore

    with JobStore() as store:
        if task.job_ids:
            jobs = []
            for jid in task.job_ids:
                job = store.get_job(jid)
                if job is not None:
                    jobs.append(job)
            return jobs
        # 一键匹配：全库全部岗位（C4 已拍板，不再 top_k 封顶）
        return store.list_jobs(limit=100000, offset=0)


def _resume_brief(resume: Any, llm_parse: dict[str, Any] | None) -> str:
    """组装 prompt 里的简历块：优先 LLM 结构（summary/skills/work/intent）。"""
    lines: list[str] = []
    data = (llm_parse or {}).get("data") if isinstance(llm_parse, dict) else None
    if isinstance(data, dict):
        intent = data.get("intent") if isinstance(data.get("intent"), dict) else {}
        parts = [str(intent.get(k) or "").strip() for k in ("position", "city", "salary")]
        parts = [p for p in parts if p]
        if parts:
            lines.append(f"- 求职意向：{' / '.join(parts)}")
        skills = data.get("skills")
        if isinstance(skills, list) and skills:
            lines.append(f"- 技能标签：{'、'.join(str(s) for s in skills[:24])}")
        summary = str(data.get("summary") or "").strip()
        if summary:
            lines.append(f"- 摘要：{summary[:500]}")
        for w in (data.get("work") or [])[:3]:
            if not isinstance(w, dict):
                continue
            company = str(w.get("company") or "").strip()
            title = str(w.get("title") or "").strip()
            period = str(w.get("period") or "").strip()
            head = " · ".join(p for p in (company, title, period) if p)
            highs = [str(h) for h in (w.get("highlights") or [])[:3] if str(h).strip()]
            if head:
                lines.append(f"- 工作：{head}")
            for h in highs:
                lines.append(f"  · {h}")

    if not lines:
        # 兜底：规则切章节的摘要
        intent = (resume.sections.get("求职意向") or "").strip()
        if intent:
            lines.append(f"- 求职意向：{intent[:120]}")
        skills = resume.skills[:20]
        if skills:
            lines.append(f"- 技能标签：{'、'.join(skills)}")
        summary = (resume.summary or "").strip()
        if summary:
            lines.append(f"- 摘要：{summary[:400]}")
        work = (resume.sections.get("工作经历") or resume.sections.get("项目经历") or "").strip()
        if work:
            lines.append("- 经历摘录：")
            lines.append(work[:500])

    return "\n".join(lines) or "- （简历为空）"


def _job_desc_block(job: Job) -> str:
    desc = (job.job_desc or "").strip()
    if desc:
        return desc[:1500]
    # 没抓到 JD 就用标签/技能兜底
    bits = [b for b in (job.job_labels + job.skills) if b]
    return "（未抓到 JD）标签/技能：" + ("、".join(bits[:12]) if bits else "无")


def _match_one(llm: LLMClient, resume_brief: str, job: Job) -> dict[str, Any]:
    user = USER_TEMPLATE.format(
        resume_brief=resume_brief,
        job_name=job.job_name,
        brand_name=job.brand_name,
        brand_industry=job.brand_industry or "-",
        brand_scale_name=job.brand_scale_name or "-",
        location=job.location or "-",
        salary_desc=job.salary_desc or "-",
        job_experience=job.job_experience or "-",
        job_degree=job.job_degree or "-",
        job_labels="、".join(job.job_labels[:12]) or "-",
        job_skills="、".join(job.skills[:12]) or "-",
        job_desc=_job_desc_block(job),
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
        "pros": [str(p) for p in (data.get("pros") or [])][:6],
        "cons": [str(c) for c in (data.get("cons") or [])][:6],
        "advice": str(data.get("advice") or ""),
        "greeting": str(data.get("greeting") or ""),
    }


def _finalize(task: MatchTask, resume: Any, cfg: Any, llm_parse: dict[str, Any] | None) -> None:
    gaps: Counter[str] = Counter()
    for item in task.matches:
        for skill in item.get("missing_skills") or []:
            gaps[str(skill)] += 1
    ranked = sorted(task.matches, key=lambda m: m.get("match_score") or 0, reverse=True)

    payload = {
        "analysis_id": task.analysis_id,
        "kind": "match",
        "resume_id": resume.resume_id,
        "resume_title": resume.title,
        "created_at": task.created_at,
        "status": STATUS_DONE if task.status == STATUS_RUNNING else task.status,
        "llm": {"base_url": cfg.base_url, "model": cfg.model, "temperature": cfg.temperature},
        "resume_summary": {
            "skills": resume.skills[:20],
            "intent": (resume.sections.get("求职意向") or "")[:200],
            "summary": resume.summary,
            "llm": (llm_parse or {}).get("data") if llm_parse else None,
        },
        "matches": ranked,
        "skill_gaps": [{"skill": k, "count": v} for k, v in gaps.most_common(15)],
        "top_recommendations": [
            m["encrypt_job_id"] for m in ranked if (m.get("match_score") or 0) >= 70
        ][:10],
        "errors": [m.get("error") for m in task.matches if m.get("error")],
        "deliveries": [],
    }
    save_analysis(payload)
    with task.lock:
        if task.status == STATUS_RUNNING:
            task.status = STATUS_DONE
        task.ended_at = time.time()
        task.matches = ranked
    task.push("finished", {"analysis_id": task.analysis_id})
