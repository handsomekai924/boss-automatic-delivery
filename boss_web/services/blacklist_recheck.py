"""Re-evaluate stored jobs against a fixed blacklist-rule snapshot."""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from typing import Any, Sequence

from boss_jobs.store import JobStore

from ..errors import ConflictError, NotFoundError
from .blacklist import evaluate_company, evaluate_jd, list_enabled_rules, save_hit

ST_RUNNING = "running"
ST_DONE = "done"
ST_ERROR = "error"
ST_CANCELLED = "cancelled"


class RecheckTask:
    def __init__(self, *, job_ids: Sequence[str], limit: int | None) -> None:
        self.task_id = "br_" + uuid.uuid4().hex[:10]
        self.job_ids = [str(value) for value in job_ids if str(value).strip()]
        self.limit = max(1, min(5000, int(limit))) if limit is not None else None
        self.status = ST_RUNNING
        self.created_at = time.time()
        self.ended_at: float | None = None
        self.error = ""
        self.progress: dict[str, Any] = {
            "total": 0,
            "done": 0,
            "updated": 0,
            "failed": 0,
            "company_hits": 0,
            "jd_hits": 0,
        }
        self.rules_snapshot: tuple[dict[str, Any], ...] = ()
        self.events: deque[dict[str, Any]] = deque(maxlen=100)
        self.cancel_flag = False
        self.lock = threading.Lock()
        #: 本轮已写过审计的 (job_id, rule_id)：同一轮内不重复刷流水
        self.logged_pairs: set[tuple[str, str]] = set()
        #: 本轮已处理过的职位：防止分页抖动 / job_ids 重复导致整条重算
        self.seen_jobs: set[str] = set()

    def push(self, payload: dict[str, Any]) -> None:
        with self.lock:
            self.events.append({"at": time.time(), **payload})

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "task_id": self.task_id,
                "status": self.status,
                "created_at": self.created_at,
                "ended_at": self.ended_at,
                "error": self.error,
                "params": {
                    "job_ids": list(self.job_ids),
                    "limit": self.limit,
                },
                "progress": dict(self.progress),
                "rules_snapshot": [dict(rule) for rule in self.rules_snapshot],
                "events": list(self.events)[-30:],
            }


class RecheckTaskManager:
    """Run one bounded current-state recheck at a time.

    This intentionally only updates ``jobs`` and audit hits. It never loads or
    writes analysis rows, and it never invokes the delivery client.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._task: RecheckTask | None = None

    def current(self) -> RecheckTask | None:
        with self._lock:
            return self._task

    def snapshot(self, task_id: str | None = None) -> dict[str, Any]:
        task = self.current()
        if task is None:
            if task_id:
                raise NotFoundError(f"规则重检任务不存在：{task_id}")
            return {"task_id": None, "status": "idle"}
        if task_id and task.task_id != task_id:
            raise NotFoundError(f"规则重检任务不存在：{task_id}")
        return task.snapshot()

    def start(self, *, job_ids: Sequence[str] = (), limit: int | None = None) -> RecheckTask:
        with self._lock:
            if self._task is not None and self._task.status == ST_RUNNING:
                raise ConflictError("已有规则重检任务在运行")
            task = RecheckTask(job_ids=job_ids, limit=limit)
            # Freeze rules before the worker starts and before returning the task.
            task.rules_snapshot = tuple(list_enabled_rules())
            self._task = task

        thread = threading.Thread(
            target=self._run,
            args=(task,),
            daemon=True,
            name=task.task_id,
        )
        thread.start()
        return task

    def cancel(self) -> RecheckTask:
        task = self.current()
        if task is None:
            raise NotFoundError("没有规则重检任务")
        task.cancel_flag = True
        task.push({"event": "cancel_requested"})
        return task

    def _run(self, task: RecheckTask) -> None:
        store: JobStore | None = None
        try:
            # Freeze the exact enabled rule content before selecting jobs.
            rules = task.rules_snapshot
            has_jd_rules = any(rule.get("rule_type") == "jd_keyword" for rule in rules)
            store = JobStore()
            if task.job_ids:
                # 去重但保序：重复 id 只算一条，进度 total 不虚高
                seen_ids: set[str] = set()
                jobs = []
                for job_id in task.job_ids:
                    if job_id in seen_ids:
                        continue
                    seen_ids.add(job_id)
                    job = store.get_job(job_id)
                    if job is not None:
                        jobs.append(job)
                total = len(jobs)
                job_iter = iter(jobs)
            else:
                total = min(store.count_jobs(), task.limit) if task.limit is not None else store.count_jobs()

                def iter_all_jobs():
                    offset = 0
                    batch_size = 500
                    while offset < total:
                        batch = store.list_jobs(
                            limit=min(batch_size, total - offset), offset=offset, stable=True
                        )
                        if not batch:
                            break
                        yield from batch
                        offset += len(batch)

                job_iter = iter_all_jobs()
            with task.lock:
                task.progress["total"] = total
            task.push({"event": "start", "total": total, "rule_count": len(rules)})

            for job in job_iter:
                if task.cancel_flag:
                    with task.lock:
                        task.status = ST_CANCELLED
                        task.ended_at = time.time()
                    task.push({"event": "cancelled"})
                    return
                if job.encrypt_job_id in task.seen_jobs:
                    # 同一轮里这条已经算过了（job_ids 重复 / 分页抖动），不再重算
                    continue
                task.seen_jobs.add(job.encrypt_job_id)
                try:
                    company_hits = evaluate_company(job.brand_name, rules)
                    jd_sources = [rule.get("scope") for rule in rules if rule.get("rule_type") == "jd_keyword"]
                    can_check_name = any(scope in {"job_name", "both"} for scope in jd_sources)
                    requires_desc = any(scope in {"job_desc", "both"} for scope in jd_sources)
                    can_check_desc = bool(job.job_desc) and requires_desc
                    jd_hits = evaluate_jd(job.job_name, job.job_desc, rules) if (can_check_name or can_check_desc) else []
                    checked = not has_jd_rules or bool(job.job_desc) or (can_check_name and not requires_desc)
                    matched = [hit.to_dict() for hit in company_hits + jd_hits]
                    jd_status = job.jd_status or ("completed" if job.job_desc else "pending")
                    missing_company = not str(job.brand_name or "").strip()
                    blocked = bool(
                        company_hits
                        or jd_hits
                        or missing_company
                        or (has_jd_rules and not checked)
                        or (has_jd_rules and jd_status in {"failed", "pending", "collecting", "unchecked"})
                    )
                    checked_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                    updated = store.update_blacklist_state(
                        job.encrypt_job_id,
                        company_blacklisted=bool(company_hits),
                        jd_blacklisted=bool(jd_hits),
                        matched_rules=matched,
                        jd_rule_checked=checked,
                        jd_status=jd_status,
                        auto_delivery_blocked=blocked,
                        checked_at=checked_at,
                    )
                    for hit in company_hits + jd_hits:
                        pair = (job.encrypt_job_id, str(hit.rule.get("rule_id") or ""))
                        if pair in task.logged_pairs:
                            continue
                        task.logged_pairs.add(pair)
                        save_hit(
                            task_id=task.task_id,
                            job_id=job.encrypt_job_id,
                            job_name=job.job_name,
                            brand_name=job.brand_name,
                            match=hit,
                            action="stored",
                            jd_status=jd_status,
                        )
                    with task.lock:
                        task.progress["done"] += 1
                        task.progress["updated"] += int(updated)
                        task.progress["company_hits"] += len(company_hits)
                        task.progress["jd_hits"] += len(jd_hits)
                    task.push({
                        "event": "item_done",
                        "encrypt_job_id": job.encrypt_job_id,
                        "job_name": job.job_name,
                        "company_hits": len(company_hits),
                        "jd_hits": len(jd_hits),
                        "jd_rule_checked": checked,
                    })
                except Exception as exc:  # noqa: BLE001 - one row must not abort batch
                    with task.lock:
                        task.progress["done"] += 1
                        task.progress["failed"] += 1
                    task.push({
                        "event": "item_error",
                        "encrypt_job_id": job.encrypt_job_id,
                        "job_name": job.job_name,
                        "message": str(exc),
                    })

            with task.lock:
                task.status = ST_DONE
                task.ended_at = time.time()
            task.push({"event": "finished", **dict(task.progress)})
        except Exception as exc:  # noqa: BLE001 - surface task error to UI
            with task.lock:
                task.status = ST_ERROR
                task.error = f"{type(exc).__name__}: {exc}"
                task.ended_at = time.time()
            task.push({"event": "error", "message": task.error})
        finally:
            if store is not None:
                store.close()


recheck_tasks = RecheckTaskManager()
