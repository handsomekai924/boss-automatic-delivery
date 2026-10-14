"""一键投递任务：把匹配结果里的目标岗位串行打招呼 **并把招呼语发出去**。

一键 / 多条 / 单条三种入口只差目标集合，任务本身只认一串 ``encrypt_job_id``
（todo.md C2）。规矩照 todo.md C6 / C7 写死，别在界面层再实现一遍：

- 确认弹窗是前端的事（C7），这里只收已确认的清单；
- 串行 + :data:`boss_jobs.config.DELIVER_INTERVAL` 节流，可随时取消；
- ``delivered_at`` 有值跳过不重发；单条失败只记 ``deliver_error``、不自动重试
  （失败条目可人工点行内「发送」再试）；
- 撞 **code 36**（账号异常 / 人机验证）**立即停整批**，继续砸只会更糟；
- 撞 **「开聊提醒」**（每日沟通配额提示弹窗，``friend/add`` 回 code 1 +
  chatRemindDialog）**不是停批信号**：这是 blockLevel 0 的提示弹窗，话术
  「您今天已与120位BOSS沟通，还剩30次沟通机会哦」里那 30 是**还能再发的次数**。
  :meth:`boss_jobs.client.JobClient.greet` 已经做过「模拟点击确认」
  （埋点 + ``cid=1`` 重打 ``friend/add``）；确认完还被拦才轮到这儿——
  只记这一条、**继续发剩下的**，除非话术明说「还剩 0 次」。
  **不是登录失效**，别叫人去重新登录（2026-10-09 实测踩过：code 1 被当
  登录态失效，人重新登录了照样发不出去）；
- 结果写回 ``analysis`` 的 ``deliver_status`` / ``delivered_at`` / ``deliver_error``。

发送是「建会话 + 真投递正文」两步：:meth:`boss_jobs.client.JobClient.deliver_greeting`
先打 ``friend/add.json``，再经 MQTT（:mod:`boss_jobs.chat`）发正文——站点
``friend/add`` **只建会话、不投递任何文本**。成功判据是「这帧发出去了」
（``ChatSocket.send_text`` PUBLISH 无异常且 ``rc == 0``）；**没有回读接口可验**，
也别指望 PUBACK（网关对文本帧不回，发完约 150ms 关 WebSocket 是常态）。
细节见 :mod:`boss_jobs.chat`。
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
from .troubleshoot import humanize

logger = logging.getLogger(__name__)

ST_RUNNING = "running"
ST_DONE = "done"
ST_ERROR = "error"
ST_CANCELLED = "cancelled"

#: 30 天发送冷却窗（秒）。**写死常量**，不进配置——以后要调再进 ``deliver_config``。
#: 判定：``可发送 ⇔ now - last_greeted_at(job) ≥ 30 天``；恰好 30 天整 → 允许再发。
GREET_COOLDOWN_DAYS = 30
GREET_COOLDOWN_SECONDS = GREET_COOLDOWN_DAYS * 86400

#: 跳过原因模板；日期 MM-DD 由调用方填
GREET_COOLDOWN_SKIP_FMT = "30 天内已发送（{date}）"


def parse_greet_time(value: Any) -> float | None:
    """``jobs.last_greeted_at`` / ``item.delivered_at`` → ``time.time()`` 同口径的秒。

    - 数字（含数字字符串）= Unix 时间戳，原样用；
    - ``%Y-%m-%d %H:%M:%S`` / ``%Y-%m-%dT%H:%M:%S``（ISO 风格，与 ``fetched_at`` 同格式）
      按服务器本地时钟解析，**不做时区换算**；
    - 读不到 / 解析失败 → ``None``（按「从未发送」处理，放行——宁可多一次确认弹窗，
      不静默吞掉可发对象）。
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

    ``last_greeted_at``（岗位维度）与 ``delivered_at``（本条分析的即时状态）**取较晚者**，
    再套 30 天窗——兼容上线前已发出的岗位（只有 ``delivered_at``）与上线后的新发送。

    :return: ``{last_greeted_ts, last_greeted_at, cooldown_until, in_cooldown, cooldown_days_left}``
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
    in_cooldown = remaining > 0  # 恰好 30 天整 → 允许再发
    return {
        "last_greeted_ts": last_ts,
        "last_greeted_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_ts)),
        "cooldown_until": cooldown_until,
        "in_cooldown": in_cooldown,
        "cooldown_days_left": max(0, int((remaining + 86399) // 86400)) if in_cooldown else 0,
    }


def greet_cooldown_reason(state: Mapping[str, Any]) -> str:
    """冷却中 → 跳过原因 ``30 天内已发送（MM-DD）``；否则空串。"""
    if not state.get("in_cooldown"):
        return ""
    ts = parse_greet_time(state.get("last_greeted_ts") or state.get("last_greeted_at"))
    date = time.strftime("%m-%d", time.localtime(ts)) if ts else "??-??"
    return GREET_COOLDOWN_SKIP_FMT.format(date=date)


def enrich_match_greet_state(items: list[dict[str, Any]], *, now: float | None = None) -> None:
    """读接口出门前给 match 条目补**当前**发送史与冷却状态（就地改）。

    分析快照里的 ``last_greeted_at`` 是匹配那一刻的值；真正算冷却要看
    ``jobs`` 表现在的 ``last_greeted_at`` 与 ``item.delivered_at`` 取较晚者。
    读不到就按「从未发送」放行。
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


#: Any safety field marks a current-format match snapshot.
_SAFETY_FIELDS = (
    "is_blacklisted", "company_blacklisted", "jd_blacklisted", "jd_rule_checked",
    "jd_status", "auto_delivery_blocked", "matched_rule_snapshot",
)


def _has_safety_snapshot(item: dict[str, Any]) -> bool:
    # 必须字段齐全才算有安全快照；缺字段的旧分析不能进自动投递
    return all(key in item for key in _SAFETY_FIELDS)


def _delivery_restriction(item: dict[str, Any], *, automatic: bool = False) -> str:
    """Return a stable reason from the immutable match-time safety snapshot.

    Legacy analyses can still be explicitly sent by a user-selected request for
    backwards compatibility, but they are never eligible for automatic/all send.
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
    """Return reasons that an automatic/all batch must never send."""
    return _delivery_restriction(item, automatic=True) or (
        "没有招呼语正文" if not str(item.get("greeting") or "").strip() else ""
    )


ITEM_SKIPPED = "skipped"
class DeliverTask:
    def __init__(
        self,
        analysis_id: str,
        job_ids: Sequence[str],
        *,
        mode: str = "selected",
        confirmed: bool = False,
        confirmation_token: str = "",
    ) -> None:
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
        #: 每个目标的状态流水，前端按 encrypt_job_id 贴回结果行
        self.items: list[dict[str, Any]] = []
        self.events: deque[dict[str, Any]] = deque(maxlen=100)
        self.cancel_flag = False
        self.lock = threading.Lock()

    def push(self, name: str, payload: dict[str, Any] | None = None) -> None:
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
                "events": list(self.events)[-20:],
            }


class DeliverTaskManager:
    """同一时刻只跑一个发送任务。

    :param client_factory: 造 ``JobClient`` 的工厂（测试里注入假的）
    :param sleeper: 可替换的 sleep（测试里注入 no-op，免得真等一秒）
    :param interval: 条间隔（秒），默认 :data:`boss_jobs.config.DELIVER_INTERVAL`
    """

    def __init__(
        self,
        *,
        client_factory: Callable[[], Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        interval: float | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._task: DeliverTask | None = None
        self._client_factory = client_factory or (lambda: create_client())
        self._sleep = sleeper
        self._interval = DELIVER_INTERVAL if interval is None else interval

    def current(self) -> DeliverTask | None:
        with self._lock:
            return self._task

    def snapshot(self, task_id: str | None = None) -> dict[str, Any]:
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
        if mode not in {"all", "selected", "manual"}:
            raise ValueError("mode 必须是 all、selected 或 manual")
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

        thread = threading.Thread(target=self._run, args=(task,), daemon=True, name=task.task_id)
        thread.start()
        return task

    def cancel(self, task_id: str | None = None) -> DeliverTask:
        task = self.current()
        if task is None or (task_id and task.task_id != task_id):
            raise NotFoundError("没有在跑的发送任务")
        task.cancel_flag = True
        task.push("cancel_requested")
        return task

    @staticmethod
    def _cooldown_for(item: dict[str, Any], store: JobStore) -> dict[str, Any]:
        """一条 match 的冷却判定：``jobs.last_greeted_at``（权威）与 ``item.delivered_at`` 取较晚者。

        快照里的 ``last_greeted_at`` 只当补充——库里的才是跨分析的真依据；
        两边读不到就按「从未发送」放行。
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
        """串行发送：已成功的跳过；单条失败记 ``deliver_error`` 继续，不重试。

        空招呼语不发（站点 ``friend/add`` 只建会话、不打字，空消息比不发更糟）；
        ``ChatSendError`` 也不重试（会话可能已建、正文没出，重试会重复打扰）。
        """
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
        # 发送前统一开库：冷却判定以 jobs.last_greeted_at 为准（权威），成功后也写回这里
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
                task.ended_at = time.time()
            task.push("confirmation_required", {"message": message, "count": len(restricted)})
            store.close()
            return

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
                    # 双写：岗位维度 last_greeted_at（跨分析冷却依据）+ item.delivered_at（本次即时状态）
                    try:
                        store.update_last_greeted_at(jid)
                    except Exception:  # noqa: BLE001 - 冷却史写失败不该把成功当失败
                        logger.warning("职位 %s 的发送史未保存", jid, exc_info=True)
                    self._record(task, item, status=DELIVER_OK, delivered_at=sent_at)
                    task.push("sent", {"encrypt_job_id": jid, "job_name": name})

                task.done += 1
                if self._interval > 0 and task.done < task.total:
                    self._sleep(self._interval)

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


    def _on_api_error(self, task: DeliverTask, item: dict[str, Any], exc: JobApiError) -> str:
        """分类处理业务错误，回 ``"stop"`` / ``"cooloff"`` / ``"continue"``。

        code 36（账号异常/人机验证）与登录态失效停整批；code 37 走 cooloff。
        每日沟通配额明确耗尽时停批；其他「开聊提醒」只在话术明说「还剩 0 次」时才停。
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
            # 「开聊提醒」是 **blockLevel 0 的提示弹窗**。确认后仍被拦时，
            # 只有明确的每日额度耗尽话术（或还剩 0 次）才停批；频率限制单独处理。
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
        logger.warning("发送任务 %s 停批：%s", task.task_id, message)
        with task.lock:
            task.status = ST_ERROR
            task.error = message
            task.ended_at = time.time()
        task.push("error", {"message": message})

    def _fail(self, task: DeliverTask, message: str) -> None:
        with task.lock:
            task.status = ST_ERROR
            task.error = message
            task.ended_at = time.time()
        task.push("error", {"message": message})


deliver_tasks = DeliverTaskManager()
