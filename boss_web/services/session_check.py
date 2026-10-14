"""启动时的登录态自检：拿库里的凭证打一次用户信息接口，别等任务开跑才发现没登录。

``GET /api/auth/status`` 只看库里有没有 token/Cookie，**不问服务端**。凭证过期后
它照样报「已登录」，用户点「开始抓取」才撞见「当前登录状态已失效」——那时候任务
已经起了一半。

启动时自检一次（:func:`schedule_startup_check`）：

============ ==========================================================
``valid``    服务端认；库里 ``user`` 顺手刷成最新的
``expired``  服务端明说登录失效 → **清掉库里那份**（更新数据库，别再谎报已登录）
``absent``   库里本来就没有登录态，不发请求
``unknown``  网络不通 / 别的错 → **不动库**，断网不该把好凭证抹掉
============ ==========================================================

「是不是真的失效」有判据：跟 :attr:`boss_jobs.errors.JobApiError.is_session_expired`
同规矩——话术里明说了登录问题才算。``getUserInfo`` 回个 ``code 1``「系统繁忙」
也走 :class:`~boss_login.errors.SessionExpired`（那边把所有非 0 都归成它），
一律清库会把还在有效期的登录态白白抹掉。

测完才开浏览器（见 ``cli._open_browser_when_ready``）：网页一打开看到的会话
状态就是定论，不用再刷新一次才正确。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

#: 自检结论
ST_VALID = "valid"
ST_EXPIRED = "expired"
ST_ABSENT = "absent"
ST_UNKNOWN = "unknown"

#: 话术判据：这些词出现在接口报错里，才算「登录失效」（与 boss_jobs / boss_filter 同规矩）
_SESSION_HINTS = ("登录", "失效", "过期", "未登录", "auth")

#: 等自检出结果的上限（秒）。超过就先开浏览器，别让用户对着空屏干等
CHECK_TIMEOUT = 20.0

_lock = threading.Lock()
#: 最近一次自检结果；``checked_at`` 为 0 表示还没测过
_cache: dict[str, Any] = {
    "checked_at": 0.0,
    "state": "",
    "logged_in": False,
    "user": {},
    "phone_masked": "",
    "message": "",
    "error": None,
    "cleared": False,
}


def reset_cache() -> None:
    """清掉缓存。给测试用。"""
    with _lock:
        _cache.update(
            checked_at=0.0,
            state="",
            logged_in=False,
            user={},
            phone_masked="",
            message="",
            error=None,
            cleared=False,
        )


def snapshot() -> dict[str, Any]:
    """最近一次自检结果的副本；还没测过时 ``state`` 为空串。"""
    with _lock:
        return dict(_cache)


def _store(**fields: Any) -> dict[str, Any]:
    with _lock:
        _cache.update(fields)
        _cache["checked_at"] = time.time()
        return dict(_cache)


def looks_like_session_expired(message: str) -> bool:
    """报错话术里是不是明说了登录问题。

    ``getUserInfo`` 的失败会被 :meth:`~boss_login.client.ZhipinLoginClient.fetch_user_info`
    一律包成 :class:`~boss_login.errors.SessionExpired`，但那不代表「凭证过期」——
    「系统繁忙」「接口不存在」也在里面。只有话术点名了登录/失效/过期才清库。
    """
    text = str(message or "")
    return any(hint in text for hint in _SESSION_HINTS)


def check_now(
    *,
    session_path: Any = None,
    client_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """查一次库里的登录态还作不作数；结果写进模块缓存，并按需改库。

    :param client_factory: 造客户端的钩子（测试注入假客户端）。默认
        :func:`boss_login.create_client`——自动带上落盘的 Cookie/token。
    """
    from boss_login.errors import BossLoginError
    from boss_login.session import clear_session, load_session, save_session

    stored = load_session(session_path)
    if stored.is_empty:
        return _store(
            state=ST_ABSENT,
            logged_in=False,
            user={},
            phone_masked="",
            message="库里没有登录态",
            error=None,
            cleared=False,
        )

    factory = client_factory
    if factory is None:
        from boss_login import create_client as factory  # noqa: PLC0415 - 避免包外拖依赖

    try:
        client = factory(session_path=session_path) if session_path is not None else factory()
        user = client.fetch_user_info()
    except BossLoginError as exc:
        return _handle_failure(exc, stored, session_path)
    except Exception as exc:  # noqa: BLE001 - 自检永远不该拦住启动
        logger.warning("登录态自检出了意外，按未知处理", exc_info=True)
        return _store(
            state=ST_UNKNOWN,
            logged_in=bool(stored.cookies or stored.token),
            user=dict(stored.user or {}),
            phone_masked=stored.phone_masked,
            message=f"没测到（{exc}）",
            error=str(exc),
            cleared=False,
        )

    # 服务端认：把库里那份 user 刷成最新的（改名 / 换昵称后界面跟着变）
    user_payload = {
        "user_id": user.user_id or (stored.user or {}).get("user_id", ""),
        "name": user.name or (stored.user or {}).get("name", ""),
        "identity": user.identity or (stored.user or {}).get("identity", ""),
    }
    if (stored.user or {}).get("is_new_user"):
        user_payload["is_new_user"] = True
    if user_payload != (stored.user or {}):
        try:
            stored.user = user_payload
            save_session(stored, session_path)
        except Exception:  # noqa: BLE001 - 刷用户名失败不影响「登录有效」这个结论
            logger.warning("刷新库里的用户信息失败", exc_info=True)

    return _store(
        state=ST_VALID,
        logged_in=True,
        user=user_payload,
        phone_masked=stored.phone_masked or user.phone_masked,
        message=f"已登录（{user.name or user.user_id or '用户'}）",
        error=None,
        cleared=False,
    )


def _handle_failure(
    exc: BossLoginError,
    stored: Any,
    session_path: Any,
) -> dict[str, Any]:
    """自检失败：只有「话术点名登录失效」才清库，其余一律原样保留。"""
    from boss_login.session import clear_session

    text = str(exc)
    if not looks_like_session_expired(text):
        logger.info("登录态自检没测出结论（%s），库里那份不动", text)
        return _store(
            state=ST_UNKNOWN,
            logged_in=bool(stored.cookies or stored.token),
            user=dict(stored.user or {}),
            phone_masked=stored.phone_masked,
            message=f"没测到（{text}）",
            error=text,
            cleared=False,
        )

    cleared = False
    try:
        cleared = clear_session(session_path)
    except Exception:  # noqa: BLE001 - 清不掉也要如实报告「已失效」
        logger.warning("登录失效但清理状态库失败", exc_info=True)

    logger.info("登录态已失效（%s），本地登录态%s", text, "已清除" if cleared else "没能清掉")
    return _store(
        state=ST_EXPIRED,
        logged_in=False,
        user={},
        phone_masked="",
        message="登录已失效，请到「登录」页重新登录",
        error=text,
        cleared=cleared,
    )


def schedule_startup_check(
    *,
    on_done: Callable[[dict[str, Any]], None] | None = None,
    session_path: Any = None,
    client_factory: Callable[..., Any] | None = None,
) -> threading.Event:
    """后台跑一次 :func:`check_now`；返回的 Event 在查完（或彻底失败）后置位。

    ``on_done(result)`` 是可选回调——CLI 拿它把「登录状态」打到控制台。守护线程
    + 完全吞异常：登录自检永远不该拦住服务起来。置位是**无条件**的，哪怕线程
    半路炸了也要放行开浏览器，否则用户会对着一个永远打不开的页面。
    """
    done = threading.Event()

    def _run() -> None:
        try:
            result = check_now(session_path=session_path, client_factory=client_factory)
        except Exception:  # noqa: BLE001 - 自检失败静默，缓存里留着「没测过」
            logger.warning("启动时的登录态自检失败", exc_info=True)
            done.set()
            return
        if on_done is not None:
            try:
                on_done(result)
            except Exception:  # noqa: BLE001 - 打印失败更不该影响服务
                logger.debug("登录态结果回调失败", exc_info=True)
        done.set()

    threading.Thread(target=_run, name="session-check", daemon=True).start()
    return done


def format_console_lines(result: dict[str, Any]) -> list[str]:
    """控制台按行打印「登录状态」。跟横幅左对齐（``键 13 列 + 值``）。"""
    state = str(result.get("state") or "")
    if state == ST_VALID:
        line = f"  登录状态     {result.get('message') or '已登录'}"
    elif state == ST_EXPIRED:
        line = "  登录状态     登录已失效，本地登录态已清除，请到网页重新登录"
    elif state == ST_ABSENT:
        line = "  登录状态     未登录（库里没有登录态）"
    else:
        error = result.get("error") or result.get("message") or "未知原因"
        line = f"  登录状态     没测到（{error}）——登录态原样保留"
    return [line]
