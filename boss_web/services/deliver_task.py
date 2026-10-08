"""一键投递任务：把匹配结果里的目标岗位串行打招呼 **并把招呼语发出去**。

三种入口（一键 / 多条 / 单条）收敛到这一个任务，区别只在**目标集合**
（见 todo.md C2）；所以任务本身只认一串 ``encrypt_job_id``。

规矩照 todo.md C6 / C7 写死，别在界面层再实现一遍：

- **必须确认弹窗**是前端的事（C7），这里只收下已经确认过的清单；
- 串行 + :data:`boss_jobs.config.DELIVER_INTERVAL` 节流，随时可取消；
- **已成功的不重发**：``delivered_at`` 有值直接跳过（一键发送天然跳过它们）；
- 单条失败只记 ``deliver_error``、下一条继续；**失败不自动重试**
  （避免重复打扰招聘方，失败条目可人工点行内「发送」再试）；
- 撞 **code 36**（账号异常 / 人机验证）**立即停整批**并提示人工处理——
  账号级风控，继续砸只会更糟；
- 每条结果写回 ``analysis`` payload 的 ``deliver_status`` /
  ``delivered_at`` / ``deliver_error``。

**发的是「建会话 + 真投递正文」两步**（2026-10-08 实测后定案，见 todo.md C6）：
:meth:`boss_jobs.client.JobClient.deliver_greeting` 先打 ``friend/add.json``
建会话，再把招呼语正文经 **MQTT 聊天通道**发给对方
（:mod:`boss_jobs.chat`）。以前只打 ``friend/add`` 不带正文——站点那条接口
**只建会话、不投递任何文本**，App 里看着像打了招呼、聊天框其实是空的，
正是本次要修的症状。

「这条内容真的进服务端了吗」这个问题**没有回读接口可验**：
``JobClient.fetch_chat_history``（``GET /wapi/zpchat/geek/historyMsg``）对这条
账号**永远回 ``code 0`` + 空 ``zpData``**，有消息、刚确认送达的会话也读不出来
（2026-10-08 实测）；早先「回读里看到招呼语」的记录是错的，已作废。判据改成
**「这帧发出去了」**：``ChatSocket.send_text`` 只要 PUBLISH 无异常、``rc == 0``
就算成功。**别指望 PUBACK**——这条网关对文本帧不回 PUBACK，发完约 150ms 就把
WebSocket 关掉，那是它的常态（站点会话列表里那几发都显示「[送达]」，重载页面
还在，是真进了服务端）。细节见 :mod:`boss_jobs.chat` 模块头。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from typing import Any, Callable, Sequence

from boss_jobs import create_client
from boss_jobs.config import (
    BROWSER_CHECK_COOLOFF,
    BROWSER_CHECK_GIVEUP,
    DELIVER_INTERVAL,
)
from boss_jobs.errors import ChatSendError, JobApiError
from boss_jobs.store import JobStore

from ..errors import ConflictError, NotFoundError
from .resume_store import (
    DELIVER_FAILED,
    DELIVER_OK,
    DELIVER_SENDING,
    load_analysis,
    update_delivery,
)

logger = logging.getLogger(__name__)

ST_RUNNING = "running"
ST_DONE = "done"
ST_ERROR = "error"
ST_CANCELLED = "cancelled"

#: 发送状态里「这条跳过了」（已发送过）
ITEM_SKIPPED = "skipped"


class DeliverTask:
    def __init__(self, analysis_id: str, job_ids: Sequence[str]) -> None:
        self.task_id = "dv_" + uuid.uuid4().hex[:10]
        self.analysis_id = analysis_id
        self.job_ids = list(job_ids)
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

    def start(self, *, analysis_id: str, job_ids: Sequence[str]) -> DeliverTask:
        with self._lock:
            if self._task is not None and self._task.status == ST_RUNNING:
                raise ConflictError("已有发送任务在跑，等它结束或先取消")
            task = DeliverTask(analysis_id, job_ids)
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

    # ------------------------------------------------------------------ #

    def _run(self, task: DeliverTask) -> None:
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

        todo: list[dict[str, Any]] = []
        for jid in task.job_ids:
            item = index.get(jid)
            if item is None:
                logger.warning("分析 %s 里没有职位 %s，跳过", task.analysis_id, jid)
                continue
            if item.get("delivered_at"):
                # 已成功的不重发（一键发送天然跳过它们）
                task.add_item(
                    jid,
                    job_name=str(item.get("job_name") or ""),
                    brand_name=str(item.get("brand_name") or ""),
                    status=ITEM_SKIPPED,
                    delivered_at=float(item.get("delivered_at") or 0.0),
                )
                task.skipped += 1
                task.push("skipped", {"encrypt_job_id": jid, "reason": "已发送"})
                continue
            todo.append(item)

        task.total = len(todo)
        if not todo:
            with task.lock:
                task.status = ST_DONE
                task.ended_at = time.time()
            task.push("finished", {"sent": 0, "skipped": task.skipped})
            return

        try:
            client = self._client_factory()
            store = JobStore()
        except Exception as exc:  # noqa: BLE001 - 客户端/库起不来就整批失败
            self._fail(task, f"{type(exc).__name__}: {exc}")
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
                    # 没有正文就不发：站点那条 friend/add 只建会话、不打字，
                    # 「发了一条空招呼」比不发更糟（招聘方那边真会收到一条空消息）。
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
                    # 聊天通道那步失败：会话可能建起来了、正文没发出去。
                    # 不重试（避免重复打扰），让人点行内「发送」再试。
                    logger.warning("打招呼正文没发出去 %s / %s：%s", jid, name, exc)
                    self._record(task, item, status=DELIVER_FAILED, error=str(exc))
                    consecutive_37 = 0
                except JobApiError as exc:
                    outcome = self._on_api_error(task, item, exc)
                    if outcome == "stop":
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
                    self._record(task, item, status=DELIVER_FAILED, error=str(exc))
                    consecutive_37 = 0
                else:
                    consecutive_37 = 0
                    self._record(task, item, status=DELIVER_OK, delivered_at=time.time())
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
            # 聊天连接是整批复用的，收尾时统一断——别把 MQTT 会话漏在那儿
            close_chat = getattr(client, "close_chat", None)
            if callable(close_chat):
                try:
                    close_chat()
                except Exception:  # noqa: BLE001 - 断连失败不该盖掉任务结果
                    logger.debug("关聊天通道失败", exc_info=True)
            store.close()

    # ------------------------------------------------------------------ #

    def _on_api_error(self, task: DeliverTask, item: dict[str, Any], exc: JobApiError) -> str:
        """分类处理业务错误，回 ``"stop"`` / ``"cooloff"`` / ``"continue"``。"""
        jid = str(item.get("encrypt_job_id"))
        name = str(item.get("job_name") or "")
        if exc.is_risk_control:
            # 账号异常 / 人机验证：继续只会让风控更紧，立刻停整批
            self._record(task, item, status=DELIVER_FAILED, error=f"账号异常：{exc.message}")
            self._stop(task, f"账号异常（code {exc.code}）：{exc.message}。请人工处理后再发。")
            return "stop"
        if exc.is_session_expired:
            # 登录态失效：后面每一条都会失败，别白打
            self._record(task, item, status=DELIVER_FAILED, error=f"登录态失效：{exc.message}")
            self._stop(task, f"登录态失效：{exc.message}。请先到「登录」页重新登录。")
            return "stop"
        if exc.is_browser_check:
            # 安全网关：客户端已退避 + 换新令牌重试过，还 37 多半是整段被限速
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
