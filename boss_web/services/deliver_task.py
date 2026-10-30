"""一键投递任务：把匹配结果里的目标岗位串行打招呼并把招呼语发出去。

一键 / 多条 / 单条三种入口只差目标集合，任务本身只认一串 `encrypt_job_id`。
串行发送、`delivered_at` 有值跳过不重发、单条失败只记 `deliver_error` 不自动
重试；结果写回 `analysis` 的 `deliver_status` / `delivered_at` / `deliver_error`。
条间间隔与每日发对话配额从 `site_settings` 取——限速是**站点属性**不是全局
常量，`throttle_ms` 配了就盖掉 `DELIVER_INTERVAL`，`daily_quota` 到顶整批不再发。
协议与风控细节见 `notes/boss_web/services/deliver_task.md`。

Attributes:
    SITE: 发送通道所属站点；`site_settings` 的节流/每日配额按它取。
    ST_RUNNING: 任务运行中。
    ST_DONE: 任务正常结束。
    ST_ERROR: 任务出错或被停批。
    ST_CANCELLED: 任务被取消。
    GREET_COOLDOWN_DAYS: 发送冷却天数。
    GREET_COOLDOWN_SECONDS: 发送冷却窗（秒）。
    GREET_COOLDOWN_SKIP_FMT: 跳过原因模板；日期 MM-DD 由调用方填。
    ITEM_SKIPPED: 跳过单条时的状态字面量。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from typing import Any, Callable, Mapping, Sequence

from boss_jobs import create_client
from boss_jobs.config import (
    BROWSER_CHECK_COOLOFF,
    BROWSER_CHECK_GIVEUP,
    DELIVER_INTERVAL,
)
from boss_jobs.errors import ChatSendError, JobApiError
from boss_jobs.store import JobStore

from ..errors import ConflictError, NotFoundError, ValidationWebError
from .resume_store import (
    DELIVER_FAILED,
    DELIVER_OK,
    DELIVER_SENDING,
    load_analysis,
    update_delivery,
)
from .deliver_config_store import load_config as load_deliver_config
from .site_settings_store import limits_payload, load_site_limits
from .task_runtime import POLICY_GLOBAL_SINGLE, TaskTypeSpec, hub
from .troubleshoot import humanize

logger = logging.getLogger(__name__)

ST_RUNNING = "running"

#: 发送通道所属站点：任务占用、`site_settings` 的节流/配额都按它分账。
SITE = "boss"

hub.register(
    TaskTypeSpec(
        "deliver",
        title="批量发送",
        policy=POLICY_GLOBAL_SINGLE,
        title_fn=lambda req: f"批量发送（{len(req.get('job_ids') or [])}条）",
        site=SITE,  # todo3 Stage 1：随本模块一起搬进 boss-platform-boss，届时由 adapter 自述
    )
)
ST_DONE = "done"
ST_ERROR = "error"
ST_CANCELLED = "cancelled"

GREET_COOLDOWN_DAYS = 30
GREET_COOLDOWN_SECONDS = GREET_COOLDOWN_DAYS * 86400

GREET_COOLDOWN_SKIP_FMT = "30 天内已发送（{date}）"


def parse_greet_time(value: Any) -> float | None:
    """`jobs.last_greeted_at` / `item.delivered_at` → `time.time()` 同口径的秒。

    数字（含数字字符串）当 Unix 时间戳；`%Y-%m-%d %H:%M:%S` 等按本地时钟解析，
    不做时区换算。读不到 / 解析失败 → `None`（按「从未发送」处理）。
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            ts = float(value)
        except (TypeError, ValueError):
            return None
        return ts if ts > 0 else None
    text = str(value).strip()
    if not text:
        return None
    try:
        ts = float(text)
    except ValueError:
        ts = 0.0
    if ts > 0:
        return ts
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(text, fmt))
        except ValueError:
            continue
    return None


def greet_cooldown_state(
    last_greeted_at: Any,
    delivered_at: Any = None,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """30 天冷却判定（单一口径）。

    `last_greeted_at`（岗位维度）与 `delivered_at`（本条分析的即时状态）取
    较晚者，再套 30 天窗；恰好 30 天整允许再发。

    Args:
        last_greeted_at: 岗位维度最近发送时间。
        delivered_at: 本条分析的即时发送时间。
        now: 判定时刻；省略用当前时间。

    Returns:
        `{last_greeted_ts, last_greeted_at, cooldown_until, in_cooldown,
        cooldown_days_left}`。
    """
    stamps = [ts for ts in (parse_greet_time(last_greeted_at), parse_greet_time(delivered_at)) if ts is not None]
    last_ts = max(stamps) if stamps else None
    if last_ts is None:
        return {
            "last_greeted_ts": None,
            "last_greeted_at": "",
            "cooldown_until": None,
            "in_cooldown": False,
            "cooldown_days_left": 0,
        }
    now_ts = time.time() if now is None else float(now)
    cooldown_until = last_ts + GREET_COOLDOWN_SECONDS
    remaining = cooldown_until - now_ts
    in_cooldown = remaining > 0
    return {
        "last_greeted_ts": last_ts,
        "last_greeted_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_ts)),
        "cooldown_until": cooldown_until,
        "in_cooldown": in_cooldown,
        "cooldown_days_left": max(0, int((remaining + 86399) // 86400)) if in_cooldown else 0,
    }


def greet_cooldown_reason(state: Mapping[str, Any]) -> str:
    """冷却中 → 跳过原因 `30 天内已发送（MM-DD）`；否则空串。

    Args:
        state: `greet_cooldown_state` 的结果。
    """
    if not state.get("in_cooldown"):
        return ""
    ts = parse_greet_time(state.get("last_greeted_ts") or state.get("last_greeted_at"))
    date = time.strftime("%m-%d", time.localtime(ts)) if ts else "??-??"
    return GREET_COOLDOWN_SKIP_FMT.format(date=date)


def enrich_match_greet_state(items: list[dict[str, Any]], *, now: float | None = None) -> None:
    """读接口出门前给 match 条目补当前发送史与冷却状态（就地改）。

    真正算冷却要看 `jobs` 表现在的 `last_greeted_at` 与 `item.delivered_at`
    取较晚者；读不到就按「从未发送」放行。

    Args:
        items: match 条目列表。
        now: 判定时刻；省略用当前时间。
    """
    from boss_jobs.store import JobStore

    if not items:
        return
    ids = [str(it.get("encrypt_job_id") or "") for it in items if it.get("encrypt_job_id")]
    fresh: dict[str, str] = {}
    if ids:
        try:
            with JobStore() as store:
                fresh = store.last_greeted_map(ids)
        except Exception:  # noqa: BLE001 - 读不到就用快照值
            logger.debug("批量读发送史失败，退回快照", exc_info=True)
    now_ts = time.time() if now is None else float(now)
    for item in items:
        jid = str(item.get("encrypt_job_id") or "")
        snapshot = item.get("last_greeted_at") or ""
        current = fresh.get(jid, "") or snapshot
        state = greet_cooldown_state(current, item.get("delivered_at"), now=now_ts)
        item["last_greeted_at"] = state["last_greeted_at"] or current or ""
        item["greet_in_cooldown"] = bool(state["in_cooldown"])
        item["greet_cooldown_until"] = state["cooldown_until"]
        item["greet_cooldown_days_left"] = state["cooldown_days_left"]


_SAFETY_FIELDS = (
    "is_blacklisted", "company_blacklisted", "jd_blacklisted", "jd_rule_checked",
    "jd_status", "auto_delivery_blocked", "matched_rule_snapshot",
)


def _has_safety_snapshot(item: dict[str, Any]) -> bool:
    """必备安全快照字段是否齐全。"""
    return all(key in item for key in _SAFETY_FIELDS)


def _delivery_restriction(item: dict[str, Any], *, automatic: bool = False) -> str:
    """从匹配时安全快照得出稳定拦截原因；可发回空串。

    Args:
        item: match 条目。
        automatic: 是否自动 / 全量发送；旧分析缺快照时只拦自动发送。
    """
    if not _has_safety_snapshot(item):
        return "缺少匹配时职位安全快照" if automatic else ""
    if item.get("company_blacklisted"):
        return "公司黑名单职位"
    if item.get("jd_blacklisted"):
        return "JD 关键词黑名单职位"
    if not item.get("jd_rule_checked", False):
        return "JD 规则未检查"
    if str(item.get("jd_status") or "") in {"failed", "pending", "collecting", "unchecked"}:
        return "JD 状态不完整"
    if item.get("auto_delivery_blocked"):
        return "职位状态不完整，禁止自动投递"
    return ""


def _all_restriction(item: dict[str, Any]) -> str:
    """自动 / 全量批量绝不能发的原因；可发回空串。"""
    return _delivery_restriction(item, automatic=True) or (
        "没有招呼语正文" if not str(item.get("greeting") or "").strip() else ""
    )


ITEM_SKIPPED = "skipped"


class DeliverTask:
    """一次投递任务的状态、单条结果流水与事件流。

    Attributes:
        mode: `all` / `selected` / `manual`。
        confirmed: 是否已确认受限职位。
        confirmation_token: 手动发送的确认令牌。
        items: 每个目标的状态流水，按 `encrypt_job_id` 贴回结果行。
        events: 最近事件环。
        cancel_flag: 是否已请求取消。
        limits: 本次生效的站点限速/配额（`site_settings`），只读视图用。
        stopped_reason: 整批停住 / 剩下的没发的原因；没停 = 空串。跟
            `flow.DeliverReport.stopped_reason` 同一语义，投递页直接显示它。
    """

    def __init__(
        self,
        analysis_id: str,
        job_ids: Sequence[str],
        *,
        mode: str = "selected",
        confirmed: bool = False,
        confirmation_token: str = "",
    ) -> None:
        """初始化。"""
        self.task_id = "dv_" + uuid.uuid4().hex[:10]
        self.analysis_id = analysis_id
        self.job_ids = list(job_ids)
        self.mode = mode
        self.confirmed = bool(confirmed)
        self.confirmation_token = confirmation_token
        self.status = ST_RUNNING
        self.created_at = time.time()
        self.ended_at: float | None = None
        self.error: str | None = None
        self.current = ""
        self.total = 0
        self.done = 0
        self.ok = 0
        self.failed = 0
        self.skipped = 0
        self.items: list[dict[str, Any]] = []
        self.events: deque[dict[str, Any]] = deque(maxlen=100)
        self.cancel_flag = False
        #: 本次生效的站点限速/配额（`site_settings`），任务中心只读视图用。
        self.limits: dict[str, Any] = {}
        #: 整批停住 / 剩下的没发的原因（`flow.DeliverReport.stopped_reason` 同义）。
        self.stopped_reason: str = ""
        self.lock = threading.RLock()

    def push(self, name: str, payload: dict[str, Any] | None = None) -> None:
        """追加一条命名事件（带时间戳）。

        Args:
            name: 事件名。
            payload: 事件字段。
        """
        with self.lock:
            self.events.append({"at": time.time(), "name": name, "payload": payload or {}})

    def add_item(
        self,
        encrypt_job_id: str,
        *,
        job_name: str = "",
        brand_name: str = "",
        status: str = DELIVER_SENDING,
        error: str = "",
        delivered_at: float | None = None,
    ) -> dict[str, Any]:
        """记一条单目标结果流水。

        Args:
            encrypt_job_id: 职位 id。
            job_name: 职位名。
            brand_name: 公司名。
            status: 结果状态。
            error: 失败 / 跳过原因。
            delivered_at: 发送时间。

        Returns:
            写入的流水字典。
        """
        item = {
            "encrypt_job_id": encrypt_job_id,
            "job_name": job_name,
            "brand_name": brand_name,
            "status": status,
            "error": error,
            "delivered_at": delivered_at,
        }
        with self.lock:
            self.items.append(item)
        return item

    def snapshot(self) -> dict[str, Any]:
        """取一份只含最近 20 条事件的只读快照。"""
        with self.lock:
            return {
                "task_id": self.task_id,
                "analysis_id": self.analysis_id,
                "status": self.status,
                "mode": self.mode,
                "confirmed": self.confirmed,
                "confirmation_token": self.confirmation_token,
                "created_at": self.created_at,
                "ended_at": self.ended_at,
                "error": self.error,
                "current": self.current,
                "total": self.total,
                "done": self.done,
                "ok": self.ok,
                "failed": self.failed,
                "skipped": self.skipped,
                "items": list(self.items),
                "limits": dict(self.limits),
                "stopped_reason": self.stopped_reason,
                "events": list(self.events)[-20:],
            }


class DeliverTaskManager:
    """同一时刻只跑一个发送任务。

    Args:
        client_factory: 造 `JobClient` 的工厂（测试里注入假的）。
        sleeper: 可替换的 sleep（测试里注入 no-op）。
        interval: 条间隔（秒），默认 `boss_jobs.config.DELIVER_INTERVAL`；
            `site_settings.throttle_ms` 配了就盖掉它。
    """

    def __init__(
        self,
        *,
        client_factory: Callable[[], Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        interval: float | None = None,
    ) -> None:
        """初始化。"""
        self._lock = threading.Lock()
        self._task: DeliverTask | None = None
        self._client_factory = client_factory or (lambda: create_client())
        self._sleep = sleeper
        self._interval = DELIVER_INTERVAL if interval is None else interval

    def current(self) -> DeliverTask | None:
        """当前任务；没有则 `None`。"""
        with self._lock:
            return self._task

    def snapshot(self, task_id: str | None = None) -> dict[str, Any]:
        """取任务快照；指定 id 对不上时抛 `NotFoundError`。

        Args:
            task_id: 可选的期望任务 id。

        Returns:
            任务快照；没有任务时回 `{"task_id": None, "status": "idle"}`。
        """
        task = self.current()
        if task is None:
            return {"task_id": None, "status": "idle"}
        if task_id and task.task_id != task_id:
            raise NotFoundError(f"发送任务不存在：{task_id}")
        return task.snapshot()

    def start(
        self,
        *,
        analysis_id: str,
        job_ids: Sequence[str],
        mode: str = "selected",
        confirmed: bool = False,
        confirmation_token: str = "",
    ) -> DeliverTask:
        """启动发送任务；已有任务在跑时抛 `ConflictError`。

        Args:
            analysis_id: 所属分析 id。
            job_ids: 目标职位 id 列表。
            mode: `all` / `selected` / `manual`。
            confirmed: 是否已确认受限职位。
            confirmation_token: 手动发送的确认令牌。
        """
        if mode not in {"all", "selected", "manual"}:
            raise ValueError("mode 必须是 all、selected 或 manual")
        request = {
            "analysis_id": analysis_id,
            "job_ids": list(job_ids),
            "mode": mode,
            "confirmed": confirmed,
            "confirmation_token": confirmation_token,
        }
        hub.occupy("deliver", request=request)
        with self._lock:
            if self._task is not None and self._task.status == ST_RUNNING:
                raise ConflictError("已有发送任务在跑，等它结束或先取消")
            task = DeliverTask(
                analysis_id,
                job_ids,
                mode=mode,
                confirmed=confirmed,
                confirmation_token=confirmation_token,
            )
            self._task = task
        hub.track(task, task_type="deliver", request=request, retry_fn=lambda: self.start(**request))

        thread = threading.Thread(target=self._run, args=(task,), daemon=True, name=task.task_id)
        thread.start()
        return task

    def cancel(self, task_id: str | None = None) -> DeliverTask:
        """请求取消当前任务；没有在跑的任务时抛 `NotFoundError`。

        Args:
            task_id: 可选的期望任务 id。
        """
        task = self.current()
        if task is None or (task_id and task.task_id != task_id):
            raise NotFoundError("没有在跑的发送任务")
        task.cancel_flag = True
        task.push("cancel_requested")
        return task

    @staticmethod
    def _cooldown_for(item: dict[str, Any], store: JobStore) -> dict[str, Any]:
        """一条 match 的冷却判定：库里发送史与 `item.delivered_at` 取较晚者。

        Args:
            item: match 条目。
            store: 职位库，读 `last_greeted_at`。
        """
        jid = str(item.get("encrypt_job_id") or "")
        raw_job = ""
        if store is not None and jid:
            try:
                raw_job = store.get_last_greeted_at(jid)
            except Exception:  # noqa: BLE001 - 读不到就放行
                logger.debug("读不到职位 %s 的发送史", jid, exc_info=True)
        snapshot = item.get("last_greeted_at") or ""
        return greet_cooldown_state(raw_job or snapshot, item.get("delivered_at"))

    def _run(self, task: DeliverTask) -> None:
        """串行发送：已成功的跳过；单条失败记 `deliver_error` 继续，不重试。"""
        try:
            payload = load_analysis(task.analysis_id)
        except FileNotFoundError:
            self._fail(task, f"分析结果不存在：{task.analysis_id}")
            return

        matches = payload.get("matches")
        if not isinstance(matches, list):
            matches = []
        index = {
            str(m.get("encrypt_job_id")): m
            for m in matches
            if isinstance(m, dict) and m.get("encrypt_job_id")
        }

        minimum = load_deliver_config().min_score if task.mode == "all" else None
        candidate_ids = list(index) if task.mode == "all" else task.job_ids
        todo: list[dict[str, Any]] = []
        restricted: list[dict[str, Any]] = []
        try:
            store = JobStore()
        except Exception as exc:  # noqa: BLE001 - 库起不来就整批失败
            self._fail(task, humanize(exc))
            return
        try:
            for jid in candidate_ids:
                item = index.get(jid)
                if item is None:
                    logger.warning("分析 %s 里没有职位 %s，跳过", task.analysis_id, jid)
                    continue
                cooldown = self._cooldown_for(item, store)
                if cooldown.get("in_cooldown"):
                    reason = greet_cooldown_reason(cooldown)
                    self._record(
                        task,
                        item,
                        status=ITEM_SKIPPED,
                        error=reason,
                        delivered_at=cooldown.get("last_greeted_ts"),
                    )
                    task.push("skipped", {"encrypt_job_id": jid, "reason": reason})
                    continue
                restriction = _delivery_restriction(item)
                if task.mode == "all":
                    restriction = _all_restriction(item)
                    if not restriction and minimum is not None and (item.get("match_score") or 0) < minimum:
                        restriction = f"匹配分低于一键发送阈值 {minimum}"
                    if restriction:
                        self._record(task, item, status=ITEM_SKIPPED, error=restriction)
                        task.push("skipped", {"encrypt_job_id": jid, "reason": restriction})
                        continue
                elif restriction:
                    restricted.append(item)
                todo.append(item)
        except Exception:
            store.close()
            raise

        if restricted and (not task.confirmed or (task.mode == "manual" and not task.confirmation_token.strip())):
            reasons = sorted({
                _delivery_restriction(item) or "受限职位"
                for item in restricted
            })
            message = "发送清单包含受限职位，必须确认后才能发送：" + "、".join(reasons)
            for item in restricted:
                self._record(
                    task,
                    item,
                    status=ITEM_SKIPPED,
                    error="未确认受限职位：" + (_delivery_restriction(item) or "受限职位"),
                )
                task.push(
                    "skipped",
                    {
                        "encrypt_job_id": item.get("encrypt_job_id"),
                        "reason": _delivery_restriction(item) or "受限职位",
                        "requires_confirmation": True,
                    },
                )
            with task.lock:
                task.status = ST_ERROR
                task.error = message
                task.stopped_reason = message
                task.ended_at = time.time()
            task.push("confirmation_required", {"message": message, "count": len(restricted)})
            store.close()
            return

        # 站点限速/配额（site_settings）：限速是站点属性，条间隔与每日上限照它办
        settings, sent_today = load_site_limits(SITE)
        interval = settings.throttle_seconds or self._interval
        quota_left = settings.quota_left(sent_today)
        with task.lock:
            task.limits = {"interval": interval, **limits_payload(settings, sent_today)}
        if quota_left is not None and len(todo) > quota_left:
            reason = f"今日发对话配额已用完（{settings.daily_quota} 条），未发送"
            with task.lock:
                task.stopped_reason = reason
            for item in todo[quota_left:]:
                self._record(task, item, status=ITEM_SKIPPED, error=reason)
                task.push(
                    "skipped",
                    {"encrypt_job_id": item.get("encrypt_job_id"), "reason": reason},
                )
            todo = todo[:quota_left]
            task.push("quota_exhausted", {"daily_quota": settings.daily_quota})

        task.total = len(todo)
        if not todo:
            with task.lock:
                task.status = ST_DONE
                task.ended_at = time.time()
            task.push("finished", {"sent": 0, "skipped": task.skipped})
            store.close()
            return

        try:
            client = self._client_factory()
        except Exception as exc:  # noqa: BLE001 - 客户端起不来就整批失败
            store.close()
            self._fail(task, humanize(exc))
            return

        consecutive_37 = 0
        try:
            for item in todo:
                if task.cancel_flag:
                    with task.lock:
                        task.status = ST_CANCELLED
                    break

                jid = str(item.get("encrypt_job_id"))
                name = str(item.get("job_name") or "")
                with task.lock:
                    task.current = name
                task.push("send_start", {"encrypt_job_id": jid, "job_name": name})

                job = store.get_job(jid)
                security_id = (job.security_id if job else "") or str(item.get("security_id") or "")
                lid = (job.lid if job else "") or str(item.get("lid") or "")
                encrypt_boss_id = job.encrypt_boss_id if job else ""

                if not security_id:
                    self._record(
                        task, item, status=DELIVER_FAILED, error="职位缺少 securityId，无法打招呼"
                    )
                    task.done += 1
                    continue

                greeting = str(item.get("greeting") or "").strip()
                if not greeting:
                    self._record(
                        task,
                        item,
                        status=DELIVER_FAILED,
                        error="没有招呼语正文，未发送（先在匹配页补一句再发）",
                    )
                    task.done += 1
                    continue

                try:
                    client.deliver_greeting(
                        security_id=security_id,
                        encrypt_job_id=jid,
                        lid=lid,
                        encrypt_boss_id=encrypt_boss_id,
                        greeting=greeting,
                    )
                except ChatSendError as exc:
                    logger.warning("打招呼正文没发出去 %s / %s：%s", jid, name, exc)
                    self._record(task, item, status=DELIVER_FAILED, error=humanize(exc))
                    consecutive_37 = 0
                except JobApiError as exc:
                    outcome = self._on_api_error(task, item, exc)
                    if outcome == "stop":
                        task.done += 1
                        return
                    if outcome == "cooloff":
                        consecutive_37 += 1
                        if consecutive_37 >= BROWSER_CHECK_GIVEUP:
                            self._stop(
                                task,
                                f"连续 {consecutive_37} 次撞安全网关 code 37，已停发送；"
                                "过几分钟再试（别在这时候继续砸）",
                            )
                            return
                        self._sleep(BROWSER_CHECK_COOLOFF)
                except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮整批
                    logger.warning("打招呼失败 %s / %s：%s", jid, name, exc)
                    self._record(task, item, status=DELIVER_FAILED, error=humanize(exc))
                    consecutive_37 = 0
                else:
                    consecutive_37 = 0
                    sent_at = time.time()
                    try:
                        store.update_last_greeted_at(jid)
                    except Exception:  # noqa: BLE001 - 冷却史写失败不该把成功当失败
                        logger.warning("职位 %s 的发送史未保存", jid, exc_info=True)
                    self._record(task, item, status=DELIVER_OK, delivered_at=sent_at)
                    task.push("sent", {"encrypt_job_id": jid, "job_name": name})

                task.done += 1
                if interval > 0 and task.done < task.total:
                    self._sleep(interval)

            with task.lock:
                if task.status == ST_RUNNING:
                    task.status = ST_DONE
                task.ended_at = time.time()
            task.push("finished", {"sent": task.ok, "failed": task.failed})
        finally:
            close_chat = getattr(client, "close_chat", None)
            if callable(close_chat):
                try:
                    close_chat()
                except Exception:  # noqa: BLE001 - 断连失败不该盖掉任务结果
                    logger.debug("关聊天通道失败", exc_info=True)
            store.close()
            hub.release(task.task_id)

    def _on_api_error(self, task: DeliverTask, item: dict[str, Any], exc: JobApiError) -> str:
        """分类处理业务错误，回 `"stop"` / `"cooloff"` / `"continue"`。

        Args:
            task: 当前任务。
            item: 命中的 match 条目。
            exc: 业务错误。
        """
        jid = str(item.get("encrypt_job_id"))
        name = str(item.get("job_name") or "")
        if exc.is_risk_control:
            self._record(task, item, status=DELIVER_FAILED, error=f"账号异常：{exc.message}")
            self._stop(task, f"账号异常（code {exc.code}）：{exc.message}。请人工处理后再发。")
            return "stop"
        if exc.is_chat_rate_limited:
            error = f"操作频率限制：{exc.message}"
            self._record(task, item, status=DELIVER_FAILED, error=error)
            self._stop(task, f"{error}。已立即停止后续发送，请稍后再试。")
            return "stop"
        if exc.is_chat_limit_exhausted:
            error = f"每日沟通配额已耗尽：{exc.message}"
            self._record(task, item, status=DELIVER_FAILED, error=error)
            self._stop(task, f"{error}。这是 BOSS 侧的每日沟通配额（不是登录失效），明天再试。")
            return "stop"
        if exc.is_chat_remind:
            remaining = exc.chat_remind_remaining
            self._record(task, item, status=DELIVER_FAILED, error=f"开聊提醒：{exc.message}")
            if remaining == 0:
                self._stop(
                    task,
                    f"今日沟通配额已用完：{exc.message}。"
                    "这是 BOSS 侧的每日沟通配额（不是登录失效），明天再试。",
                )
                return "stop"
            logger.warning(
                "开聊提醒（已模拟确认仍未通过）%s / %s：剩余 %s，继续下一条",
                jid,
                name,
                remaining if remaining is not None else "未知",
            )
            return "continue"
        if exc.is_session_expired:
            self._record(task, item, status=DELIVER_FAILED, error=f"登录态失效：{exc.message}")
            self._stop(task, f"登录态失效：{exc.message}。请先到「登录」页重新登录。")
            return "stop"
        if exc.is_browser_check:
            logger.warning("打招呼撞安全网关 %s / %s：%s", jid, name, exc.message)
            self._record(task, item, status=DELIVER_FAILED, error=f"安全网关：{exc.message}")
            return "cooloff"
        self._record(task, item, status=DELIVER_FAILED, error=exc.message)
        return "continue"

    def _record(
        self,
        task: DeliverTask,
        item: dict[str, Any],
        *,
        status: str,
        error: str = "",
        delivered_at: float | None = None,
    ) -> None:
        """一条的结果：写进任务流水 + 写回 analysis payload。"""
        jid = str(item.get("encrypt_job_id"))
        task.add_item(
            jid,
            job_name=str(item.get("job_name") or ""),
            brand_name=str(item.get("brand_name") or ""),
            status=status,
            error=error,
            delivered_at=delivered_at,
        )
        if status == DELIVER_OK:
            task.ok += 1
        elif status == ITEM_SKIPPED:
            task.skipped += 1
        else:
            task.failed += 1
        try:
            update_delivery(
                task.analysis_id,
                jid,
                deliver_status=status,
                delivered_at=delivered_at,
                deliver_error=error,
            )
        except (FileNotFoundError, KeyError) as exc:
            logger.warning("发送结果写不回 analysis %s / %s：%s", task.analysis_id, jid, exc)

    def _stop(self, task: DeliverTask, message: str) -> None:
        """整批停住并把原因写进任务。"""
        logger.warning("发送任务 %s 停批：%s", task.task_id, message)
        with task.lock:
            task.status = ST_ERROR
            task.error = message
            task.stopped_reason = message
            task.ended_at = time.time()
        task.push("error", {"message": message})

    def _fail(self, task: DeliverTask, message: str) -> None:
        """整批失败并把原因写进任务。"""
        with task.lock:
            task.status = ST_ERROR
            task.error = message
            task.stopped_reason = message
            task.ended_at = time.time()
        task.push("error", {"message": message})


deliver_tasks = DeliverTaskManager()
