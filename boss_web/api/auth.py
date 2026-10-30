"""登录 / 会话 / 滑块路由。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from ..errors import ConflictError, NotFoundError
from ..services.login_task import login_tasks

router = APIRouter()


class SmsRequest(BaseModel):
    """发短信验证码请求。"""

    phone: str = Field(..., description="11 位手机号")
    dial_code: str = "86"


class LoginRequest(BaseModel):
    """提交短信验证码登录。"""

    task_id: str
    code: str


class CancelRequest(BaseModel):
    """取消登录任务。"""

    task_id: str


class SliderSolutionBody(BaseModel):
    """极验票据三键，长短名都认。"""

    geetest_challenge: str | None = None
    geetest_validate: str | None = None
    geetest_seccode: str | None = None
    challenge: str | None = None
    validate_token: str | None = Field(None, alias="validate")
    seccode: str | None = None
    rand_key: str | None = Field(None, alias="randKey")

    model_config = {"populate_by_name": True}

    def as_payload(self) -> dict[str, Any]:
        """合并长短名，拼出提交给极验的 payload。"""
        return {
            k: v
            for k, v in {
                "geetest_challenge": self.geetest_challenge or self.challenge,
                "geetest_validate": self.geetest_validate or self.validate_token,
                "geetest_seccode": self.geetest_seccode or self.seccode,
                "randKey": self.rand_key,
            }.items()
            if v
        }


@router.get("/status")
def status() -> dict[str, Any]:
    """当前登录任务总览。"""
    return login_tasks.status_payload()


@router.post("/sms")
def send_sms(body: SmsRequest) -> dict[str, Any]:
    """发起发码登录任务。"""
    task = login_tasks.start_send(body.phone, dial_code=body.dial_code)
    return task.snapshot()


@router.post("/login")
def submit_login(body: LoginRequest) -> dict[str, Any]:
    """提交短信验证码。"""
    task = login_tasks.submit_code(body.task_id, body.code)
    return task.snapshot()


class QrStartRequest(BaseModel):
    """APP 扫码登录的启动参数。pk 省略就用登录页那个 `cpc_user_sign_up`。"""

    pk: str = ""


@router.post("/qr")
def start_qr(body: QrStartRequest | None = None) -> dict[str, Any]:
    """起一条 APP 扫码登录：后台出二维码，网页去 `/qr/{id}/image` 取图。"""
    task = login_tasks.start_qr(pk=(body.pk if body else ""))
    return task.snapshot()


@router.get("/qr/{task_id}/image")
def qr_image(task_id: str) -> Response:
    """把当前二维码画成 PNG。`?v=` 递增可破浏览器缓存。"""
    png = login_tasks.qr_png(task_id)
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/tasks/{task_id}")
def task_status(task_id: str) -> dict[str, Any]:
    """查询单条登录任务快照。"""
    return login_tasks.snapshot(task_id)


@router.post("/cancel")
def cancel(body: CancelRequest) -> dict[str, Any]:
    """取消登录任务。"""
    task = login_tasks.cancel(body.task_id)
    return task.snapshot()


@router.get("/slider/{task_id}")
def slider_page(task_id: str) -> Response:
    """极验官方组件帮助页：人拖滑块，票据 POST 回 `/solution`。"""
    task = login_tasks.get(task_id)
    if task.slider_html is None:
        raise NotFoundError("当前没有待完成的滑块验证")
    return Response(content=task.slider_html, media_type="text/html; charset=utf-8")


@router.post("/slider/{task_id}/refresh")
def slider_refresh(request: Request, task_id: str) -> dict[str, Any]:
    """帮助页「重新加载」：换一张新的极验挑战再渲染。

    回的 `page_url` 是**站点作用域**路径，跟前端开窗那条同一个口径——
    legacy 别名（`/api/auth/…`）是给旧 SPA 的，别再从这里往外发。
    """
    task = login_tasks.refresh_slider(task_id)
    return {
        "ok": True,
        "status": task.status,
        "page_url": f"/api/sites/{request.state.site}/auth/slider/{task.task_id}",
    }


@router.post("/slider/{task_id}/solution")
def slider_solution(task_id: str, body: SliderSolutionBody) -> dict[str, Any]:
    """提交极验滑块票据。"""
    task = login_tasks.solve_slider(task_id, body.as_payload())
    return {"ok": True, "status": task.status}


@router.post("/logout")
def logout() -> dict[str, Any]:
    """登出：尽量清远端会话，并清本地登录态。"""
    try:
        from boss_login import create_client

        client = create_client(load_stored=True)
        client.logout()
    except Exception:  # noqa: BLE001 - 远端登出失败也要清本地
        pass
    from boss_login.session import clear_session

    try:
        clear_session()
    except Exception:  # noqa: BLE001
        pass
    _broadcast_session_changed()
    return {"logged_in": False}


def _broadcast_session_changed() -> None:
    """登出后推一帧 `session.changed`，信号球 / 首页会话卡立刻跟着刷。"""
    try:
        from ..services.login_task import login_tasks
        from ..services.task_runtime import hub

        hub.broadcast("session.changed", login_tasks.status_payload())
    except Exception:  # noqa: BLE001 - 会话推送失败不影响登出本身
        pass
