"""把底层技术异常翻译成人话，外加 Chrome 环境检测。

``boss_jobs`` 抛的 ``StokenError`` 是给开发者看的——裸类名、``ws://127.0.0.1:9222/
devtools/browser/<uuid>``、CDP 方法名。普通用户看到这些只会去问「这是啥」。

这一层只做翻译和探测，**不改 ``boss_jobs`` 一行**：错误从哪里冒出来就在哪里翻
（Web 端走异常处理器，后台线程直接调 :func:`humanize`）。
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

#: ``(原文里的特征串, 给用户看的话)``，**顺序敏感**：从上往下第一条命中为准，
#: 所以更具体的规则要排在更泛的前面。
#:
#: ⚠️ 「找不到 Chrome」必须排在「BOSS_CHROME_BIN」**前面**：前者那句原文本身就
#: 把环境变量当兜底方案提了一嘴（``…或用 BOSS_CHROME_BIN 指到 chrome…``），
#: 排在后面的话永远轮不到它，用户拿到的提示会变成让他去设环境变量。
_RULES: tuple[tuple[str, str], ...] = (
    (
        "找不到 Chrome",
        "没找到 Google Chrome 浏览器。搜「Chrome 下载」装一个（第一条就是官网），"
        "装完回到「首页」点一下「重新检测」，不用重启程序。",
    ),
    (
        "BOSS_CHROME_BIN",
        "程序里设了一个指向 Chrome 的路径，但那个文件不存在。"
        "请把「首页 → 环境自检」里的 Chrome 路径设置改回「自动检测」。",
    ),
    (
        "连不上 CDP",
        "连不上取令牌用的 Chrome。多半是上一次的 Chrome 还挂在后台占着端口。\n"
        "把任务栏里的 Chrome 全部关掉，再重试一次。",
    ),
    (
        "调试口没开",
        "Chrome 打开了，但调试端口没起来——常见原因是 Chrome 卡在启动页，"
        "或被安全软件拦住了。关掉 Chrome 重试一次。",
    ),
    (
        "拉不起 Chrome",
        "Chrome 启动失败。先手动打开一次 Chrome 确认它能用，"
        "不行就重启电脑再试。",
    ),
    (
        "建 Chrome 专属 profile 失败",
        "没法创建 Chrome 的工作目录——磁盘空间不足，或者程序没有写入权限。",
    ),
    (
        "没等到站点写出",
        "Chrome 打开了，但没能拿到安全令牌。多半是登录状态过期了——"
        "请到「登录」页重新登录一次再抓。",
    ),
    (
        "CDP ",
        "和 Chrome 的通信中断了（发送 / 等回包 / 超时）。"
        "关掉所有 Chrome 窗口，重新跑一次。",
    ),
    (
        "websocket-client",
        "程序文件不完整（缺少组件）。请重新下载完整的程序。",
    ),
)


def humanize(exc: BaseException | str) -> str:
    """把技术异常/原文换成用户能照着做的话。

    认不出来就原样返回——宁可露出原始信息，也不要吞掉线索。
    """
    text = str(exc)
    for needle, friendly in _RULES:
        if needle in text:
            return friendly
    return text


# --------------------------------------------------------------------------- #
# Chrome 检测
# --------------------------------------------------------------------------- #


def current_mode() -> str:
    """当前生效的 Chrome 窗口档位（环境变量优先，否则 ``boss_jobs`` 的默认值）。"""
    from boss_jobs.cdp_stoken import CHROME_MODE_ENV, DEFAULT_CHROME_MODE

    env = os.environ.get(CHROME_MODE_ENV, "").strip().lower()
    return env or DEFAULT_CHROME_MODE


def chrome_status() -> dict[str, Any]:
    """界面用的 Chrome 检测结果。**不启动任何东西**，探测失败也照常返回。"""
    from boss_jobs.cdp_stoken import CHROME_MODES, find_chrome

    status: dict[str, Any] = {
        "found": False,
        "path": "",
        "message": "",
        "mode": current_mode(),
        "modes": list(CHROME_MODES),
    }
    try:
        path = find_chrome()
    except Exception as exc:  # noqa: BLE001 - 探测失败就是要如实告诉用户
        status["message"] = humanize(exc)
    else:
        status.update({"found": True, "path": path, "message": "已找到 Chrome，可以正常抓取。"})
    return status
