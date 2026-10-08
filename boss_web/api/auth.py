"""登录 / 会话 / 滑块路由。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from ..errors import ConflictError, NotFoundError
from ..services.login_task import login_tasks

router = APIRouter()


class SmsRequest(BaseModel):
    phone: str = Field(..., description="11 位手机号")
    dial_code: str = "86"


class LoginRequest(BaseModel):
    task_id: str
    code: str


class CancelRequest(BaseModel):
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
    return login_tasks.status_payload()


@router.post("/sms")
def send_sms(body: SmsRequest) -> dict[str, Any]:
    task = login_tasks.start_send(body.phone, dial_code=body.dial_code)
    return task.snapshot()


@router.post("/login")
def submit_login(body: LoginRequest) -> dict[str, Any]:
    task = login_tasks.submit_code(body.task_id, body.code)
    return task.snapshot()


@router.get("/tasks/{task_id}")
def task_status(task_id: str) -> dict[str, Any]:
    return login_tasks.snapshot(task_id)


@router.post("/cancel")
def cancel(body: CancelRequest) -> dict[str, Any]:
    task = login_tasks.cancel(body.task_id)
    return task.snapshot()


@router.get("/slider/{task_id}")
def slider_page(task_id: str) -> Response:
    """极验官方组件帮助页：人拖滑块，票据 POST 回 ``/solution``。"""
    task = login_tasks.get(task_id)
    if task.slider_html is None:
        raise NotFoundError("当前没有待完成的滑块验证")
    return Response(content=task.slider_html, media_type="text/html; charset=utf-8")


@router.post("/slider/{task_id}/solution")
def slider_solution(task_id: str, body: SliderSolutionBody) -> dict[str, Any]:
    task = login_tasks.solve_slider(task_id, body.as_payload())
    return {"ok": True, "status": task.status}


@router.post("/logout")
def logout() -> dict[str, Any]:
    try:
        from boss_login import create_client

        client = create_client(load_stored=True)
        client.logout()
    except Exception:  # noqa: BLE001 - 远端登出失败也要清本地
        pass
    from boss_login.session import DEFAULT_SESSION_PATH, clear_session

    try:
        clear_session(DEFAULT_SESSION_PATH)
    except Exception:  # noqa: BLE001
        pass
    return {"logged_in": False}
