"""后台抓取任务：线程跑 ``JobClient.crawl``，网页轮询进度。"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from typing import Any

from boss_jobs import create_client
from boss_jobs.store import JobStore

from .. import config as C
from ..errors import ConflictError, NotFoundError, UpstreamError

logger = logging.getLogger(__name__)

ST_RUNNING = "running"
ST_DONE = "done"
ST_ERROR = "error"
ST_CANCELLED = "cancelled"


class CrawlTask:
    def __init__(self, params: dict[str, Any]) -> None:
        self.task_id = uuid.uuid4().hex[:12]
        self.params = params
        self.status = ST_RUNNING
        self.phase = "prepare"
        self.created_at = time.time()
        self.ended_at: float | None = None
        self.error: str | None = None
        self.stopped_reason = ""
        self.progress: dict[str, Any] = {
            "pages": 0,
            "raw_count": 0,
            "kept_count": 0,
            "inserted": 0,
            "updated": 0,
            "desc_done": 0,
            "desc_ok": 0,
            "desc_failed": 0,
            "desc_skipped": 0,
        }
        self.events: deque[dict[str, Any]] = deque(maxlen=200)
        self.cancel_flag = False
        self.lock = threading.Lock()

    def push(self, payload: dict[str, Any]) -> None:
        with self.lock:
            self.events.append({"at": time.time(), **payload})
            # 只有翻页回调才带 page（见 JobClient.crawl 的 _push）；
            # stoken / detail_* / finished / cancel_requested / error 这些不算页
            if payload.get("page") is not None:
                self.progress["pages"] = self.progress.get("pages", 0) + 1
            for key in ("raw_count", "kept_count", "inserted", "updated"):
                self.progress[key] = self.progress.get(key, 0) + int(payload.get(key, 0) or 0)
            # JD 补抓的流水（enrich_details 开着才有）
            event = payload.get("event")
            if event == "detail_done":
                self.progress["desc_ok"] = self.progress.get("desc_ok", 0) + (
                    1 if payload.get("has_desc") else 0
                )
                self.progress["desc_done"] = self.progress.get("desc_done", 0) + 1
            elif event == "detail_error":
                self.progress["desc_failed"] = self.progress.get("desc_failed", 0) + 1
                self.progress["desc_done"] = self.progress.get("desc_done", 0) + 1
            elif event == "detail_skipped":
                # 已有描述，根本没打详情接口——不算 desc_done
                self.progress["desc_skipped"] = self.progress.get("desc_skipped", 0) + 1

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "task_id": self.task_id,
                "status": self.status,
                "phase": self.phase,
                "params": self.params,
                "created_at": self.created_at,
                "ended_at": self.ended_at,
                "error": self.error,
                "stopped_reason": self.stopped_reason,
                "progress": dict(self.progress),
                "events": list(self.events)[-30:],
            }


class CrawlTaskManager:
    """同一时刻只跑一个抓取任务。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._task: CrawlTask | None = None

    def current(self) -> CrawlTask | None:
        with self._lock:
            return self._task

    def snapshot(self, task_id: str | None = None) -> dict[str, Any]:
        task = self.current()
        if task is None:
            return {"task_id": None, "status": "idle"}
        if task_id and task.task_id != task_id:
            raise NotFoundError(f"抓取任务不存在：{task_id}")
        return task.snapshot()

    def start(
        self,
        *,
        max_pages: int = 5,
        interval: float = 1.0,
        start_page: int = 1,
        use_search: bool = True,
        fetch_details: bool = True,
    ) -> CrawlTask:
        with self._lock:
            if self._task is not None and self._task.status == ST_RUNNING:
                raise ConflictError("已有抓取任务在跑，等它结束或先取消")
            task = CrawlTask(
                {
                    "max_pages": max_pages,
                    "interval": interval,
                    "start_page": start_page,
                    "use_search": use_search,
                    "fetch_details": fetch_details,
                }
            )
            self._task = task

        thread = threading.Thread(
            target=self._run, args=(task,), daemon=True, name=f"crawl-{task.task_id}"
        )
        thread.start()
        return task

    def cancel(self, task_id: str | None = None) -> CrawlTask:
        task = self.current()
        if task is None or (task_id and task.task_id != task_id):
            raise NotFoundError("没有在跑的抓取任务")
        task.cancel_flag = True
        task.push({"event": "cancel_requested"})
        return task

    # ------------------------------------------------------------------ #

    def _run(self, task: CrawlTask) -> None:
        params = task.params
        try:
            task.phase = "client"
            client = create_client(page_interval=float(params["interval"]))

            search_filter = None
            if params.get("use_search"):
                from boss_filter import load_search_filter

                task.phase = "filter"
                search_filter = load_search_filter()
                if getattr(search_filter, "is_blank", False):
                    search_filter = None

            if search_filter is not None:
                # 搜索流要 __zp_stoken__，可能拉真 Chrome，先把状态亮出来
                task.phase = "stoken"
                task.push({"event": "stoken", "message": "正在准备搜索令牌（可能拉起 Chrome）…"})

            task.phase = "crawl"
            store = JobStore()
            try:
                report = client.crawl(
                    store=store,
                    max_pages=int(params["max_pages"]),
                    start_page=int(params["start_page"]),
                    search_filter=search_filter,
                    on_progress=task.push,
                    should_stop=lambda: task.cancel_flag,
                    # 抓取时顺带补 JD（已拍板）；单条失败不拖垮列表
                    enrich_details=bool(params.get("fetch_details", True)),
                )
            finally:
                store.close()

            with task.lock:
                task.stopped_reason = report.stats.stopped_reason
                task.status = ST_CANCELLED if task.cancel_flag else ST_DONE
                task.ended_at = time.time()
            task.push({"event": "finished", "stopped_reason": report.stats.stopped_reason})
        except Exception as exc:  # noqa: BLE001 - 线程里兜住，状态给前端
            logger.exception("抓取任务 %s 失败", task.task_id)
            with task.lock:
                task.status = ST_ERROR
                task.error = f"{type(exc).__name__}: {exc}"
                task.ended_at = time.time()
            task.push({"event": "error", "message": task.error})


crawl_tasks = CrawlTaskManager()
