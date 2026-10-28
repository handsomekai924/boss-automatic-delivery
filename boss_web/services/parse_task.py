"""简历解析异步任务：多份简历并发解析，每份同解析中不可重复提交。

同步解析长 POST 会卡住页面；改任务中心后提交即回 `task_id`，进度走 WS。
产物仍是原地 `UPDATE resume.meta`（只改不增），失败保留旧 `meta.llm`。

Attributes:
    STATUS_RUNNING: 解析中。
    STATUS_DONE: 完成。
    STATUS_ERROR: 失败。
    STATUS_CANCELLED: 已取消。
    MAX_PARALLEL: 全局并发上限，护 LLM 配额。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from typing import Any

from ..errors import ConflictError, NotFoundError, ValidationWebError
from .resume_parser import ResumeParseError, parse_and_stamp
from .resume_store import load_resume, save_llm_parse
from .task_runtime import POLICY_PER_KEY_SINGLE, TaskTypeSpec, hub

logger = logging.getLogger(__name__)

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ERROR = "error"
STATUS_CANCELLED = "cancelled"

MAX_PARALLEL = 3

hub.register(
    TaskTypeSpec(
        "parse",
        title="简历解析",
        policy=POLICY_PER_KEY_SINGLE,
        key_fn=lambda req: str(req.get("resume_id") or ""),
        max_parallel=MAX_PARALLEL,
        title_fn=lambda req: f"简历解析（{req.get('resume_title') or req.get('resume_id') or ''}）",
    )
)


class ParseTask:
    """一次简历解析任务的内存状态与事件流水。

    Attributes:
        task_id: 任务 id。
        resume_id: 被解析的简历 id。
        status: 任务状态。
        created_at: 创建时间戳。
        ended_at: 结束时间戳。
        error: 失败文案。
        current: 人话进度。
        result: 成功后的 `meta.llm` 载荷。
        events: 事件环。
        cancel_flag: 协作式取消标记。
        lock: 状态锁。
    """

    def __init__(self, *, resume_id: str, resume_title: str = "") -> None:
        """初始化。"""
        self.task_id = "pt_" + uuid.uuid4().hex[:10]
        self.resume_id = resume_id
        self.resume_title = resume_title
        self.status = STATUS_RUNNING
        self.created_at = time.time()
        self.ended_at: float | None = None
        self.error: str | None = None
        self.current = "准备调用 LLM 解析…"
        self.result: dict[str, Any] | None = None
        self.events: deque[dict[str, Any]] = deque(maxlen=50)
        self.cancel_flag = False
        self.lock = threading.RLock()

    def push(self, name: str, payload: dict[str, Any] | None = None) -> None:
        """记一条事件。

        Args:
            name: 事件名。
            payload: 事件载荷。
        """
        with self.lock:
            self.events.append({"at": time.time(), "name": name, "payload": payload or {}})

    def snapshot(self) -> dict[str, Any]:
        """当前状态副本。"""
        with self.lock:
            return {
                "task_id": self.task_id,
                "resume_id": self.resume_id,
                "resume_title": self.resume_title,
                "status": self.status,
                "current": self.current,
                "created_at": self.created_at,
                "ended_at": self.ended_at,
                "error": self.error,
                "events": list(self.events)[-20:],
                "finished": self.result is not None or self.status in (STATUS_ERROR, STATUS_CANCELLED),
            }


class ParseTaskManager:
    """多份简历并发解析：同一份简历解析中不可再提，不同简历随意并发。

    Attributes:
        KEEP: 最多保留的历史任务数。
    """

    KEEP = 50

    def __init__(self) -> None:
        """初始化。"""
        self._lock = threading.Lock()
        self._tasks: dict[str, ParseTask] = {}

    def all(self) -> list[ParseTask]:
        """全部任务，新到旧。"""
        with self._lock:
            return list(reversed(self._tasks.values()))

    def get(self, task_id: str) -> ParseTask:
        """按 id 取任务；不存在抛 `NotFoundError`。"""
        with self._lock:
            task = self._tasks.get(task_id)
        if task is None:
            raise NotFoundError(f"解析任务不存在：{task_id}")
        return task

    def by_resume(self, resume_id: str) -> ParseTask | None:
        """某份简历当前未结束的解析任务；没有回 `None`。"""
        with self._lock:
            for task in reversed(self._tasks.values()):
                if task.resume_id == resume_id and task.status == STATUS_RUNNING:
                    return task
        return None

    def snapshot(self, task_id: str | None = None) -> dict[str, Any]:
        """查询快照；不给 id 回某空闲结构。"""
        if task_id:
            return self.get(task_id).snapshot()
        return {"task_id": None, "status": "idle"}

    def start(self, *, resume_id: str, resume_title: str = "") -> ParseTask:
        """启动一次解析并起后台线程。

        Args:
            resume_id: 简历 id。
            resume_title: 简历标题（展示用）。

        Returns:
            新建的解析任务。

        Raises:
            NotFoundError: 简历不存在。
            ConflictError: 这份简历解析中 / 全局并发已满。
            ValidationWebError: LLM 没配置。
        """
        from .llm_config_store import load_config

        try:
            draft = load_resume(resume_id)
        except FileNotFoundError as exc:
            raise NotFoundError(f"简历不存在：{resume_id}") from exc

        cfg = load_config()
        if not cfg.configured:
            raise ValidationWebError(
                "LLM 还没配置，请先到「模型」页填好 API Key / Base URL / 模型名"
            )

        request = {
            "resume_id": resume_id,
            "resume_title": resume_title or draft.title,
        }
        hub.occupy("parse", request=request)
        task = ParseTask(resume_id=resume_id, resume_title=request["resume_title"])
        with self._lock:
            self._tasks[task.task_id] = task
            self._prune_locked()
        hub.track(
            task,
            task_type="parse",
            request=request,
            keys=[resume_id],
            retry_fn=lambda: self.start(**request),
        )

        thread = threading.Thread(
            target=self._run, args=(task, draft.raw, cfg), daemon=True, name=task.task_id
        )
        thread.start()
        return task

    def cancel(self, task_id: str) -> ParseTask:
        """请求取消；不存在或已结束抛错。"""
        task = self.get(task_id)
        if task.status != STATUS_RUNNING:
            raise ConflictError("解析任务已结束，无法取消")
        task.cancel_flag = True
        hub.notify(task.task_id)
        return task

    def _prune_locked(self) -> None:
        """历史裁到 `KEEP`。"""
        if len(self._tasks) <= self.KEEP:
            return
        finished = [tid for tid, t in self._tasks.items() if t.status != STATUS_RUNNING]
        while len(self._tasks) > self.KEEP and finished:
            self._tasks.pop(finished.pop(0), None)

    def _run(self, task: ParseTask, raw: str, cfg: Any) -> None:
        """线程体：调 LLM 解析并原地写回 `meta.llm`。"""
        try:
            task.push("parse_start", {"resume_id": task.resume_id})
            if task.cancel_flag:
                with task.lock:
                    task.status = STATUS_CANCELLED
                    task.ended_at = time.time()
                return
            with task.lock:
                task.current = "调用 LLM 解析…"
            llm_payload = parse_and_stamp(raw, model=cfg.model, config=cfg)
            if task.cancel_flag:
                with task.lock:
                    task.status = STATUS_CANCELLED
                    task.ended_at = time.time()
                return
            save_llm_parse(task.resume_id, llm_payload)
            with task.lock:
                task.result = llm_payload
                task.current = "解析完成"
                task.status = STATUS_DONE
                task.ended_at = time.time()
            task.push("parse_done", {"resume_id": task.resume_id})
        except ResumeParseError as exc:
            with task.lock:
                task.status = STATUS_ERROR
                task.error = str(exc)
                task.ended_at = time.time()
            task.push("parse_error", {"message": task.error})
        except Exception as exc:  # noqa: BLE001 - 线程里兜住，状态给前端
            logger.exception("解析任务 %s 失败", task.task_id)
            with task.lock:
                task.status = STATUS_ERROR
                task.error = str(exc) or exc.__class__.__name__
                task.ended_at = time.time()
            task.push("parse_error", {"message": task.error})
        finally:
            hub.release(task.task_id)


parse_tasks = ParseTaskManager()
