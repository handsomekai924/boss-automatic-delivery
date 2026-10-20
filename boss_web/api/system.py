"""环境自检与 Chrome 引导。

懒检测：不阻塞启动，用户装完 Chrome 点「重新检测」即可。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from .. import __version__ as APP_VERSION
from .. import runtime
from ..errors import ConflictError, EnvironmentWebError, UpstreamError, ValidationWebError
from ..services import app_settings_store, troubleshoot, update_check
from ..services import updater

router = APIRouter()
logger = logging.getLogger(__name__)


class ChromeModeBody(BaseModel):
    """Chrome 窗口档位请求体。

    Attributes:
        mode: `visible` / `offscreen` / `hidden` / `headless`；空串 = 跟随默认。
    """

    mode: str = Field("", description="Chrome 窗口档位")


class IgnoreUpdateBody(BaseModel):
    """忽略版本请求体。

    Attributes:
        version: 要忽略的版本号（`v` 前缀可有可无）；空串 = 撤销忽略。
    """

    version: str = Field("", description="要忽略的版本号，空串表示撤销")


class ApplyUpdateBody(BaseModel):
    """立即更新请求体。

    Attributes:
        version: 目标版本号（`v` 前缀可有可无）；空串 = 接受服务端当下最新的那一版。
    """

    version: str = Field("", description="要更新到的版本号，空串表示最新版")


@router.get("/health")
def health() -> dict[str, Any]:
    """数据存哪、是不是从压缩包里跑的、exe 目录能不能写。"""
    import boss_db

    data_dir = runtime.app_data_dir()
    return {
        "frozen": runtime.is_frozen(),
        "version": APP_VERSION,
        "data_dir": str(data_dir),
        "db": str(boss_db.resolve_db_path()),
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
    """切换取令牌窗口的档位，立即生效（不用重启）。"""
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
    """用可见窗口拉起取令牌的 Chrome（风控滑块场景）；失败抛 `EnvironmentWebError`。"""
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


@router.get("/update")
def update_status(force: bool = False) -> dict[str, Any]:
    """当前版本 / 最新版 / 要不要弹窗；`should_prompt` = 有更新且没被忽略压住。

    检测失败时 `latest` 为空、`error` 带人话，不报 500。
    """
    body = update_check.build_status(force=force)
    body["version"] = APP_VERSION
    return body


@router.post("/update/ignore")
def ignore_update(body: IgnoreUpdateBody) -> dict[str, Any]:
    """记住「忽略此版本」。只点取消不调这个接口，下次照样弹。"""
    ignored = update_check.save_ignored_version(body.version)
    return {
        "ok": True,
        "ignored": ignored or None,
        "message": f"已忽略 {ignored}。" if ignored else "已恢复版本提示。",
    }


@router.get("/update/log")
def update_log(tail: int = updater.LOG_TAIL_BYTES) -> dict[str, Any]:
    """更新过程日志 + 诊断信息，永远 200；诊断只读缓存 `update_check.cached_snapshot`。"""
    limit = max(1, min(int(tail or updater.LOG_TAIL_BYTES), updater.LOG_TAIL_MAX))
    files, hint = updater.read_update_logs(tail_bytes=limit)
    snap = update_check.cached_snapshot()

    frozen = runtime.is_frozen()
    temp_run = runtime.is_temp_run()
    if not frozen:
        install_mode, label = "source", "源码运行（不支持自动更新）"
    elif temp_run:
        install_mode, label = "temp_zip", "压缩包临时运行（请先解压）"
    else:
        install_mode, label = "installed", "安装版（可自动更新）"

    progress = updater.peek_progress()
    return {
        "ok": True,
        "work_dir": str(updater.work_dir()),
        "tail_bytes": limit,
        "files": files,
        "hint": hint,
        "diagnostics": {
            "current": snap["current"],
            "latest": snap["latest"],
            "has_update": snap["has_update"],
            "should_prompt": snap["should_prompt"],
            "ignored": snap["ignored"],
            "error": snap["error"],
            "checked_at": snap["checked_at"],
            "frozen": frozen,
            "temp_run": temp_run,
            "install_mode": install_mode,
            "install_mode_label": label,
            "install_dir": str(runtime.executable_dir()),
            "data_dir": str(runtime.app_data_dir()),
            "can_self_update": snap["can_self_update"],
            "zip_asset": snap["zip_asset"],
            "last_stage": progress.get("stage") or "",
            "last_stage_text": progress.get("text") or "",
        },
    }


@router.post("/update/apply")
def apply_update(body: ApplyUpdateBody) -> dict[str, Any]:
    """点「立即更新」：确认 zip 能拿到后立刻返回，进度全走 WS。

    不做破坏性的事；真删/真换交给 bat。环境不支持时抛 `ConflictError`。
    """
    if not runtime.is_frozen():
        raise ConflictError("开发/源码模式不支持自动更新，请用 git 拉最新代码。")
    if runtime.is_temp_run():
        raise ConflictError(
            "当前是直接在压缩包里跑的，请先把程序解压到本地文件夹再更新。"
        )

    status = update_check.build_status(force=True)
    if not status.get("latest") and status.get("error"):
        raise UpstreamError(f"读不到 Gitee 发行版：{status['error']}")

    zip_asset = status.get("zip_asset")
    if not zip_asset or not zip_asset.get("url"):
        raise ConflictError("这个发行版没附 zip 更新包，请到 Gitee 手动下载覆盖。")

    latest = str(status.get("latest") or "")
    target = (body.version or "").strip().lstrip("vV")
    if target and update_check.compare_versions(target, latest) != 0:
        raise ConflictError(f"版本已变化（现在最新是 v{latest}），请刷新页面后再试。")

    try:
        updater.start_update_job(
            version=latest,
            zip_url=str(zip_asset["url"]),
            zip_name=str(zip_asset.get("name") or ""),
        )
    except updater.UpdateApplyError as exc:
        raise EnvironmentWebError(str(exc)) from exc

    return {
        "ok": True,
        "version": latest,
        "zip_name": zip_asset.get("name") or "",
        "message": "更新已开始，保持页面打开就能看到进度。",
    }


@router.websocket("/update/ws")
async def update_ws(ws: WebSocket) -> None:
    """推送自更新进度。连接先给一帧快照，之后只出不进。

    swap 阶段这个连接一定会断（服务在换自己）；前端靠同端口重连接着走。
    """
    await ws.accept()
    loop = asyncio.get_running_loop()
    queue = updater.subscribe(loop)
    try:
        await ws.send_text(json.dumps(updater.snapshot_progress(), ensure_ascii=False))
        while True:
            event = await queue.get()
            await ws.send_text(json.dumps(event.to_payload(), ensure_ascii=False))
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 - 连接断得难看也不能把服务带崩
        logger.debug("更新进度 WS 异常退出", exc_info=True)
    finally:
        updater.unsubscribe(queue)
