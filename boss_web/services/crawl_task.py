"""后台抓取任务：线程跑 ``JobClient.crawl``，网页轮询进度。"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from typing import Any, Mapping

from boss_jobs import CrawlBlacklistContext, create_client
from boss_jobs.store import JobStore

from .. import config as C
from ..errors import ConflictError, NotFoundError, UpstreamError
from .blacklist import RuleMatch, evaluate_company, evaluate_jd, save_hit
from .collection_config_store import CollectionSnapshot, load_snapshot
from .troubleshoot import humanize

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
            event = payload.get("event")
            # 终态 / 瞬时事件只进流水，不再累加计数（finished 带的是累计值，会重复加）
            terminal = {"finished", "error", "cancel_requested", "stoken", "rules_snapshot"}
            if event not in terminal:
                # 只有翻页回调才带 page（见 JobClient.crawl 的 _push）；
                # stoken / detail_* 这些不算页
                if payload.get("page") is not None:
                    self.progress["pages"] = self.progress.get("pages", 0) + 1
                for key in (
                    "raw_count", "kept_count", "inserted", "updated", "company_hits",
                    "jd_hits", "blacklist_skipped", "ordinary_stored",
                ):
                    self.progress[key] = self.progress.get(key, 0) + int(payload.get(key, 0) or 0)
            if event == "detail_done":
                self.progress["desc_ok"] = self.progress.get("desc_ok", 0) + (
                    1 if payload.get("has_desc") else 0
                )
                self.progress["desc_done"] = self.progress.get("desc_done", 0) + 1
                if payload.get("jd_status") == "empty":
                    self.progress["jd_empty"] = self.progress.get("jd_empty", 0) + 1
                elif payload.get("jd_status") == "unchecked":
                    self.progress["jd_unchecked"] = self.progress.get("jd_unchecked", 0) + 1
            elif event == "detail_error":
                self.progress["desc_failed"] = self.progress.get("desc_failed", 0) + 1
                self.progress["desc_done"] = self.progress.get("desc_done", 0) + 1
            elif event == "detail_skipped":
                self.progress["desc_skipped"] = self.progress.get("desc_skipped", 0) + 1
            elif event == "company_blacklist_skipped":
                self.progress["blacklist_skipped"] = self.progress.get("blacklist_skipped", 0) + 1
            elif event == "jd_blacklist_hit" and not payload.get("stored", True):
                self.progress["blacklist_skipped"] = self.progress.get("blacklist_skipped", 0) + 1

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
        max_pages: int | None = None,
        interval: float | None = None,
        start_page: int | None = None,
        use_search: bool | None = None,
        fetch_details: bool | None = None,
        detail_interval: float | None = None,
        store_blacklisted: bool | None = None,
    ) -> CrawlTask:
        config_snapshot = load_snapshot()
        saved = config_snapshot.collection
        params = {
            "max_pages": int(saved["max_pages"] if max_pages is None else max_pages),
            "interval": float(saved["page_interval"] if interval is None else interval),
            "start_page": int(saved["start_page"] if start_page is None else start_page),
            "use_search": bool(saved["use_search"] if use_search is None else use_search),
            "fetch_details": bool(saved["fetch_details"] if fetch_details is None else fetch_details),
            "detail_interval": float(saved["detail_interval"] if detail_interval is None else detail_interval),
            "store_blacklisted": bool(saved["store_blacklisted"] if store_blacklisted is None else store_blacklisted),
            "config_snapshot": config_snapshot.to_dict(),
        }
        with self._lock:
            if self._task is not None and self._task.status == ST_RUNNING:
                raise ConflictError("已有抓取任务在跑，等它结束或先取消")
            task = CrawlTask(params)
            task.progress.update({
                "company_hits": 0, "jd_hits": 0, "blacklist_skipped": 0,
                "ordinary_stored": 0, "jd_unchecked": 0, "jd_empty": 0,
            })
            task.push({
                "event": "rules_snapshot",
                "snapshot_id": config_snapshot.snapshot_id,
                "rule_count": len(config_snapshot.rules),
                "company_rules": sum(1 for r in config_snapshot.rules if r.get("rule_type") == "company"),
                "jd_rules": sum(1 for r in config_snapshot.rules if r.get("rule_type") == "jd_keyword"),
            })
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

            from boss_filter.search import JobSearchFilter

            snapshot_data = params.get("config_snapshot") or {}
            search = snapshot_data.get("search") or {}
            search_filter = None
            if params.get("use_search"):
                task.phase = "filter"
                search_filter = JobSearchFilter(
                    query=search.get("query", ""),
                    city=search.get("city", ""),
                    job_type=search.get("jobType", ""),
                    salary=search.get("salary", ""),
                    experience=tuple(search.get("experience") or ()),
                    degree=tuple(search.get("degree") or ()),
                    industry=tuple(search.get("industry") or ()),
                    scale=tuple(search.get("scale") or ()),
                    pay_type=tuple(search.get("payType") or ()),
                    part_time=tuple(search.get("partTime") or ()),
                    stage=tuple(search.get("stage") or ()),
                )
                if search_filter.is_blank:
                    search_filter = None

            if search_filter is not None:
                # 搜索流要 __zp_stoken__，可能拉真 Chrome，先把状态亮出来
                task.phase = "stoken"
                task.push({"event": "stoken", "message": "正在准备搜索令牌（可能拉起 Chrome）…"})

            task.phase = "crawl"
            store = JobStore()
            snapshot_data = params.get("config_snapshot") or {}
            rules = tuple(snapshot_data.get("rules") or ())
            has_jd_rules = any(r.get("rule_type") == "jd_keyword" and r.get("enabled") for r in rules)

            def company_matches(job: Any) -> list[dict[str, Any]]:
                return [m.to_dict() for m in evaluate_company(job.brand_name, rules)]

            def jd_matches(job: Any, text: str) -> list[dict[str, Any]]:
                return [m.to_dict() for m in evaluate_jd(job.job_name, text, rules)]

            def record_hit(job: Any, hit: Mapping[str, Any], *, action: str, jd_status: str) -> None:
                match = RuleMatch(
                    rule={k: v for k, v in hit.items() if k not in {"matched_text", "source"}},
                    matched_text=str(hit.get("matched_text") or ""),
                    source=str(hit.get("source") or ""),
                )
                save_hit(
                    task_id=task.task_id,
                    job_id=job.encrypt_job_id,
                    job_name=job.job_name,
                    brand_name=job.brand_name,
                    match=match,
                    action=action,
                    jd_status=jd_status,
                )

            context = CrawlBlacklistContext(
                task_id=task.task_id,
                store_blacklisted=bool(params.get("store_blacklisted", True)),
                has_jd_rules=has_jd_rules,
                jd_scopes=tuple(
                    str(r.get("scope") or "")
                    for r in rules
                    if r.get("rule_type") == "jd_keyword" and r.get("enabled")
                ),
                company_matches=company_matches,
                jd_matches=jd_matches,
                record_hit=record_hit,
            )
            try:
                report = client.crawl(
                    store=store,
                    max_pages=int(params["max_pages"]),
                    start_page=int(params["start_page"]),
                    search_filter=search_filter,
                    on_progress=task.push,
                    should_stop=lambda: task.cancel_flag,
                    enrich_details=bool(params.get("fetch_details", True)),
                    detail_interval=float(params.get("detail_interval", 1.0)),
                    blacklist_context=context,
                )
            finally:
                store.close()

            with task.lock:
                task.stopped_reason = report.stats.stopped_reason
                task.status = ST_CANCELLED if task.cancel_flag else ST_DONE
                task.ended_at = time.time()
            task.push({"event": "finished", "stopped_reason": report.stats.stopped_reason,
                       "company_hits": report.stats.company_hits, "jd_hits": report.stats.jd_hits,
                       "skipped": report.stats.skipped, "inserted": report.stats.inserted,
                       "updated": report.stats.updated})
        except Exception as exc:  # noqa: BLE001 - 线程里兜住，状态给前端
            logger.exception("抓取任务 %s 失败", task.task_id)
            with task.lock:
                task.status = ST_ERROR
                # 线程不走 FastAPI 的异常处理器，人话化得在这儿做
                task.error = humanize(exc)
                task.ended_at = time.time()
            task.push({"event": "error", "message": task.error})


crawl_tasks = CrawlTaskManager()
