"""手动补抓职位描述（JD）：后台任务 + 进度 + 可取消。

抓取流程里顺带补 JD 是主路径（见 `boss_web.services.crawl_task`）；这份是兜底
——老数据、或某次抓取时详情失败的职位，点一下批量补。单条失败只记流水、
下一条继续。

Attributes:
    ST_RUNNING: 任务运行中。
    ST_DONE: 任务正常结束。
    ST_ERROR: 任务出错。
    ST_CANCELLED: 任务被取消。
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
from .blacklist import RuleMatch, evaluate_jd, list_enabled_rules, save_hit
from .task_runtime import POLICY_GLOBAL_SINGLE, TaskTypeSpec, hub

logger = logging.getLogger(__name__)

ST_RUNNING = "running"
ST_DONE = "done"
ST_ERROR = "error"
ST_CANCELLED = "cancelled"

hub.register(
    TaskTypeSpec(
        "desc",
        title="补抓JD",
        policy=POLICY_GLOBAL_SINGLE,
        mutual_group="fetch",
        title_fn=lambda req: f"补抓JD（{req.get('limit') or '不限'}条）",
    )
)


class DescTask:
    """一次补抓任务的状态、进度与事件流。

    Attributes:
        task_id: 任务 id。
        params: 入参（`limit` / `interval`）。
        status: 任务状态，取 `ST_*`。
        created_at: 创建时间（Unix 秒）。
        ended_at: 结束时间；未结束为 `None`。
        error: 失败原因；成功为 `None`。
        progress: 进度计数与当前条目。
        events: 最近事件环。
        cancel_flag: 是否已请求取消。
        lock: 保护进度 / 状态的锁。
    """

    def __init__(self, params: dict[str, Any]) -> None:
        """初始化。"""
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
            "interval": float(params.get("interval") or DETAIL_INTERVAL),
        }
        self.events: deque[dict[str, Any]] = deque(maxlen=200)
        self.cancel_flag = False
        self.lock = threading.RLock()

    def push(self, payload: dict[str, Any]) -> None:
        """追加一条事件（带时间戳）。

        Args:
            payload: 事件字段，会盖上 `at`。
        """
        with self.lock:
            self.events.append({"at": time.time(), **payload})

    def snapshot(self) -> dict[str, Any]:
        """取一份只含最近 30 条事件的只读快照。"""
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
        """初始化。"""
        self._lock = threading.Lock()
        self._task: DescTask | None = None

    def current(self) -> DescTask | None:
        """当前任务；没有则 `None`。"""
        with self._lock:
            return self._task

    def snapshot(self, task_id: str | None = None) -> dict[str, Any]:
        """取任务快照；指定 id 对不上时抛 `NotFoundError`。

        Args:
            task_id: 可选的期望任务 id；省略则取当前任务。

        Returns:
            任务快照；没有任务时回 `{"task_id": None, "status": "idle"}`。
        """
        task = self.current()
        if task is None:
            return {"task_id": None, "status": "idle"}
        if task_id and task.task_id != task_id:
            raise NotFoundError(f"补抓任务不存在：{task_id}")
        return task.snapshot()

    def start(self, *, limit: int = 0, interval: float | None = None) -> DescTask:
        """启动补抓；已有任务在跑时抛 `ConflictError`。

        Args:
            limit: 最多补几条；`0` = 不限。
            interval: 条间隔（秒）；`None` / `0` 用 `DETAIL_INTERVAL`，
                下限夹到 0.2s。
        """
        if interval is None or interval <= 0:
            interval = DETAIL_INTERVAL
        else:
            interval = max(0.2, float(interval))
        request = {"limit": limit, "interval": interval}
        hub.occupy("desc", request=request)
        with self._lock:
            if self._task is not None and self._task.status == ST_RUNNING:
                raise ConflictError("已有补抓任务在跑，等它结束或先取消")
            task = DescTask(request)
            self._task = task
        hub.track(
            task,
            task_type="desc",
            request=request,
            retry_fn=lambda: self.start(**request),
        )

        thread = threading.Thread(
            target=self._run, args=(task,), daemon=True, name=f"desc-{task.task_id}"
        )
        thread.start()
        return task

    def cancel(self, task_id: str | None = None) -> DescTask:
        """请求取消当前任务；没有在跑的任务时抛 `NotFoundError`。

        Args:
            task_id: 可选的期望任务 id。
        """
        task = self.current()
        if task is None or (task_id and task.task_id != task_id):
            raise NotFoundError("没有在跑的补抓任务")
        task.cancel_flag = True
        task.push({"event": "cancel_requested"})
        return task

    def _run(self, task: DescTask) -> None:
        """后台逐条补抓并写库、记流水。"""
        limit = int(task.params.get("limit") or 0)
        interval = float(task.progress.get("interval") or DETAIL_INTERVAL)
        try:
            client = create_client()
            store = JobStore()
            rules = tuple(list_enabled_rules())
            has_jd_rules = any(r.get("rule_type") == "jd_keyword" for r in rules)
            try:
                missing = store.list_jobs_missing_desc(limit=limit or 10_000)
                with task.lock:
                    task.progress["total"] = len(missing)
                task.push({"event": "start", "total": len(missing)})

                consecutive_37 = 0
                for i, job in enumerate(missing):
                    if task.cancel_flag:
                        with task.lock:
                            task.status = ST_CANCELLED
                            task.ended_at = time.time()
                        task.push({"event": "cancelled"})
                        return

                    if job.job_desc or (job.detail_fetched_at and job.jd_status != "failed"):
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
                        fetched_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                        jd_status = "completed" if detail.has_desc else "empty"
                        jd_sources = [
                            rule.get("scope")
                            for rule in rules
                            if rule.get("rule_type") == "jd_keyword"
                        ]
                        can_check_name = any(scope in {"job_name", "both"} for scope in jd_sources)
                        requires_desc = any(scope in {"job_desc", "both"} for scope in jd_sources)
                        can_check_desc = bool(detail.has_desc and requires_desc)
                        can_check_jd = can_check_name or can_check_desc
                        hits = evaluate_jd(job.job_name, detail.job_desc, rules) if can_check_jd else []
                        checked = not has_jd_rules or can_check_jd and (detail.has_desc or not requires_desc)
                        matched = [hit.to_dict() for hit in hits]
                        missing_company = not str(job.brand_name or "").strip()
                        blocked = bool(
                            job.company_blacklisted
                            or hits
                            or missing_company
                            or (has_jd_rules and not checked)
                            or (has_jd_rules and jd_status in {"failed", "pending", "collecting", "unchecked"})
                        )
                        store.update_job_desc(
                            job.encrypt_job_id,
                            detail.job_desc,
                            fetched_at=fetched_at,
                            jd_status=jd_status,
                            jd_error="",
                            jd_rule_checked=checked,
                            jd_rule_checked_at=fetched_at if checked else "",
                        )
                        store.update_blacklist_state(
                            job.encrypt_job_id,
                            company_blacklisted=job.company_blacklisted,
                            jd_blacklisted=bool(hits),
                            matched_rules=[rule for rule in job.matched_rule_snapshot if rule.get("rule_type") == "company"] + matched,
                            jd_rule_checked=checked,
                            jd_status=jd_status,
                            auto_delivery_blocked=blocked,
                            checked_at=fetched_at,
                        )
                        for hit in hits:
                            save_hit(
                                task_id=task.task_id,
                                job_id=job.encrypt_job_id,
                                job_name=job.job_name,
                                brand_name=job.brand_name,
                                match=RuleMatch(hit.rule, hit.matched_text, hit.source),
                                action="stored",
                                jd_status=jd_status,
                            )
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
                        failed_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                        store.update_job_desc(
                            job.encrypt_job_id,
                            "",
                            fetched_at=failed_at,
                            jd_status="failed",
                            jd_error=str(exc),
                            jd_rule_checked=False,
                            jd_rule_checked_at="",
                        )
                        missing_company = not str(job.brand_name or "").strip()
                        store.update_blacklist_state(
                            job.encrypt_job_id,
                            company_blacklisted=job.company_blacklisted,
                            jd_blacklisted=bool(job.jd_blacklisted),
                            matched_rules=[rule for rule in job.matched_rule_snapshot],
                            jd_rule_checked=False,
                            jd_status="failed",
                            auto_delivery_blocked=bool(
                                job.company_blacklisted
                                or job.jd_blacklisted
                                or missing_company
                                or has_jd_rules
                            ),
                            checked_at=failed_at,
                        )
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
        finally:
            hub.release(task.task_id)


desc_tasks = DescTaskManager()
