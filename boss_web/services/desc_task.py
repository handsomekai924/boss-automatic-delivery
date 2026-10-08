"""手动补抓职位描述（JD）：后台任务 + 进度 + 可取消。

抓取流程里顺带补 JD 是主路径（见 :mod:`boss_web.services.crawl_task`）；
这份是兜底——老数据、或某次抓取时详情失败的职位，点一下批量补。

单条失败只记流水、下一条继续，绝不拖垮整批（跟列表抓取同一条规矩）。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from typing import Any

from boss_jobs import create_client
from boss_jobs.config import BROWSER_CHECK_COOLOFF, BROWSER_CHECK_GIVEUP, DETAIL_INTERVAL
from boss_jobs.errors import JobApiError
from boss_jobs.store import JobStore

from ..errors import ConflictError, NotFoundError

logger = logging.getLogger(__name__)

ST_RUNNING = "running"
ST_DONE = "done"
ST_ERROR = "error"
ST_CANCELLED = "cancelled"


class DescTask:
    def __init__(self, params: dict[str, Any]) -> None:
        self.task_id = "desc_" + uuid.uuid4().hex[:10]
        self.params = params
        self.status = ST_RUNNING
        self.created_at = time.time()
        self.ended_at: float | None = None
        self.error: str | None = None
        self.progress: dict[str, Any] = {
            "total": 0,
            "done": 0,
            "ok": 0,
            "failed": 0,
            "skipped": 0,
            "already": 0,
            "current": "",
            # 条间隔（秒），给前端算「还要多久」用；默认跟 DETAIL_INTERVAL 一致
            "interval": float(params.get("interval") or DETAIL_INTERVAL),
        }
        self.events: deque[dict[str, Any]] = deque(maxlen=200)
        self.cancel_flag = False
        self.lock = threading.Lock()

    def push(self, payload: dict[str, Any]) -> None:
        with self.lock:
            self.events.append({"at": time.time(), **payload})

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "task_id": self.task_id,
                "status": self.status,
                "params": self.params,
                "created_at": self.created_at,
                "ended_at": self.ended_at,
                "error": self.error,
                "progress": dict(self.progress),
                "events": list(self.events)[-30:],
            }


class DescTaskManager:
    """同一时刻只跑一个补抓任务。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._task: DescTask | None = None

    def current(self) -> DescTask | None:
        with self._lock:
            return self._task

    def snapshot(self, task_id: str | None = None) -> dict[str, Any]:
        task = self.current()
        if task is None:
            return {"task_id": None, "status": "idle"}
        if task_id and task.task_id != task_id:
            raise NotFoundError(f"补抓任务不存在：{task_id}")
        return task.snapshot()

    def start(self, *, limit: int = 0, interval: float | None = None) -> DescTask:
        """:param interval: 条间隔（秒）；``None``/``0`` = 用 :data:`DETAIL_INTERVAL`。
        下限夹到 0.2s，免得前端手滑把风控敲出来。"""
        if interval is None or interval <= 0:
            interval = DETAIL_INTERVAL
        else:
            interval = max(0.2, float(interval))
        with self._lock:
            if self._task is not None and self._task.status == ST_RUNNING:
                raise ConflictError("已有补抓任务在跑，等它结束或先取消")
            task = DescTask({"limit": limit, "interval": interval})
            self._task = task

        thread = threading.Thread(
            target=self._run, args=(task,), daemon=True, name=f"desc-{task.task_id}"
        )
        thread.start()
        return task

    def cancel(self, task_id: str | None = None) -> DescTask:
        task = self.current()
        if task is None or (task_id and task.task_id != task_id):
            raise NotFoundError("没有在跑的补抓任务")
        task.cancel_flag = True
        task.push({"event": "cancel_requested"})
        return task

    # ------------------------------------------------------------------ #

    def _run(self, task: DescTask) -> None:
        limit = int(task.params.get("limit") or 0)
        interval = float(task.progress.get("interval") or DETAIL_INTERVAL)
        try:
            client = create_client()
            store = JobStore()
            try:
                missing = store.list_jobs_missing_desc(limit=limit or 10_000)
                with task.lock:
                    task.progress["total"] = len(missing)
                task.push({"event": "start", "total": len(missing)})

                # 连续撞安全网关的计数：连环 N 次就整批停（见 BROWSER_CHECK_GIVEUP）
                consecutive_37 = 0
                for i, job in enumerate(missing):
                    if task.cancel_flag:
                        with task.lock:
                            task.status = ST_CANCELLED
                            task.ended_at = time.time()
                        task.push({"event": "cancelled"})
                        return

                    # 兜底：列表本来就没带这些，但真跑起来再确认一次——
                    # 已有描述的不再重复获取。
                    if job.job_desc or job.detail_fetched_at:
                        with task.lock:
                            task.progress["done"] += 1
                            task.progress["already"] += 1
                        task.push(
                            {
                                "event": "item_skipped",
                                "encrypt_job_id": job.encrypt_job_id,
                                "job_name": job.job_name,
                                "reason": "已有描述",
                            }
                        )
                        continue

                    with task.lock:
                        task.progress["current"] = f"{job.job_name} @ {job.brand_name}"
                    try:
                        detail = client.fetch_job_detail(
                            security_id=job.security_id, lid=job.lid
                        )
                        store.update_job_desc(job.encrypt_job_id, detail.job_desc)
                        ok = True
                        consecutive_37 = 0
                    except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮整批
                        logger.warning(
                            "补抓描述失败 %s / %s：%s",
                            job.encrypt_job_id,
                            job.job_name,
                            exc,
                        )
                        detail = None
                        ok = False
                        task.push(
                            {
                                "event": "item_error",
                                "encrypt_job_id": job.encrypt_job_id,
                                "job_name": job.job_name,
                                "message": str(exc),
                            }
                        )
                        if isinstance(exc, JobApiError) and exc.is_browser_check:
                            consecutive_37 += 1
                            if consecutive_37 >= BROWSER_CHECK_GIVEUP:
                                # 整段被限速了：别再砸，停下来等人歇几分钟
                                msg = (
                                    f"连续 {consecutive_37} 次撞安全网关 code 37，"
                                    "已停；过几分钟再试"
                                )
                                logger.warning("补抓任务 %s：%s", task.task_id, msg)
                                with task.lock:
                                    task.status = ST_DONE
                                    task.ended_at = time.time()
                                    task.progress["current"] = msg
                                    task.progress["done"] += 1
                                    task.progress["failed"] += 1
                                task.push({"event": "stopped", "reason": msg})
                                return
                            # 限速墙抬手前多躺一会儿，别拿下一条去探墙
                            time.sleep(BROWSER_CHECK_COOLOFF)

                    with task.lock:
                        task.progress["done"] += 1
                        if ok:
                            if detail and detail.has_desc:
                                task.progress["ok"] += 1
                            else:
                                task.progress["skipped"] += 1
                        else:
                            task.progress["failed"] += 1
                    if ok:
                        task.push(
                            {
                                "event": "item_done",
                                "encrypt_job_id": job.encrypt_job_id,
                                "job_name": job.job_name,
                                "has_desc": bool(detail and detail.has_desc),
                            }
                        )
                    # 最后一条不睡，省掉尾部那一秒
                    if interval > 0 and i < len(missing) - 1:
                        time.sleep(interval)

                with task.lock:
                    task.status = ST_DONE
                    task.ended_at = time.time()
                task.push({"event": "finished", **dict(task.progress)})
            finally:
                store.close()
        except Exception as exc:  # noqa: BLE001 - 线程里兜住，状态给前端
            logger.exception("补抓任务 %s 失败", task.task_id)
            with task.lock:
                task.status = ST_ERROR
                task.error = f"{type(exc).__name__}: {exc}"
                task.ended_at = time.time()
            task.push({"event": "error", "message": task.error})


desc_tasks = DescTaskManager()
