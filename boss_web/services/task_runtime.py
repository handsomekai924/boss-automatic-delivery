"""统一任务运行时：任务模型、并发策略、事件总线（WS 广播）。

九套长任务骨架收敛到这一份：各业务 Manager 只负责执行体与落库，
任务登记 / 占用判定 / 进度广播 / 任务中心快照都从这里走。
设计见 `docs/task-center-and-unified-ws.md`。

注意：`track()` 会把业务任务的 `push()` / `set_status()` 包一层，改完立刻
调 `snapshot()` 广播。所以**任务对象自己的锁必须是 `threading.RLock`**——
业务代码常常持锁调 `push()`，包一层后会同线程再进 `snapshot()`；`Lock`
会当场死锁（`LoginTask` 一开始就用的 `RLock`，其余任务对象已对齐）。

Attributes:
    STATUS_PENDING: 已提交未开跑。
    STATUS_RUNNING: 运行中。
    STATUS_WAITING_HUMAN: 等人工喂料（登录滑块/验证码）。
    STATUS_DONE: 正常结束。
    STATUS_ERROR: 出错结束。
    STATUS_CANCELLED: 已取消。
    UNFINISHED: 未结束状态集合（含登录人机流的中间态）——占用判定只看这几个。
    DEFAULT_KEEP: 任务中心历史保留条数。
    POLICY_GLOBAL_SINGLE: 并发策略。
    hub: 进程内唯一 Hub。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from typing import Any, Callable

from ..errors import ConflictError, NotFoundError

logger = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_WAITING_HUMAN = "waiting_human"
STATUS_DONE = "done"
STATUS_ERROR = "error"
STATUS_CANCELLED = "cancelled"

UNFINISHED = frozenset({
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_WAITING_HUMAN,
    "sending_sms",
    "need_code",
    "need_slider",
    "logging_in",
    "need_scan",
})
FINISHED = frozenset({STATUS_DONE, STATUS_ERROR, STATUS_CANCELLED})

DEFAULT_KEEP = 100

POLICY_GLOBAL_SINGLE = "global_single"
POLICY_PER_KEY_SINGLE = "per_key_single"
POLICY_UNLIMITED = "unlimited"


class TaskTypeSpec:
    """一类任务的登记信息。

    Attributes:
        task_type: 任务类型键（`crawl` / `match` / …）。
        title: 任务中心展示名。
        policy: 并发策略，取 `POLICY_*`。
        key_fn: `PER_KEY_SINGLE` 时从启动参数算占用 key；可回多个（面试题按岗位占用）。
        max_parallel: `UNLIMITED` 时的并发上限（按站点计）；`None` = 不限。
        mutual_group: 互斥组——同站点同组任务同时只能跑一个（抓取/补抓共用站点通道）。
        title_fn: 从启动参数算任务卡标题。
        site: 这类任务走哪个站点的通道；空 = 共用（简历解析这类不碰站点的）。
    """

    def __init__(
        self,
        task_type: str,
        *,
        title: str,
        policy: str = POLICY_GLOBAL_SINGLE,
        key_fn: Callable[[dict[str, Any]], "str | list[str]"] | None = None,
        max_parallel: int | None = None,
        mutual_group: str = "",
        title_fn: Callable[[dict[str, Any]], str] | None = None,
        site: str = "",
    ) -> None:
        """初始化。"""
        self.task_type = task_type
        self.title = title
        self.policy = policy
        self.key_fn = key_fn
        self.max_parallel = max_parallel
        self.mutual_group = mutual_group or task_type
        self.title_fn = title_fn
        self.site = site


def spec_keys(spec: TaskTypeSpec, request: dict[str, Any]) -> list[str]:
    """算一次启动要占的 key 列表；`GLOBAL_SINGLE`/`UNLIMITED` 回 `[""]`。"""
    if spec.policy != POLICY_PER_KEY_SINGLE or spec.key_fn is None:
        return [""]
    raw = spec.key_fn(request)
    if isinstance(raw, str):
        return [raw] if raw else [""]
    return [str(k) for k in raw if str(k)] or [""]


def site_of(spec: TaskTypeSpec, request: dict[str, Any]) -> str:
    """算这次启动走哪个站点的通道；与 `track()` 的站点解析同一条规则。

    占用一律按 `(site, key)` 记账：站点是通道边界，同型任务按站点排队
    （docs/multiplatform-console-redesign.md 6.5）。站点为空 = 共用任务，
    退化成原来的全局语义，所以简历解析这类不碰站点的行为不变。
    """
    return str(request.get("site") or "") or spec.site


class TaskRecord:
    """Hub 里的一条任务记录：业务任务对象 + 登记元数据。

    Attributes:
        task_id: 任务 id（取业务任务对象的）。
        task_type: 类型键。
        request: 启动参数快照（重试用）。
        keys: 占用 key 列表（`PER_KEY_SINGLE`）。
        title: 任务卡标题。
        ref: 业务任务对象（`CrawlTask` / `MatchTask` / …）。
        retry_fn: 重试入口（可空）。
        site: 站点 id；空 = 共用。进快照与 WS 信封，供前端按站点过滤/分账。
    """

    def __init__(
        self,
        *,
        task_id: str,
        task_type: str,
        request: dict[str, Any],
        keys: list[str],
        title: str,
        ref: Any,
        retry_fn: Callable[[], Any] | None = None,
        site: str = "",
    ) -> None:
        """初始化。"""
        self.task_id = task_id
        self.task_type = task_type
        self.request = dict(request)
        self.keys = list(keys) or [""]
        self.title = title
        self.ref = ref
        self.retry_fn = retry_fn
        self.site = site


class TaskHub:
    """全任务中心：占用表 + 历史 + WS 广播。

    进程内单例（模块底部 `hub`）。业务 Manager 在 `start()` 里先
    `occupy()` 拿槽，造完任务再 `track()`；跑完 `release()`。
    """

    def __init__(self) -> None:
        """初始化。"""
        self._lock = threading.RLock()
        self._specs: dict[str, TaskTypeSpec] = {}
        self._records: dict[str, TaskRecord] = {}
        self._order: list[str] = []
        self._subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []

    def register(self, spec: TaskTypeSpec) -> None:
        """登记一类任务；重复登记覆盖（方便测试打补丁）。"""
        with self._lock:
            self._specs[spec.task_type] = spec

    def spec(self, task_type: str) -> TaskTypeSpec:
        """取类型登记；未登记回一个宽松默认。"""
        with self._lock:
            return self._specs.get(task_type) or TaskTypeSpec(task_type, title=task_type)

    def _unfinished(self, task_type: str) -> list[TaskRecord]:
        """某类型下所有未结束任务。"""
        out: list[TaskRecord] = []
        for tid in self._order:
            rec = self._records.get(tid)
            if rec and rec.task_type == task_type:
                status = _status_of(rec.ref)
                if status in UNFINISHED:
                    out.append(rec)
        return out

    def occupy(self, task_type: str, *, request: dict[str, Any] | None = None) -> list[str]:
        """提交前的占用检查；冲突抛 `ConflictError`，通过则回占用 key 列表。

        占用按 `(site, key)` 记账——站点是通道边界，同型任务按站点排队
        （docs 6.5）；站点为空的共用任务退化成原来的全局语义。

        - `GLOBAL_SINGLE`：同站点该类型已有未结束任务就拒；
        - `PER_KEY_SINGLE`：同站点同 key 未结束就拒；
        - `UNLIMITED`：只看 `max_parallel`（按站点计）；
        - 互斥组：同站点同 `mutual_group` 里其它类型有未结束任务也拒
          （抓取/补抓共用**站点**通道，跨站点互不干扰）。

        Args:
            task_type: 任务类型键。
            request: 启动参数，用来算占用 key 与站点。

        Returns:
            本次占用的 key 列表。

        Raises:
            ConflictError: 并发冲突。
        """
        spec = self.spec(task_type)
        req = dict(request or {})
        keys = spec_keys(spec, req)
        site = site_of(spec, req)
        with self._lock:
            group = spec.mutual_group
            for tid in self._order:
                rec = self._records.get(tid)
                if rec is None:
                    continue
                other = self.spec(rec.task_type)
                status = _status_of(rec.ref)
                if status not in UNFINISHED:
                    continue
                if rec.site != site:
                    continue  # 别的站点的通道，不归这里管
                if rec.task_type != task_type:
                    if other.mutual_group == group:
                        raise ConflictError(
                            f"已有{other.title}任务在跑，等它结束或先取消"
                        )
                    continue
                if spec.policy == POLICY_GLOBAL_SINGLE:
                    raise ConflictError(f"已有{spec.title}任务在跑，等它结束或先取消")
                if spec.policy == POLICY_PER_KEY_SINGLE:
                    overlap = set(rec.keys) & set(keys)
                    if overlap:
                        raise ConflictError(
                            f"已有{spec.title}任务在跑，不能重复提交：" + "、".join(sorted(overlap)[:5])
                        )

            if spec.policy == POLICY_PER_KEY_SINGLE and keys == [""]:
                raise ConflictError(f"{spec.title}任务缺少占用 key")

            if spec.policy == POLICY_UNLIMITED and spec.max_parallel:
                same_site = [
                    r for r in self._unfinished(task_type) if r.site == site
                ]
                if len(same_site) >= spec.max_parallel:
                    raise ConflictError(
                        f"{spec.title}任务并发已满（{spec.max_parallel}），稍后再试"
                    )
        return keys

    def track(
        self,
        task: Any,
        *,
        task_type: str,
        request: dict[str, Any] | None = None,
        keys: list[str] | None = None,
        title: str = "",
        retry_fn: Callable[[], Any] | None = None,
        site: str = "",
    ) -> TaskRecord:
        """把业务任务对象登记进 Hub，并广播 `task.progress`。

        会把业务对象的 `push()` 包一层，推完事件自动广播进度。

        Args:
            task: 业务任务对象，需有 `task_id` / `snapshot()`。
            task_type: 类型键。
            request: 启动参数快照。
            keys: 占用 key 列表。
            title: 任务卡标题；空则按类型模板算。
            retry_fn: 重试入口。
            site: 站点 id；省略按「启动参数里的 site → 任务类型自述」兜底。

        Returns:
            登记后的记录。
        """
        spec = self.spec(task_type)
        req = dict(request or {})
        rec = TaskRecord(
            task_id=str(getattr(task, "task_id", "") or uuid.uuid4().hex[:12]),
            task_type=task_type,
            request=req,
            keys=list(keys) if keys is not None else spec_keys(spec, req),
            title=title or (spec.title_fn(req) if spec.title_fn else spec.title),
            ref=task,
            retry_fn=retry_fn,
            site=site or site_of(spec, req),
        )
        with self._lock:
            self._records[rec.task_id] = rec
            self._order.append(rec.task_id)
            self._prune_locked()
        self._wrap_push(task, rec.task_id)
        self.emit_progress(rec)
        return rec

    def release(self, task_id: str) -> None:
        """任务结束：广播终态（占用靠状态判定，自动释放）。"""
        with self._lock:
            rec = self._records.get(task_id)
        if rec is None:
            return
        self.emit_finished(rec)

    def reset(self) -> None:
        """清空全部任务记录（测试隔离用）。

        占用判定看的是任务对象自身状态，记录清掉后旧线程就算还在跑
        也不会再拦新提交；旧线程收尾时 `release` 找不到记录直接返回。
        """
        with self._lock:
            self._records.clear()
            self._order.clear()

    def _prune_locked(self) -> None:
        """历史裁到 `DEFAULT_KEEP`，只裁已结束的。"""
        if len(self._order) <= DEFAULT_KEEP:
            return
        finished_ids = [
            tid
            for tid in self._order
            if tid in self._records and _status_of(self._records[tid].ref) in FINISHED
        ]
        while len(self._order) > DEFAULT_KEEP and finished_ids:
            tid = finished_ids.pop(0)
            if tid in self._order:
                self._order.remove(tid)
                self._records.pop(tid, None)

    def get(self, task_id: str) -> TaskRecord:
        """按 id 取记录；不存在抛 `NotFoundError`。"""
        with self._lock:
            rec = self._records.get(task_id)
        if rec is None:
            raise NotFoundError(f"任务不存在：{task_id}")
        return rec

    def snapshot(self, task_id: str) -> dict[str, Any]:
        """单任务规范化快照。"""
        return _normalize(self.get(task_id))

    def list_snapshots(
        self, *, task_type: str = "", status: str = "", site: str | None = None
    ) -> dict[str, Any]:
        """任务中心列表。

        Args:
            task_type: 按类型过滤；空 = 全部。
            site: 按站点过滤；`None` = 全部，`""` = 只看共用任务。
            status: 按状态过滤；`running` 等价于「未结束」；空 = 全部。

        Returns:
            `{running_count, failed_count, by_type, tasks[]}`，新到旧。
        """
        with self._lock:
            recs = [self._records[t] for t in reversed(self._order) if t in self._records]
        tasks = [_normalize(r) for r in recs]
        if task_type:
            tasks = [t for t in tasks if t["task_type"] == task_type]
        if site is not None:
            tasks = [t for t in tasks if t.get("site", "") == site]
        if status == "running":
            tasks = [t for t in tasks if t["status"] in UNFINISHED]
        elif status == "finished":
            tasks = [t for t in tasks if t["status"] in FINISHED]
        elif status:
            tasks = [t for t in tasks if t["status"] == status]
        by_type: dict[str, int] = {}
        for t in tasks:
            if t["status"] in UNFINISHED:
                by_type[t["task_type"]] = by_type.get(t["task_type"], 0) + 1
        return {
            "running_count": sum(1 for t in tasks if t["status"] in UNFINISHED),
            "failed_count": sum(1 for t in tasks if t["status"] == STATUS_ERROR),
            "by_type": by_type,
            "tasks": tasks,
        }

    def cancel(self, task_id: str) -> dict[str, Any]:
        """请求取消；已有 `cancel()` 的用之，否则置 `cancel_flag`。"""
        rec = self.get(task_id)
        ref = rec.ref
        status = _status_of(ref)
        if status not in UNFINISHED:
            raise ConflictError("任务已结束，无法取消")
        cancel = getattr(ref, "cancel_flag", None)
        if hasattr(ref, "request_cancel") and callable(ref.request_cancel):
            ref.request_cancel()
        elif cancel is not None:
            ref.cancel_flag = True
            push = getattr(ref, "push", None)
            if callable(push):
                try:
                    push({"event": "cancel_requested"})
                except TypeError:
                    push("cancel_requested", {"source": "hub"})
        self.emit_progress(rec)
        return _normalize(rec)

    def retry(self, task_id: str) -> dict[str, Any]:
        """按原参数重跑一条已结束任务；没有 `retry_fn` 抛 `ConflictError`。"""
        rec = self.get(task_id)
        if _status_of(rec.ref) in UNFINISHED:
            raise ConflictError("任务还在跑，不能重复提交")
        if rec.retry_fn is None:
            raise ConflictError("这个任务不支持重试，请重新提交")
        result = rec.retry_fn()
        new_id = str(getattr(result, "task_id", "") or "")
        if new_id and new_id in self._records:
            return _normalize(self._records[new_id])
        return {"task_id": new_id or None, "status": STATUS_RUNNING}

    def delete(self, task_id: str) -> dict[str, Any]:
        """删除一条已结束任务记录（不影响产物落库）。"""
        rec = self.get(task_id)
        if _status_of(rec.ref) in UNFINISHED:
            raise ConflictError("任务还在跑，请先停止再删除")
        with self._lock:
            self._records.pop(task_id, None)
            if task_id in self._order:
                self._order.remove(task_id)
        self.broadcast("task.removed", {"task_id": task_id, "task_type": rec.task_type})
        return {"task_id": task_id, "removed": True}

    def cancel_all(self) -> int:
        """取消所有未结束任务，返回取消条数。"""
        with self._lock:
            recs = [self._records[t] for t in list(self._order) if t in self._records]
        n = 0
        for rec in recs:
            if _status_of(rec.ref) in UNFINISHED:
                try:
                    self.cancel(rec.task_id)
                    n += 1
                except Exception:  # noqa: BLE001 - 逐条尽力而为
                    logger.debug("取消任务失败：%s", rec.task_id, exc_info=True)
        return n

    def subscribe(self, loop: asyncio.AbstractEventLoop) -> asyncio.Queue:
        """订阅事件队列（WS 连接生命周期）。"""
        queue: asyncio.Queue = asyncio.Queue(maxsize=128)
        with self._lock:
            self._subscribers.append((loop, queue))
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """摘掉订阅。"""
        with self._lock:
            self._subscribers[:] = [(lp, q) for lp, q in self._subscribers if q is not queue]

    def broadcast(
        self,
        msg_type: str,
        data: dict[str, Any],
        *,
        task_type: str = "",
        task_id: str = "",
        site: str = "",
    ) -> None:
        """线程安全广播一帧 WS 消息。信封带 `site`：空 = 共用事件。"""
        payload = {
            "type": msg_type,
            "ts": time.time(),
            "task_type": task_type,
            "task_id": task_id,
            "site": site,
            "data": data,
        }
        with self._lock:
            targets = list(self._subscribers)
        for loop, queue in targets:
            def _put(q: asyncio.Queue = queue, p: dict[str, Any] = payload) -> None:
                """把一帧塞进订阅队列；满了丢最旧保最新。"""
                try:
                    q.put_nowait(p)
                except asyncio.QueueFull:  # pragma: no cover
                    try:
                        q.get_nowait()
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        q.put_nowait(p)
                    except Exception:  # noqa: BLE001
                        pass

            try:
                loop.call_soon_threadsafe(_put)
            except RuntimeError:  # pragma: no cover - loop 已关
                pass

    def emit_progress(self, rec: TaskRecord) -> None:
        """广播某任务的进度快照。"""
        try:
            snap = _normalize(rec)
        except Exception:  # noqa: BLE001
            return
        self.broadcast(
            "task.progress",
            snap,
            task_type=rec.task_type,
            task_id=rec.task_id,
            site=rec.site,
        )

    def emit_finished(self, rec: TaskRecord) -> None:
        """广播某任务的终态快照。"""
        try:
            snap = _normalize(rec)
        except Exception:  # noqa: BLE001
            return
        self.broadcast(
            "task.finished",
            snap,
            task_type=rec.task_type,
            task_id=rec.task_id,
            site=rec.site,
        )

    def notify(self, task_id: str, *, finished: bool | None = None) -> None:
        """业务任务 push/结束时的对外钩子。

        Args:
            task_id: 任务 id。
            finished: 显式指定是否终态；`None` 则按状态自动判断。
        """
        with self._lock:
            rec = self._records.get(task_id)
        if rec is None:
            return
        if finished is None:
            finished = _status_of(rec.ref) in FINISHED
        if finished:
            self.emit_finished(rec)
        else:
            self.emit_progress(rec)
        self._emit_auth_hint(rec)

    def _emit_auth_hint(self, rec: TaskRecord) -> None:
        """登录任务的人机喂料提示（`auth.need_*` / `auth.state`）。"""
        if rec.task_type != "login":
            return
        status = _status_of(rec.ref)
        try:
            snap = rec.ref.snapshot() if callable(getattr(rec.ref, "snapshot", None)) else {}
        except Exception:  # noqa: BLE001
            snap = {}
        data = {
            "task_id": rec.task_id,
            "status": status,
            "phone_masked": (snap or {}).get("phone_masked", ""),
            "slider": (snap or {}).get("slider", {}),
            "qr": (snap or {}).get("qr", {}),
            "error": (snap or {}).get("error"),
        }
        mapping = {
            "need_code": "auth.need_code",
            "need_slider": "auth.need_slider",
            "need_scan": "auth.need_scan",
            "done": "auth.state",
            "error": "auth.state",
            "cancelled": "auth.state",
        }
        msg_type = mapping.get(status, "auth.state")
        self.broadcast(msg_type, data, task_type="login", task_id=rec.task_id, site=rec.site)
        if msg_type == "auth.state":
            try:
                from .login_task import login_tasks

                self.broadcast(
                    "session.changed",
                    login_tasks.status_payload(),
                    task_type="login",
                    task_id=rec.task_id,
                    site=rec.site,
                )
            except Exception:  # noqa: BLE001 - 会话推送失败不影响任务本身
                logger.debug("session.changed 广播失败", exc_info=True)

    def hello_payload(self) -> dict[str, Any]:
        """WS 连接建立时的全量快照。"""
        return self.list_snapshots()

    def _wrap_push(self, task: Any, task_id: str) -> None:
        """把业务任务的 `push()` / `set_status()` 包一层，变更后自动广播进度。"""
        hub_self = self

        def _notify() -> None:
            """广播一次进度。"""
            try:
                hub_self.notify(task_id)
            except Exception:  # noqa: BLE001 - 广播失败不影响业务
                logger.debug("任务进度广播失败：%s", task_id, exc_info=True)

        orig_push = getattr(task, "push", None)
        if callable(orig_push) and not getattr(task, "_hub_push_wrapped", False):

            def _push(*args: Any, **kwargs: Any) -> Any:
                """先落事件环，再广播。"""
                result = orig_push(*args, **kwargs)
                _notify()
                return result

            try:
                task.push = _push  # type: ignore[attr-defined]
                task._hub_push_wrapped = True  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass

        orig_status = getattr(task, "set_status", None)
        if callable(orig_status) and not getattr(task, "_hub_status_wrapped", False):

            def _set_status(*args: Any, **kwargs: Any) -> Any:
                """改状态后立刻广播（登录喂料要靠这个推）。"""
                result = orig_status(*args, **kwargs)
                _notify()
                return result

            try:
                task.set_status = _set_status  # type: ignore[attr-defined]
                task._hub_status_wrapped = True  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass


def _status_of(ref: Any) -> str:
    """从业务任务对象上取状态字符串。"""
    return str(getattr(ref, "status", "") or "")


def _normalize(rec: TaskRecord) -> dict[str, Any]:
    """业务快照 → 任务中心统一形状（原样附在 `raw`）。"""
    try:
        raw = rec.ref.snapshot() if callable(getattr(rec.ref, "snapshot", None)) else {}
    except Exception:  # noqa: BLE001
        raw = {}
    raw = dict(raw or {})
    progress = dict(raw.get("progress") or {})
    done = raw.get("done", progress.get("done", 0)) or 0
    total = raw.get("total", progress.get("total", 0)) or 0
    if not total and raw.get("pages") is not None:
        total = raw.get("total_pages") or 0
    if not done and progress.get("pages") is not None:
        done = progress.get("pages") or 0
        total = total or progress.get("max_pages") or 0
    current = str(
        raw.get("current")
        or raw.get("current_job")
        or progress.get("current")
        or raw.get("phase")
        or ""
    )
    return {
        "task_id": rec.task_id,
        "task_type": rec.task_type,
        "site": rec.site,
        "title": rec.title,
        "status": _status_of(rec.ref) or STATUS_RUNNING,
        "current": current,
        "done": int(done or 0),
        "total": int(total or 0),
        "error": raw.get("error"),
        "created_at": raw.get("created_at"),
        "ended_at": raw.get("ended_at"),
        "events": list(raw.get("events") or [])[-20:],
        "raw": raw,
    }


hub = TaskHub()
