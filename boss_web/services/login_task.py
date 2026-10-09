"""登录任务状态机：把 ``run_sms_login`` 的阻塞回调接到网页表单上。

「发码 → 输码 → （滑块）→ 登录」在后台线程里跑，HTTP 只负责启动和喂料：
``start_send`` 发短信，``submit_code`` / ``solve_slider`` 把人工输入塞回阻塞中的
``code_provider`` / ``slider_solver``。

滑块只是**人机协作管道**：官方组件渲染、人拖、票据回传。不认缺口、不伪造轨迹。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from boss_login import (
    create_client,
    persist_login,
)
from boss_login.errors import (
    BossLoginError,
    CodeRejected,
    RateLimited,
    RiskControlRequired,
    ValidationError,
)
from boss_login.models import LoginResult
from boss_login.verify import (
    SliderChallenge,
    SliderSolution,
    SliderSolver,
    build_helper_html,
    parse_solution,
)

from .. import config as C
from ..errors import ConflictError, NotFoundError, UpstreamError, ValidationWebError

logger = logging.getLogger(__name__)

ST_PENDING = "pending"
ST_SENDING = "sending_sms"
ST_NEED_CODE = "need_code"
ST_NEED_SLIDER = "need_slider"
ST_LOGGING_IN = "logging_in"
ST_DONE = "done"
ST_ERROR = "error"
ST_CANCELLED = "cancelled"

_TERMINAL = {ST_DONE, ST_ERROR, ST_CANCELLED}


@dataclass
class LoginTask:
    """一次网页登录的全部状态。"""

    task_id: str
    phone: str
    dial_code: str = "86"
    status: str = ST_PENDING
    created_at: float = field(default_factory=time.time)
    events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=50))
    error: str | None = None
    result: dict[str, Any] | None = None
    retry_after: float = 0.0
    attempts: int = 0

    #: code_provider 阻塞在这里等网页表单
    code_event: threading.Event = field(default_factory=threading.Event)
    code_box: dict[str, Any] = field(default_factory=dict)

    #: slider_solver 阻塞在这里等网页里的极验组件
    slider_event: threading.Event = field(default_factory=threading.Event)
    slider_box: dict[str, Any] = field(default_factory=dict)
    slider_challenge: SliderChallenge | None = None
    slider_html: str | None = None
    slider_needed: bool = False
    #: 重新拉一张滑块挑战的回调（帮助页「重新加载」用）；由 solver 挂上
    slider_refresher: Callable[[], SliderChallenge] | None = None

    lock: threading.RLock = field(default_factory=threading.RLock)
    cancel_flag: bool = False

    def record(self, name: str, payload: Mapping[str, Any] | None = None) -> None:
        with self.lock:
            self.events.append(
                {"at": time.time(), "name": name, "payload": dict(payload or {})}
            )

    def set_status(self, status: str) -> None:
        with self.lock:
            self.status = status

    @property
    def finished(self) -> bool:
        return self.status in _TERMINAL

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "task_id": self.task_id,
                "status": self.status,
                "phone": self.phone,
                "phone_masked": _mask_phone(self.phone),
                "dial_code": self.dial_code,
                "created_at": self.created_at,
                "attempts": self.attempts,
                "retry_after": self.retry_after,
                "error": self.error,
                "result": self.result,
                "slider": {
                    "needed": self.slider_needed,
                    "solved": bool(self.slider_box.get("solution")),
                    "page_url": f"/api/auth/slider/{self.task_id}",
                },
                "events": list(self.events),
            }


def _mask_phone(phone: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) < 7:
        return phone
    return f"{digits[:3]}****{digits[-4:]}"


class LoginTaskManager:
    """同一时刻只跑一个登录任务（本机单用户工具）。"""

    def __init__(self, *, session_path: Any = None, wait_timeout: float | None = None) -> None:
        self._session_path = session_path
        self._wait_timeout = wait_timeout or C.LOGIN_WAIT_TIMEOUT
        self._lock = threading.Lock()
        self._tasks: dict[str, LoginTask] = {}
        self._current: LoginTask | None = None


    def current(self) -> LoginTask | None:
        with self._lock:
            return self._current

    def get(self, task_id: str) -> LoginTask:
        task = self._tasks.get(task_id)
        if task is None:
            raise NotFoundError(f"登录任务不存在：{task_id}")
        return task

    def snapshot(self, task_id: str | None = None) -> dict[str, Any]:
        if task_id:
            return self.get(task_id).snapshot()
        task = self.current()
        return task.snapshot() if task else {}

    def status_payload(self) -> dict[str, Any]:
        """``GET /api/auth/status`` 用：本地会话 + 当前任务。"""
        import boss_db

        from boss_login.session import load_session

        path = boss_db.resolve_db_path(self._session_path)
        stored = load_session(path)
        task = self.current()
        return {
            "logged_in": bool(stored.cookies or stored.token),
            "phone_masked": stored.phone_masked,
            "user": stored.user,
            "saved_at": stored.saved_at,
            "session_path": str(path),
            "task": task.snapshot() if task else None,
        }


    def start_send(self, phone: str, *, dial_code: str = "86") -> LoginTask:
        digits = "".join(ch for ch in phone if ch.isdigit())
        if len(digits) < 11:
            raise ValidationWebError("手机号看起来不对，请填 11 位数字")

        with self._lock:
            if self._current is not None and not self._current.finished:
                raise ConflictError("已有登录流程在进行中，请先完成或取消")

            task = LoginTask(task_id=uuid.uuid4().hex[:12], phone=digits, dial_code=dial_code)
            self._tasks[task.task_id] = task
            self._current = task

        thread = threading.Thread(
            target=self._run_sms_login, args=(task,), daemon=True, name=f"login-{task.task_id}"
        )
        thread.start()
        return task

    def submit_code(self, task_id: str, code: str) -> LoginTask:
        task = self.get(task_id)
        code = (code or "").strip()
        if not code:
            raise ValidationWebError("验证码不能为空")
        if task.finished:
            raise ConflictError("本次登录已结束，请重新发起")
        with task.lock:
            task.code_box["code"] = code
            task.attempts += 1
            task.code_event.set()
            task.set_status(ST_LOGGING_IN)
        task.record("code_submitted", {"attempt": task.attempts})
        return task

    def solve_slider(self, task_id: str, payload: Mapping[str, Any]) -> LoginTask:
        task = self.get(task_id)
        if task.finished:
            raise ConflictError("本次登录已结束")
        try:
            solution = parse_solution(payload)
        except ValidationError as exc:
            raise ValidationWebError(f"滑块票据不完整：{exc}") from exc
        with task.lock:
            task.slider_box["solution"] = solution
            task.slider_needed = False
            task.slider_event.set()
            task.set_status(ST_LOGGING_IN if task.attempts else ST_SENDING)
        task.record("slider_solved", {"challenge": solution.challenge})
        return task

    def refresh_slider(self, task_id: str) -> LoginTask:
        """帮助页「重新加载」：换一张新的极验挑战，重建帮助页。

        极验 ``challenge`` 一次性，旧页重载会拿已用过的挑战去 initGeetest 而 onError。
        """
        task = self.get(task_id)
        if task.finished:
            raise ConflictError("本次登录已结束，请重新发起")
        refresher = task.slider_refresher
        if refresher is None:
            raise ConflictError("当前没有待完成的滑块验证")
        try:
            challenge = refresher()
        except BossLoginError as exc:
            raise UpstreamError(f"重新拉取滑块挑战失败：{exc}") from exc
        html = build_helper_html(
            challenge,
            post_url=f"/api/auth/slider/{task.task_id}/solution",
        )
        with task.lock:
            task.slider_challenge = challenge
            task.slider_html = html
            task.slider_needed = True
            task.slider_box.clear()
            task.slider_event.clear()
            task.set_status(ST_NEED_SLIDER)
        task.record("slider_refreshed", {"gt": challenge.gt})
        return task

    def cancel(self, task_id: str) -> LoginTask:
        task = self.get(task_id)
        with task.lock:
            task.cancel_flag = True
            task.code_box["code"] = None
            task.code_event.set()
            task.slider_box["solution"] = None
            task.slider_event.set()
            task.set_status(ST_CANCELLED)
        task.record("cancelled", {})
        return task


    def _web_slider_solver(self, task: LoginTask, client: Any) -> SliderSolver:
        """``slider_solver`` 回调：挂出帮助页，等人拖完把票据送回来。

        ``client`` 供「重新加载」换挑战用（极验 challenge 一次性）。
        """
        def fetch_challenge() -> SliderChallenge:
            return client.fetch_slider_challenge()

        def solver(challenge: SliderChallenge) -> SliderSolution | None:
            if task.cancel_flag:
                return None
            html = build_helper_html(
                challenge,
                post_url=f"/api/auth/slider/{task.task_id}/solution",
            )
            with task.lock:
                task.slider_challenge = challenge
                task.slider_html = html
                task.slider_needed = True
                task.slider_refresher = fetch_challenge
                task.slider_box.clear()
                task.slider_event.clear()
                task.set_status(ST_NEED_SLIDER)
            task.record("slider_required", {"gt": challenge.gt})

            if not task.slider_event.wait(self._wait_timeout):
                task.record("slider_timeout", {})
                return None
            if task.cancel_flag:
                return None
            return task.slider_box.get("solution")

        return solver

    def _code_provider(self, task: LoginTask):
        def provider(attempt: int, _ticket: Any) -> str | None:
            with task.lock:
                task.attempts = max(task.attempts, attempt)
                task.set_status(ST_NEED_CODE)
            task.record("need_code", {"attempt": attempt})
            if not task.code_event.wait(self._wait_timeout):
                task.record("code_timeout", {})
                return None
            if task.cancel_flag:
                return None
            code = task.code_box.get("code")
            task.code_event.clear()
            return code

        return provider

    def _run_sms_login(self, task: LoginTask) -> None:
        kwargs: dict[str, Any] = {"load_stored": True}
        if self._session_path is not None:
            kwargs["session_path"] = self._session_path
        client = create_client(**kwargs)
        solver = self._web_slider_solver(task, client)

        def on_event(name: str, payload: dict[str, Any]) -> None:
            task.record(name, payload)

        try:
            task.set_status(ST_SENDING)
            task.record("started", {"phone_masked": _mask_phone(task.phone)})
            result = client.run_sms_login(
                task.phone,
                self._code_provider(task),
                dial_code=task.dial_code,
                slider_solver=solver,
                on_event=on_event,
            )
            if task.cancel_flag:
                task.set_status(ST_CANCELLED)
                return
            self._finish_ok(task, client, result)
        except CodeRejected as exc:
            self._finish_err(task, f"验证码不对：{exc}")
        except RateLimited as exc:
            with task.lock:
                task.retry_after = float(getattr(exc, "retry_after", 60) or 60)
            self._finish_err(task, f"发送太频繁：{exc}")
        except RiskControlRequired as exc:
            self._finish_err(task, f"触发风控，需要人工处理：{exc}")
        except ValidationError as exc:
            self._finish_err(task, str(exc))
        except BossLoginError as exc:
            if task.cancel_flag:
                task.set_status(ST_CANCELLED)
            else:
                self._finish_err(task, str(exc))
        except Exception as exc:  # noqa: BLE001 - 兜住线程里的意外
            logger.exception("登录任务 %s 崩了", task.task_id)
            self._finish_err(task, f"登录流程出错：{exc}")

    def _finish_ok(self, task: LoginTask, client: Any, result: LoginResult) -> None:
        """落盘登录态（不给 ``session_path`` = 写默认状态库 ``BOSS_DB`` / ``data/boss.db``）。"""
        try:
            persist_login(client, result, session_path=self._session_path)
        except Exception as exc:  # noqa: BLE001 - 落盘失败不该把登录说成失败
            logger.warning("登录成功但写状态库失败：%s", exc)
        with task.lock:
            task.result = {
                "logged_in": True,
                "phone_masked": getattr(result, "phone_masked", "") or _mask_phone(task.phone),
                "user": getattr(result, "user", None) or {},
            }
            task.set_status(ST_DONE)
        task.record("success", task.result)

    @staticmethod
    def _finish_err(task: LoginTask, message: str) -> None:
        with task.lock:
            task.error = message
            task.set_status(ST_ERROR)
        task.record("error", {"message": message})


login_tasks = LoginTaskManager()
