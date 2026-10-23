"""简历优化的单飞任务：装配 → LLM 生成 → diff → done。

与 `MatchTaskManager` 同一套单飞骨架：同一时刻只跑一个，长任务面板轮询
`/api/resume/revise/status`。生成原地写回草稿行（只改不增），停止 / 出错
不写文件、不入库。

支持两种优化模式：
- targeted：基于目标岗位 JD 的定向优化
- free：基于用户选择章节 + 提示词的自由优化

Attributes:
    STATUS_RUNNING: 任务状态：进行中。
    STATUS_DONE: 任务状态：完成（草稿可对比）。
    STATUS_ERROR: 任务状态：失败。
    STATUS_CANCELLED: 任务状态：已取消。
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from typing import Any, Sequence

from ..errors import ConflictError, NotFoundError
from ..services import resume_revision as RR
from ..services import resume_revision_free as RRF
from ..services.resume_store import (
    REVISION_CANCELLED,
    REVISION_ERROR,
    create_revision,
    mark_revision_status,
    update_revision_result,
)

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ERROR = "error"
STATUS_CANCELLED = "cancelled"


class RevisionTask:
    """内存里的简历优化任务状态与事件流水。

    Attributes:
        events: 最近事件流水（最多 100 条）。
        cancel_flag: 协作式取消标记。
        result: 完成后回 `(revised_md, changes, target, original_md, resume_title)`。
    """

    def __init__(
        self,
        *,
        resume_id: str,
        resume_title: str,
        job_count: int,
        target_kind: str,
        target_json: dict[str, Any],
        crawl_task_id: str,
        original_md: str,
        revision_id: str | None = None,
        mode: str = "targeted",
        selected_sections: list[str] | None = None,
        optimization_prompt: str = "",
    ) -> None:
        """初始化。

        Args:
            mode: 'targeted' (基于 JD) 或 'free' (自由优化)。
            selected_sections: 自由模式选择的章节列表。
            optimization_prompt: 自由模式的优化提示词。
        """
        if revision_id:
            self.task_id = revision_id
        else:
            self.task_id = "rv_" + uuid.uuid4().hex[:10]
        self.revision_id = self.task_id
        self.resume_id = resume_id
        self.resume_title = resume_title
        self.job_count = job_count
        self.target_kind = target_kind
        self.target_json = target_json
        self.crawl_task_id = crawl_task_id
        self.original_md = original_md
        self.mode = mode or "targeted"
        self.selected_sections = list(selected_sections or [])
        self.optimization_prompt = optimization_prompt or ""
        self.status = STATUS_RUNNING
        self.created_at = time.time()
        self.ended_at: float | None = None
        self.error: str | None = None
        self.current = ""
        self.events: deque[dict[str, Any]] = deque(maxlen=100)
        self.cancel_flag = False
        self.lock = threading.Lock()
        self.result: dict[str, Any] | None = None

    def push(self, name: str, payload: dict[str, Any] | None = None) -> None:
        """记一条事件。"""
        with self.lock:
            self.events.append({"at": time.time(), "name": name, "payload": payload or {}})

    def snapshot(self) -> dict[str, Any]:
        """当前状态副本（含进度 / 事件 / 可选草稿摘要）。"""
        with self.lock:
            return {
                "task_id": self.task_id,
                "revision_id": self.revision_id,
                "status": self.status,
                "resume_id": self.resume_id,
                "mode": self.mode,
                "crawl_task_id": self.crawl_task_id,
                "job_count": self.job_count,
                "current": self.current,
                "created_at": self.created_at,
                "ended_at": self.ended_at,
                "error": self.error,
                "events": list(self.events)[-20:],
                "done": self.result is not None,
            }


class RevisionTaskManager:
    """同一时刻只跑一个简历优化任务。"""

    def __init__(self) -> None:
        """初始化。"""
        self._lock = threading.Lock()
        self._task: RevisionTask | None = None

    def current(self) -> RevisionTask | None:
        """当前任务；没有则 `None`。"""
        with self._lock:
            return self._task

    def snapshot(self, task_id: str | None = None) -> dict[str, Any]:
        """查询进度快照；空闲时为 idle，指定 task_id 不是当前任务时抛 `NotFoundError`。"""
        task = self.current()
        if task is None:
            return {"task_id": None, "status": "idle"}
        if task_id and task.task_id != task_id:
            raise NotFoundError(f"简历优化任务不存在：{task_id}")
        return task.snapshot()

    def start(
        self,
        *,
        resume_id: str,
        resume_title: str,
        jobs: Sequence[Any],
        target_kind: str,
        target_json: dict[str, Any],
        crawl_task_id: str,
        revision_id: str | None = None,
        mode: str = "targeted",
        selected_sections: list[str] | None = None,
        optimization_prompt: str = "",
    ) -> RevisionTask:
        """启动一次简历优化并起后台线程；已有任务在跑时抛 `ConflictError`。

        Args:
            resume_id: 底稿简历 id。
            resume_title: 底稿标题。
            jobs: 目标岗位（`Job` 或其他行为兼容对象）。
            target_kind: jobs | crawl_task。
            target_json: 自包含岗位快照。
            crawl_task_id: 非空时来源是某个抓取批次。
            revision_id: 复用既有草稿行的 id（重新生成用，原稿取已快照的
                `original_md`）；省略则新建。
            mode: 'targeted' (基于 JD) 或 'free' (自由优化)。
            selected_sections: 自由模式选择的章节列表。
            optimization_prompt: 自由模式的优化提示词。

        Returns:
            新建的任务。
        """
        reusing = bool(revision_id)
        if reusing:
            from .resume_store import load_revision

            try:
                rev = load_revision(revision_id)
            except FileNotFoundError as exc:
                raise NotFoundError(f"简历优化记录不存在：{revision_id}") from exc
            task = RevisionTask(
                resume_id=resume_id,
                resume_title=rev.get("resume_title") or resume_title,
                job_count=len(jobs),
                target_kind=target_kind,
                target_json=target_json,
                crawl_task_id=crawl_task_id,
                original_md=rev.get("original_md") or "",
                revision_id=revision_id,
                mode=rev.get("mode") or mode,
                selected_sections=rev.get("selected_sections") or selected_sections or [],
                optimization_prompt=rev.get("optimization_prompt") or optimization_prompt,
            )
        else:
            from .resume_store import load_resume

            try:
                seed = load_resume(resume_id)
            except FileNotFoundError as exc:
                raise NotFoundError(f"简历不存在：{resume_id}") from exc
            task = RevisionTask(
                resume_id=resume_id,
                resume_title=resume_title or seed.title,
                job_count=len(jobs),
                target_kind=target_kind,
                target_json=target_json,
                crawl_task_id=crawl_task_id,
                original_md=seed.raw or "",
                mode=mode,
                selected_sections=selected_sections or [],
                optimization_prompt=optimization_prompt,
            )

        with self._lock:
            if self._task is not None and self._task.status == STATUS_RUNNING:
                raise ConflictError("已有简历优化任务在跑")
            if not reusing:
                create_revision(
                    revision_id=task.task_id,
                    resume_id=resume_id,
                    resume_title=task.resume_title,
                    target_kind=target_kind,
                    target_json=target_json,
                    crawl_task_id=crawl_task_id,
                    job_count=len(jobs),
                    original_md=task.original_md,
                    mode=mode,
                    selected_sections=selected_sections,
                    optimization_prompt=optimization_prompt,
                )
            self._task = task

        thread = threading.Thread(target=self._run, args=(task, jobs), daemon=True, name=task.task_id)
        thread.start()
        return task

    def cancel(self, task_id: str | None = None) -> RevisionTask:
        """请求取消当前任务；没有在跑的简历优化任务时抛 `NotFoundError`。"""
        task = self.current()
        if task is None or (task_id and task.task_id != task_id):
            raise NotFoundError("没有在跑的简历优化任务")
        task.cancel_flag = True
        return task

    def _run(self, task: RevisionTask, jobs: Sequence[Any]) -> None:
        """线程体：装配画像 → LLM 生成 → 写草稿 → done。"""
        task.push("revise_start", {"job_count": len(jobs), "mode": task.mode})
        try:
            if task.cancel_flag:
                raise _CancelledError()

            if task.mode == "free":
                with task.lock:
                    task.current = "按选定章节调用 LLM 优化…"
                task.push("revise_llm", {})
                result = RRF.generate_free_revision(
                    resume_id=task.resume_id,
                    selected_sections=task.selected_sections,
                    optimization_prompt=task.optimization_prompt,
                )
            else:
                with task.lock:
                    task.current = "装配岗位画像、调用 LLM 生成…"
                task.push("revise_llm", {})
                result = RR.generate_revision(
                    resume_id=task.resume_id, jobs=jobs, target_json=task.target_json
                )

            if task.cancel_flag:
                raise _CancelledError()
            updated = update_revision_result(
                task.revision_id,
                revised_md=result["revised_md"],
                changes=result["changes"],
                model=_model_name(),
                status="draft",
            )
            task.result = result
            task.push("revise_done", {"updated": updated})
            with task.lock:
                task.status = STATUS_DONE
                task.ended_at = time.time()
            return
        except _CancelledError:
            status = REVISION_CANCELLED
            error = ""
        except Exception as exc:  # noqa: BLE001 - 单个任务失败不拖垮服务
            task.error = str(exc) if str(exc) else exc.__class__.__name__
            status = REVISION_ERROR
            error = task.error
        with task.lock:
            task.status = STATUS_ERROR if status == REVISION_ERROR else REVISION_CANCELLED
            task.ended_at = time.time()
        mark_revision_status(task.revision_id, status, error=error)


def _model_name() -> str:
    """取当前配置的模型名（生成时用于 `resume_revisions.model`）。"""
    from .llm_config_store import load_config

    try:
        cfg = load_config()
        return cfg.model or ""
    except Exception:  # noqa: BLE001 - 拿不到就留空
        return ""


class _CancelledError(Exception):
    """内部：任务被协作式取消。"""


revision_tasks = RevisionTaskManager()