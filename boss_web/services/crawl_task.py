"""后台抓取任务：线程跑 `JobClient.crawl`，网页轮询进度。

除了内存里的「当前任务」，每次 `start()` 还会落一行 `crawl_tasks` 档案。
落库失败不中断抓取，详见 `notes/boss_web/services/crawl_task.md`。

Attributes:
    ST_RUNNING: 任务状态：进行中。
    ST_DONE: 任务状态：完成。
    ST_ERROR: 任务状态：失败。
    ST_CANCELLED: 任务状态：已取消。
    PHASE_TEXT: `CrawlTask.phase` 内部阶段代号 → 人话（任务中心「当前」列、页面小标共用）。
    BLANK_SEARCH_MSG: 搜索流条件全空时的报错。
"""

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
from ..errors import ConflictError, NotFoundError, UpstreamError, ValidationWebError
from .blacklist import RuleMatch, evaluate_company, evaluate_jd, save_hit
from .collection_config_store import CollectionSnapshot, load_snapshot
from .task_runtime import POLICY_GLOBAL_SINGLE, TaskTypeSpec, hub
from .troubleshoot import humanize

logger = logging.getLogger(__name__)

ST_RUNNING = "running"
ST_DONE = "done"
ST_ERROR = "error"
ST_CANCELLED = "cancelled"

PHASE_TEXT = {
    "prepare": "准备中",
    "client": "连接中",
    "filter": "按条件筛选",
    "stoken": "验证身份",
    "crawl": "翻页搜索",
}

hub.register(
    TaskTypeSpec(
        "crawl",
        title="职位爬取",
        policy=POLICY_GLOBAL_SINGLE,
        mutual_group="fetch",
        title_fn=lambda req: "职位爬取",
    )
)

BLANK_SEARCH_MSG = (
    "采集条件是空的：到「系统配置 → 采集配置」填关键词或筛选维度，或改用求职期望"
)


def _search_filter_from(
    search: Mapping[str, Any], *, encrypt_expect_id: str = ""
) -> "JobSearchFilter":
    """从 snapshot 的 search payload 拼一次搜索流条件。

    期望流分支也用它当基底（再交给 `JobExpectFilter.from_search` 搬维度）。

    Args:
        search: snapshot 里的 search payload。
        encrypt_expect_id: 期望上下文 id。

    Returns:
        搜索流条件对象。
    """
    from boss_filter.search import JobSearchFilter

    return JobSearchFilter(
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
        encrypt_expect_id=encrypt_expect_id,
    )


def _archive_start(
    task_id: str,
    *,
    source: str,
    created_at: float,
    params: Mapping[str, Any],
) -> bool:
    """启动时插一行 `crawl_tasks`。落库失败只告警，不挡抓取本体。

    Args:
        task_id: 任务 id。
        source: 来源标识（expect / search）。
        created_at: 创建时间戳。
        params: 任务参数。

    Returns:
        是否落库成功。
    """
    try:
        with JobStore() as store:
            store.create_crawl_task(
                task_id,
                source=source,
                created_at=created_at,
                started_at=created_at,
                params=dict(params),
            )
        return True
    except Exception:  # noqa: BLE001 - 历史记录未保存不该拖垮抓取
        logger.warning("抓取任务 %s 的历史记录未保存（启动落库失败）", task_id, exc_info=True)
        return False


def _archive_finish(
    task: "CrawlTask",
    *,
    status: str,
    error: str = "",
) -> None:
    """结束时回写状态与统计。落库失败只告警。

    Args:
        task: 抓取任务。
        status: 终态。
        error: 错误文案。
    """
    try:
        progress = dict(task.progress)
        with JobStore() as store:
            store.finish_crawl_task(
                task.task_id,
                status=status,
                ended_at=task.ended_at or time.time(),
                stats=progress,
                error=error or "",
                stopped_reason=task.stopped_reason or "",
                job_count=int(progress.get("kept_count") or 0),
                inserted_count=int(progress.get("inserted") or 0),
                updated_count=int(progress.get("updated") or 0),
            )
    except Exception:  # noqa: BLE001
        logger.warning("抓取任务 %s 的历史记录未保存（结束落库失败）", task.task_id, exc_info=True)


def _archive_stats(task: "CrawlTask") -> None:
    """进行中任务的中间统计落库（描述补抓 / 黑名单继续累进）。

    Args:
        task: 抓取任务。
    """
    try:
        with JobStore() as store:
            store.touch_crawl_task_stats(task.task_id, dict(task.progress))
    except Exception:  # noqa: BLE001
        logger.debug("抓取任务 %s 中间统计落库失败", task.task_id, exc_info=True)


def _progress_with_archive(task: "CrawlTask"):
    """包一层 `task.push`：每页入库后顺手把中间统计落库。

    Args:
        task: 抓取任务。

    Returns:
        进度回调函数。
    """

    def _on_progress(payload: dict[str, Any]) -> None:
        """转发进度并在翻页时落中间统计。"""
        task.push(payload)
        if payload.get("page") is not None:
            _archive_stats(task)

    return _on_progress


class CrawlTask:
    """内存里的抓取任务状态与事件流水。

    Attributes:
        task_id: 任务 id。
        params: 启动参数。
        status: 任务状态。
        phase: 当前阶段。
        created_at: 创建时间戳。
        ended_at: 结束时间戳。
        error: 错误文案。
        stopped_reason: 停止原因。
        progress: 累计进度计数。
        events: 最近事件流水。
        cancel_flag: 取消标记。
        lock: 状态锁。
    """

    def __init__(self, params: dict[str, Any]) -> None:
        """初始化。"""
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
        self.lock = threading.RLock()

    def push(self, payload: dict[str, Any]) -> None:
        """记一条事件并按事件类型累进计数。

        Args:
            payload: 事件载荷。
        """
        with self.lock:
            self.events.append({"at": time.time(), **payload})
            event = payload.get("event")
            terminal = {"finished", "error", "cancel_requested", "stoken", "rules_snapshot"}
            if event not in terminal:
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
        """当前状态副本。

        `current` / `done` / `total` 是任务中心卡片直接读的三件套：阶段给
        人话，进度按页数算（详情补抓阶段页数已满，进度条停在 100% 继续跑）。

        Returns:
            含 task_id / status / current / done / total / progress / 最近事件的 dict。
        """
        with self.lock:
            return {
                "task_id": self.task_id,
                "status": self.status,
                "phase": self.phase,
                "current": PHASE_TEXT.get(self.phase, self.phase),
                "done": int(self.progress.get("pages") or 0),
                "total": int(self.params.get("max_pages") or 0),
                "params": self.params,
                "created_at": self.created_at,
                "ended_at": self.ended_at,
                "error": self.error,
                "stopped_reason": self.stopped_reason,
                "progress": dict(self.progress),
                "events": list(self.events)[-30:],
            }


class CrawlTaskManager:
    """同一时刻只跑一个抓取任务。

    Attributes:
        _lock: 任务槽锁。
        _task: 当前任务。
    """

    def __init__(self) -> None:
        """初始化。"""
        self._lock = threading.Lock()
        self._task: CrawlTask | None = None

    def current(self) -> CrawlTask | None:
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
        """启动一次抓取并起后台线程。

        Args:
            max_pages: 最大页数。
            interval: 翻页间隔。
            start_page: 起始页。
            use_search: 兼容字段，现一律按搜索流处理。
            fetch_details: 是否补抓描述。
            detail_interval: 详情间隔。
            store_blacklisted: 黑名单命中是否入库。

        Returns:
            新建的抓取任务。

        Raises:
            ValidationWebError: 搜索流条件全空。
            ConflictError: 已有抓取任务在跑。
        """
        config_snapshot = load_snapshot()
        saved = config_snapshot.collection
        if use_search is False:
            logger.warning("旧「推荐页」配置已移除，改用关键词搜索")
        params = {
            "max_pages": int(saved["max_pages"] if max_pages is None else max_pages),
            "interval": float(saved["page_interval"] if interval is None else interval),
            "start_page": int(saved["start_page"] if start_page is None else start_page),
            "use_search": True,
            "fetch_details": bool(saved["fetch_details"] if fetch_details is None else fetch_details),
            "detail_interval": float(saved["detail_interval"] if detail_interval is None else detail_interval),
            "store_blacklisted": bool(saved["store_blacklisted"] if store_blacklisted is None else store_blacklisted),
            "config_snapshot": config_snapshot.to_dict(),
        }
        if not str(saved.get("expect_kind") or "").strip():
            if _search_filter_from(config_snapshot.search).is_blank:
                raise ValidationWebError(BLANK_SEARCH_MSG)
        source = "expect" if str(saved.get("expect_kind") or "").strip() else "search"
        hub.occupy("crawl", request=params)
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
        hub.track(
            task,
            task_type="crawl",
            request=params,
            retry_fn=lambda: self.start(
                max_pages=params.get("max_pages"),
                interval=params.get("interval"),
                start_page=params.get("start_page"),
                fetch_details=params.get("fetch_details"),
                detail_interval=params.get("detail_interval"),
                store_blacklisted=params.get("store_blacklisted"),
            ),
        )

        _archive_start(task.task_id, source=source, created_at=task.created_at, params=params)

        thread = threading.Thread(
            target=self._run, args=(task,), daemon=True, name=f"crawl-{task.task_id}"
        )
        thread.start()
        return task

    def cancel(self, task_id: str | None = None) -> CrawlTask:
        """请求取消当前任务。

        Args:
            task_id: 可选，校验是否就是当前任务。

        Returns:
            被取消的任务。

        Raises:
            NotFoundError: 没有在跑的抓取任务。
        """
        task = self.current()
        if task is None or (task_id and task.task_id != task_id):
            raise NotFoundError("没有在跑的抓取任务")
        task.cancel_flag = True
        task.push({"event": "cancel_requested"})
        return task

    def _run(self, task: CrawlTask) -> None:
        """线程体：建客户端、选流、跑 crawl 并回写终态。

        Args:
            task: 抓取任务。
        """
        params = task.params
        try:
            task.phase = "client"
            client = create_client(page_interval=float(params["interval"]))

            from boss_filter.expect import JobExpectFilter

            snapshot_data = params.get("config_snapshot") or {}
            search = snapshot_data.get("search") or {}
            collection = snapshot_data.get("collection") or {}
            expect_kind = str(collection.get("expect_kind") or "").strip()
            encrypt_expect_id = str(
                collection.get("encrypt_expect_id") or search.get("encryptExpectId") or ""
            ).strip()
            mix_expect_type = str(collection.get("mix_expect_type") or "").strip()
            expect_info = str(collection.get("expect_info") or "").strip()

            expect_filter = None
            search_filter = None
            if expect_kind:
                task.phase = "filter"
                expect_filter = JobExpectFilter.from_search(
                    _search_filter_from(search),
                    kind=expect_kind,
                    encrypt_expect_id=encrypt_expect_id,
                    mix_expect_type=mix_expect_type,
                    expect_info=expect_info,
                )
            else:
                task.phase = "filter"
                search_filter = _search_filter_from(
                    search, encrypt_expect_id=encrypt_expect_id
                )
                if search_filter.is_blank:
                    raise ValueError(BLANK_SEARCH_MSG)

            task.phase = "stoken"
            task.push({"event": "stoken", "message": "正在准备搜索令牌（可能拉起 Chrome）…"})

            task.phase = "crawl"
            store = JobStore()
            snapshot_data = params.get("config_snapshot") or {}
            rules = tuple(snapshot_data.get("rules") or ())
            has_jd_rules = any(r.get("rule_type") == "jd_keyword" and r.get("enabled") for r in rules)

            def company_matches(job: Any) -> list[dict[str, Any]]:
                """公司名黑名单命中结果。"""
                return [m.to_dict() for m in evaluate_company(job.brand_name, rules)]

            def jd_matches(job: Any, text: str) -> list[dict[str, Any]]:
                """职位描述黑名单命中结果。"""
                return [m.to_dict() for m in evaluate_jd(job.job_name, text, rules)]

            def record_hit(job: Any, hit: Mapping[str, Any], *, action: str, jd_status: str) -> None:
                """把一次黑名单命中写库。"""
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
                    expect_filter=expect_filter,
                    on_progress=_progress_with_archive(task),
                    should_stop=lambda: task.cancel_flag,
                    enrich_details=bool(params.get("fetch_details", True)),
                    detail_interval=float(params.get("detail_interval", 1.0)),
                    blacklist_context=context,
                    task_id=task.task_id,
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
            _archive_finish(task, status=task.status)
        except Exception as exc:  # noqa: BLE001 - 线程里兜住，状态给前端
            logger.exception("抓取任务 %s 失败", task.task_id)
            with task.lock:
                task.status = ST_ERROR
                task.error = humanize(exc)
                task.ended_at = time.time()
            task.push({"event": "error", "message": task.error})
            _archive_finish(task, status=ST_ERROR, error=task.error or "")
        finally:
            hub.release(task.task_id)


crawl_tasks = CrawlTaskManager()
