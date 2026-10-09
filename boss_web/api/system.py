"""环境自检与 Chrome 引导。

非技术用户最需要的就是这一组：程序把库建到哪了、Chrome 有没有装、需不需要把
取令牌的窗口切成可见。全部**懒检测**——不阻塞启动，用户装完 Chrome 点一下
「重新检测」就行，不必重启。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from .. import runtime
from ..errors import EnvironmentWebError, ValidationWebError
from ..services import app_settings_store, troubleshoot

router = APIRouter()


class ChromeModeBody(BaseModel):
    #: ``visible`` / ``offscreen`` / ``hidden`` / ``headless``；空串 = 跟随默认
    mode: str = Field("", description="Chrome 窗口档位")


@router.get("/health")
def health() -> dict[str, Any]:
    """数据存哪、是不是从压缩包里跑的、exe 目录能不能写。"""
    import boss_db

    data_dir = runtime.app_data_dir()
    return {
        "frozen": runtime.is_frozen(),
        "data_dir": str(data_dir),
        "db": str(boss_db.resolve_db_path()),
        # exe 在 %TEMP% 下跑 = 用户直接在压缩包预览里双击了，数据会丢
        "temp_run": runtime.is_temp_run(),
        "writable": runtime.is_writable(data_dir),
        "portable": runtime.is_frozen() and runtime.is_writable(data_dir),
    }


@router.get("/chrome")
def chrome() -> dict[str, Any]:
    """Chrome 检测。找不到也返回 200——前端要拿 message 显示成引导。"""
    return troubleshoot.chrome_status()


@router.post("/chrome/mode")
def set_chrome_mode(body: ChromeModeBody) -> dict[str, Any]:
    """切换取令牌窗口的档位，**立即生效**（不用重启）。"""
    from boss_jobs.cdp_stoken import CHROME_MODES

    raw = (body.mode or "").strip().lower()
    if raw and raw not in CHROME_MODES:
        raise ValidationWebError(f"不认识的窗口档位：{body.mode}（可选 {', '.join(CHROME_MODES)}）")

    settings = app_settings_store.apply_chrome_mode(raw)
    return {
        "ok": True,
        "mode": settings.chrome_mode or troubleshoot.current_mode(),
        "saved": settings.chrome_mode,
        "message": "已切换。下次取令牌时生效。",
    }


@router.post("/chrome/open")
def open_chrome() -> dict[str, Any]:
    """用**可见**窗口拉起取令牌的 Chrome。

    给「账号被风控、需要人工拖一次滑块」的场景用：默认档位是 hidden，窗口连
    任务栏里都没有，用户根本够不着。
    """
    from boss_jobs.cdp_stoken import StokenError, launch_chrome

    try:
        launch_chrome(mode="visible")
    except StokenError as exc:
        raise EnvironmentWebError(troubleshoot.humanize(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - 拉不起来一律按环境问题报人话
        raise EnvironmentWebError(troubleshoot.humanize(exc)) from exc

    return {
        "ok": True,
        "message": "已用可见窗口打开 Chrome。如果它停在验证页面，把滑块拖完再回到本页继续。",
    }
