"""面试题生成任务：异步生成面试准备题目，落 `interview_questions` 表。

按 `docs/interview-question-generation.md` 实现：
- 匹配分析里的岗位逐个生成（批量），也可只生成指定岗位
- 没做匹配分析的岗位可直接按 JD + 简历出题
- 异步执行，不阻塞主流程；事件流水记录进度
- 可并行跑多个任务；同一岗位不能重复提交
- 重新生成 = 原地替换该岗位的旧题（只改不增），不新插 analysis

Attributes:
    STATUS_RUNNING: 任务状态：进行中。
    STATUS_DONE: 任务状态：完成。
    STATUS_ERROR: 任务状态：失败。
    STATUS_CANCELLED: 任务状态：已取消。
    SITE: 出题来源站点（多平台预留，当前只有 boss）。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections import deque
from typing import Any, Sequence

from ..errors import ConflictError, NotFoundError, ValidationWebError
from . import interview_generator as IG
from .llm_client import LLMClient
from .llm_config_store import load_config
from .resume_store import load_analysis, load_resume

logger = logging.getLogger(__name__)

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ERROR = "error"
STATUS_CANCELLED = "cancelled"

SITE = "boss"


class InterviewGenerationTask:
    """一次面试题生成任务的内存状态与事件流水。

    Attributes:
        events: 最近事件流水（最多 100 条）。
        cancel_flag: 协作式取消标记。
        result: 完成后的统计（生成题数 / 处理岗位数）。
        request: 启动参数快照（重试用）。
    """

    def __init__(
        self,
        *,
        analysis_id: str,
        job_ids: Sequence[str],
        question_count: int | None = None,
        resume_id: str = "",
        question_type: str = "",
        request: dict[str, Any] | None = None,
    ) -> None:
        """初始化。

        Args:
            analysis_id: 分析 ID（批量出题时取首个；可为空串）。
            job_ids: 要生成的岗位。
            question_count: 题量；缺省按匹配度定。补题时是用户填的条数。
            resume_id: 没有分析时用的简历（重试时要带着）。
            question_type: 补题题型；非空 = 补题（只补这一型，不覆盖旧题）。
            request: 原始启动参数快照（重试用）。
        """
        self.task_id = "ig_" + uuid.uuid4().hex[:10]
        self.analysis_id = analysis_id
        self.job_ids = list(job_ids)
        self.question_count = question_count
        self.resume_id = resume_id
        self.question_type = question_type
        self.request = dict(request or {})
        self.status = STATUS_RUNNING
        self.created_at = time.time()
        self.ended_at: float | None = None
        self.error: str | None = None
        self.current = ""
        self.total = 0
        self.done = 0
        self.question_total = 0
        self.events: deque[dict[str, Any]] = deque(maxlen=100)
        self.cancel_flag = False
        self.lock = threading.Lock()
        self.result: dict[str, Any] | None = None

    def push(self, name: str, payload: dict[str, Any] | None = None) -> None:
        """记一条事件。"""
        with self.lock:
            self.events.append({"at": time.time(), "name": name, "payload": payload or {}})

    def snapshot(self) -> dict[str, Any]:
        """当前状态副本（含进度 / 事件 / 岗位清单）。"""
        with self.lock:
            return {
                "task_id": self.task_id,
                "analysis_id": self.analysis_id,
                "resume_id": self.resume_id,
                "job_ids": list(self.job_ids),
                "question_type": self.question_type,
                "status": self.status,
                "current": self.current,
                "total": self.total,
                "done": self.done,
                "question_total": self.question_total,
                "created_at": self.created_at,
                "ended_at": self.ended_at,
                "error": self.error,
                "events": list(self.events)[-20:],
                "finished": self.result is not None,
            }


class InterviewTaskManager:
    """面试题生成任务管理：可并行跑多个，同一岗位不能重复提交。

    跑完的任务保留在内存里一小段时间（最近 50 条），方便查进度。
    """

    #: 最多保留的历史任务数（含在跑的）。
    KEEP = 50

    def __init__(self) -> None:
        """初始化。"""
        self._lock = threading.Lock()
        self._tasks: dict[str, InterviewGenerationTask] = {}

    def current(self) -> InterviewGenerationTask | None:
        """最近一个任务；没有则 `None`（兼容旧调用）。"""
        with self._lock:
            if not self._tasks:
                return None
            return next(reversed(self._tasks.values()))

    def running(self) -> list[InterviewGenerationTask]:
        """所有在跑的任务。"""
        with self._lock:
            return [t for t in self._tasks.values() if t.status == STATUS_RUNNING]

    def all(self) -> list[InterviewGenerationTask]:
        """最近保留的全部任务（在跑 + 已结束），按启动时间从新到旧。"""
        with self._lock:
            return list(reversed(self._tasks.values()))

    def get(self, task_id: str) -> InterviewGenerationTask:
        """按 id 取任务；不存在抛 `NotFoundError`。"""
        with self._lock:
            task = self._tasks.get(task_id)
        if task is None:
            raise NotFoundError(f"面试题生成任务不存在：{task_id}")
        return task

    def snapshot(self, task_id: str | None = None) -> dict[str, Any]:
        """查询进度快照。

        给了 task_id 就查那一个；没给回最近一个任务，一个都没有时为 idle。
        """
        if task_id:
            return self.get(task_id).snapshot()
        task = self.current()
        if task is None:
            return {"task_id": None, "status": "idle"}
        return task.snapshot()

    def list_snapshots(self) -> dict[str, Any]:
        """全部任务快照 + 在跑数 + 被占用的岗位（页面顶部 / 任务中心用）。"""
        tasks = self.all()
        busy: dict[str, str] = {}
        for t in tasks:
            if t.status in (STATUS_RUNNING, STATUS_ERROR):
                for jid in t.job_ids:
                    busy[jid] = t.task_id
        return {
            "running_count": sum(1 for t in tasks if t.status == STATUS_RUNNING),
            "tasks": [t.snapshot() for t in tasks],
            "busy_job_ids": busy,
        }

    def delete(self, task_id: str) -> InterviewGenerationTask:
        """从任务列表删掉一条（失败任务的「删除」）；不存在抛 `NotFoundError`。

        在跑的任务要先停才能删。
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise NotFoundError(f"面试题生成任务不存在：{task_id}")
            if task.status == STATUS_RUNNING:
                raise ConflictError("任务还在跑，请先停止再删除")
            self._tasks.pop(task_id, None)
        return task

    def retry(self, task_id: str) -> InterviewGenerationTask:
        """按原参数重跑一条失败任务；不存在或没失败抛错。

        先摘掉旧记录再起新任务，避免自己撞自己的占用检查。
        """
        old = self.get(task_id)
        if old.status == STATUS_RUNNING:
            raise ConflictError("任务还在跑，不能重复提交")
        if old.status == STATUS_DONE:
            raise ConflictError("任务已成功完成，无需重试")
        req = dict(old.request or {})
        with self._lock:
            self._tasks.pop(task_id, None)
        return self.start(
            analysis_id=str(req.get("analysis_id") or ""),
            job_ids=list(req.get("job_ids") or old.job_ids),
            resume_id=str(req.get("resume_id") or ""),
            question_count=req.get("question_count"),
            question_type=str(req.get("question_type") or ""),
        )

    def start(
        self,
        *,
        analysis_id: str = "",
        job_ids: Sequence[str] | None = None,
        resume_id: str = "",
        question_count: int | None = None,
        question_type: str = "",
    ) -> InterviewGenerationTask:
        """启动一次生成并起后台线程（可与其它任务并行）。

        同一批岗位已有任务在跑时直接返回那个任务；
        请求里有岗位正在别的任务里跑时抛 `ConflictError`（不能重复提交）。
        分析不存在抛 `NotFoundError`，没配 AI 抛 `ValidationWebError`。

        Args:
            analysis_id: 分析 ID；给了就整批走这份分析（含它的简历 / 匹配结果）。
            job_ids: 只生成这些岗位；给了 analysis_id 且本项为空 = 分析里全部已匹配岗位。
            resume_id: 没有分析时（或分析没绑简历时）用哪份简历。
            question_count: 题量（整套出题 10-15；补题 = 用户填的条数，最多 10）。
            question_type: 补题题型；非空 = 补题，只补这一型、不覆盖旧题。

        Returns:
            任务。
        """
        targets = _resolve_targets(
            analysis_id=analysis_id.strip(),
            job_ids=job_ids,
            resume_id=resume_id.strip(),
        )
        if not targets:
            raise ValidationWebError("没有要生成的岗位")

        cfg = load_config()
        if not cfg.configured:
            raise ValidationWebError(
                "AI 还没配置，请先到「AI 设置」填好 API Key / 接口地址 / 模型"
            )

        if question_type and question_type not in IG.QUESTION_TYPES:
            raise ValidationWebError(
                f"题型只能是：{' / '.join(IG.QUESTION_TYPES)}"
            )

        primary_analysis = analysis_id.strip() or str(targets[0].get("analysis_id") or "")
        resolved_ids = [str(t["encrypt_job_id"]) for t in targets]
        resolved_set = set(resolved_ids)

        with self._lock:
            # 整批岗位和某个在跑任务完全一样 → 幂等返回那个任务
            for t in self._tasks.values():
                if t.status != STATUS_RUNNING:
                    continue
                if set(t.job_ids) == resolved_set:
                    return t
            # 有岗位在任务列表里（在跑 / 失败）→ 不能重复提交
            busy: dict[str, str] = {}
            for t in self._tasks.values():
                if t.status not in (STATUS_RUNNING, STATUS_ERROR):
                    continue
                for jid in t.job_ids:
                    if jid in resolved_set:
                        busy[jid] = t.task_id
            if busy:
                raise ConflictError(
                    "这些岗位已有生成任务在跑，不能重复提交：" + "、".join(sorted(busy))
                )

            task = InterviewGenerationTask(
                analysis_id=primary_analysis,
                job_ids=resolved_ids,
                question_count=question_count,
                resume_id=resume_id.strip(),
                question_type=question_type.strip(),
                request={
                    "analysis_id": analysis_id.strip(),
                    "job_ids": [str(j).strip() for j in (job_ids or []) if str(j).strip()],
                    "resume_id": resume_id.strip(),
                    "question_count": question_count,
                    "question_type": question_type.strip(),
                },
            )
            task.total = len(targets)
            self._tasks[task.task_id] = task
            self._prune_locked()

        thread = threading.Thread(
            target=self._run, args=(task, targets), daemon=True, name=task.task_id
        )
        thread.start()
        logger.info(
            "面试题生成任务已启动：%s（分析 %s，%d 个岗位）",
            task.task_id,
            primary_analysis or "-",
            len(targets),
        )
        return task

    def cancel(self, task_id: str | None = None) -> InterviewGenerationTask:
        """请求取消任务；没给 task_id 就取消所有在跑的。"""
        if task_id:
            task = self.get(task_id)
            if task.status != STATUS_RUNNING:
                raise NotFoundError("该面试题生成任务已经结束")
            task.cancel_flag = True
            return task
        running = self.running()
        if not running:
            raise NotFoundError("没有在跑的面试题生成任务")
        for t in running:
            t.cancel_flag = True
        return running[0]

    def _prune_locked(self) -> None:
        """裁掉最老的已结束任务，只留最近 KEEP 条（调用方要持锁）。"""
        if len(self._tasks) <= self.KEEP:
            return
        finished = [tid for tid, t in self._tasks.items() if t.status != STATUS_RUNNING]
        # 按插入序（旧→新）淘汰
        for tid in finished[: max(0, len(self._tasks) - self.KEEP)]:
            self._tasks.pop(tid, None)

    def _run(
        self,
        task: InterviewGenerationTask,
        targets: Sequence[dict[str, Any]],
    ) -> None:
        """线程体：逐岗位出题 → 落库 → done。"""
        task.push("generate_start", {"total": len(targets)})
        resume_cache: dict[str, str | None] = {}
        try:
            llm = LLMClient(load_config())

            for item in targets:
                if task.cancel_flag:
                    raise _CancelledError()
                job_id = str(item.get("encrypt_job_id") or "")
                match = item.get("match") if isinstance(item.get("match"), dict) else {}
                job_data = _job_data(match or {"encrypt_job_id": job_id})
                job_name = str(job_data.get("job_name") or job_id)
                with task.lock:
                    task.current = f"正在处理岗位 {job_name}"
                task.push(
                    "job_progress",
                    {"job_name": job_name, "done": task.done, "total": task.total},
                )

                resume_content = _resume_text(
                    str(item.get("resume_id") or ""), cache=resume_cache
                )
                if resume_content is None:
                    rid = str(item.get("resume_id") or "")
                    if not rid:
                        reason = (
                            "分析没有关联简历"
                            if item.get("analysis_id")
                            else "没有可用简历，请先上传简历"
                        )
                    else:
                        reason = "简历不存在或已删除"
                    task.push("job_error", {"job_name": job_name, "error": reason})
                    with task.lock:
                        task.done += 1
                    continue

                if not str(job_data.get("job_desc") or "").strip():
                    task.push("skip", {"job_name": job_name, "reason": "缺少职位描述"})
                    with task.lock:
                        task.done += 1
                    continue

                try:
                    existing = (
                        load_existing_questions(job_id) if task.question_type else []
                    )
                    questions = IG.generate_questions(
                        job_data=job_data,
                        resume_content=resume_content,
                        match_result=match,
                        question_count=task.question_count,
                        question_type=task.question_type,
                        existing_questions=existing,
                        llm=llm,
                    )
                except Exception as exc:  # noqa: BLE001 - 单岗失败不拖垮整批
                    logger.warning("岗位 %s 出题失败：%s", job_name, exc)
                    task.push("job_error", {"job_name": job_name, "error": str(exc)})
                    with task.lock:
                        task.done += 1
                    continue

                if task.cancel_flag:
                    raise _CancelledError()
                saved = save_questions(
                    analysis_id=str(item.get("analysis_id") or ""),
                    encrypt_job_id=job_id,
                    questions=questions,
                    # 补题只追加；整套出题原地替换（只改不增）
                    replace=not task.question_type,
                )
                with task.lock:
                    task.done += 1
                    task.question_total += saved
                task.push("job_done", {"job_name": job_name, "count": saved})

            task.result = {
                "job_total": len(targets),
                "question_total": task.question_total,
            }
            task.push(
                "generate_done",
                {"job_total": len(targets), "question_total": task.question_total},
            )
            with task.lock:
                task.status = STATUS_DONE
                task.ended_at = time.time()
            logger.info(
                "面试题生成任务完成：%s，共 %d 道题", task.task_id, task.question_total
            )
            return
        except _CancelledError:
            error = ""
        except Exception as exc:  # noqa: BLE001 - 任务失败不拖垮服务
            task.error = str(exc) if str(exc) else exc.__class__.__name__
            error = task.error
            logger.warning("面试题生成任务失败：%s", task.error)
        with task.lock:
            task.status = STATUS_CANCELLED if not error else STATUS_ERROR
            task.ended_at = time.time()


class _CancelledError(Exception):
    """内部：任务被协作式取消。"""


def _resolve_targets(
    *,
    analysis_id: str,
    job_ids: Sequence[str] | None,
    resume_id: str,
) -> list[dict[str, Any]]:
    """把一次生成请求收成工作单元列表。

    每个工作单元：`{analysis_id, encrypt_job_id, resume_id, match}`。
    有 analysis_id 时整批走那份分析；没有则按岗位各自找最近分析，找不到就用
    入参 resume_id（或库里最新一份简历）直接按 JD 出题。
    """
    wanted = [str(j).strip() for j in (job_ids or []) if str(j).strip()]

    if analysis_id:
        try:
            payload = load_analysis(analysis_id)
        except FileNotFoundError as exc:
            raise NotFoundError(f"分析结果不存在：{analysis_id}") from exc
        matches = [m for m in (payload.get("matches") or []) if isinstance(m, dict)]
        rid = str(payload.get("resume_id") or "") or resume_id
        if wanted:
            known = {str(m.get("encrypt_job_id") or "") for m in matches}
            missing = [j for j in wanted if j not in known]
            if missing:
                raise ValidationWebError(f"分析里没有岗位：{'、'.join(missing)}")
            targets = [m for m in matches if str(m.get("encrypt_job_id") or "") in set(wanted)]
        else:
            targets = matches
        if not targets:
            raise ValidationWebError("分析结果为空，无法生成面试题")
        return [
            {
                "analysis_id": analysis_id,
                "encrypt_job_id": str(m.get("encrypt_job_id") or ""),
                "resume_id": rid,
                "match": m,
            }
            for m in targets
        ]

    if not wanted:
        raise ValidationWebError("请选择要生成的岗位")

    index = _analysis_index()
    fallback = resume_id or _latest_resume_id()
    out: list[dict[str, Any]] = []
    for jid in wanted:
        ctx = index.get(jid)
        if ctx is not None:
            out.append(
                {
                    "analysis_id": ctx["analysis_id"],
                    "encrypt_job_id": jid,
                    "resume_id": str(ctx.get("resume_id") or "") or fallback,
                    "match": ctx.get("match") or {},
                }
            )
        else:
            out.append(
                {
                    "analysis_id": "",
                    "encrypt_job_id": jid,
                    "resume_id": fallback,
                    "match": {},
                }
            )
    return out


def _analysis_index() -> dict[str, dict[str, Any]]:
    """职位 ID → 最近一条含它的分析上下文（新分析优先，只认第一次命中）。"""
    import boss_db

    conn = boss_db.acquire()
    rows = conn.execute(
        "SELECT analysis_id, payload FROM analysis ORDER BY created_at DESC"
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            payload = json.loads(str(row["payload"]))
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        resume_id = str(payload.get("resume_id") or "")
        for m in payload.get("matches") or []:
            if not isinstance(m, dict):
                continue
            jid = str(m.get("encrypt_job_id") or "")
            if jid and jid not in out:
                out[jid] = {
                    "analysis_id": str(row["analysis_id"]),
                    "resume_id": resume_id,
                    "match": m,
                }
    return out


def _latest_resume_id() -> str:
    """库里最新一份简历的 id；没有简历回空串。"""
    import boss_db

    conn = boss_db.acquire()
    row = conn.execute(
        "SELECT resume_id FROM resume ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    return str(row["resume_id"]) if row is not None else ""


def _resume_text(resume_id: str, *, cache: dict[str, str | None]) -> str | None:
    """读简历原文（带缓存）；拿不到回 `None`。"""
    if not resume_id:
        return None
    if resume_id not in cache:
        try:
            cache[resume_id] = load_resume(resume_id).raw or ""
        except FileNotFoundError:
            cache[resume_id] = None
    return cache[resume_id]


def _job_data(item: dict[str, Any]) -> dict[str, Any]:
    """从 match 条目 + `jobs` 表装职位信息；表里没有再靠快照兜底。"""
    job_id = str(item.get("encrypt_job_id") or "")
    desc = ""
    skills: list[str] = []
    job_name = str(item.get("job_name") or "")
    brand_name = str(item.get("brand_name") or "")
    job_experience = str(item.get("job_experience") or "")
    job_degree = str(item.get("job_degree") or "")
    try:
        import boss_db

        conn = boss_db.acquire()
        row = conn.execute(
            "SELECT job_name, brand_name, job_experience, job_degree, job_desc, skills "
            "FROM jobs WHERE encrypt_job_id = ?",
            (job_id,),
        ).fetchone()
        if row is not None:
            desc = str(row["job_desc"] or "")
            job_name = str(row["job_name"] or "") or job_name
            brand_name = str(row["brand_name"] or "") or brand_name
            job_experience = str(row["job_experience"] or "") or job_experience
            job_degree = str(row["job_degree"] or "") or job_degree
            try:
                parsed = json.loads(str(row["skills"] or "[]"))
                if isinstance(parsed, list):
                    skills = [str(s) for s in parsed]
            except ValueError:
                skills = []
    except Exception:  # noqa: BLE001 - 拿不到 JD 就靠快照兜底
        pass

    matched = item.get("matched_skills") or []
    return {
        "job_name": job_name,
        "brand_name": brand_name,
        "job_experience": job_experience,
        "job_degree": job_degree,
        "job_skills": skills or [str(s) for s in matched][:12],
        "job_desc": desc,
    }


def save_questions(
    *,
    analysis_id: str,
    encrypt_job_id: str,
    questions: Sequence[IG.InterviewQuestion],
    site: str = SITE,
    replace: bool = True,
) -> int:
    """写入一岗的题目；`replace=True` 原地替换旧题（只改不增），`False` 追加。

    删旧题按职位 ID（+站点）来，这样同一岗位先裸出题后带分析重出也只留一套。
    补题走 `replace=False`，只插不删。

    Args:
        analysis_id: 分析 ID（可为空串，纯 JD 出题时）。
        encrypt_job_id: 职位 ID。
        questions: 新题目。
        site: 来源站点。
        replace: 是否先删掉该岗位旧题再写。

    Returns:
        写入条数。
    """
    import boss_db

    now = time.time()
    conn = boss_db.acquire()
    with conn:
        if replace:
            conn.execute(
                "DELETE FROM interview_questions WHERE encrypt_job_id = ? AND site = ?",
                (encrypt_job_id, site),
            )
        for q in questions:
            conn.execute(
                "INSERT INTO interview_questions "
                "(analysis_id, site, encrypt_job_id, question_text, question_type, "
                "difficulty, is_frequent, answer_hints, user_prepared, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (
                    analysis_id,
                    site,
                    encrypt_job_id,
                    q.question_text,
                    q.question_type,
                    q.difficulty,
                    1 if q.is_frequent else 0,
                    json.dumps(q.answer_hints, ensure_ascii=False),
                    now,
                    now,
                ),
            )
    return len(questions)


def load_existing_questions(
    encrypt_job_id: str, *, site: str = SITE
) -> list[str]:
    """读一岗已有题目正文（补题去重用）。"""
    import boss_db

    rows = boss_db.acquire().execute(
        "SELECT question_text FROM interview_questions "
        "WHERE encrypt_job_id = ? AND site = ? ORDER BY created_at",
        (encrypt_job_id, site),
    ).fetchall()
    return [str(r["question_text"]) for r in rows]


interview_tasks = InterviewTaskManager()
