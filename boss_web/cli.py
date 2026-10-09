"""网页控制台的命令行入口：``python -m boss_web``。

打包成 exe 后这个入口由 ``run_boss.py`` 调用，两者的差别只在 uvicorn 的启动
方式（见 :func:`main` 里的 ``frozen`` 分支）。
"""

from __future__ import annotations

import argparse
import logging
import socket
import threading
import time
import webbrowser

from . import config as C
from .runtime import bootstrap, is_frozen, log_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="boss_web",
        description="BOSS 直聘可视化控制台（登录 / 抓取 / LLM / 简历分析）",
    )
    parser.add_argument("--host", default=C.DEFAULT_HOST, help="监听地址（默认 127.0.0.1）")
    parser.add_argument(
        "--port",
        type=int,
        default=C.DEFAULT_PORT,
        help="端口（默认 8787；被占用就往后顺延）",
    )
    parser.add_argument(
        "--reload", action="store_true", help="改代码自动重启（仅开发用，打包后无效）"
    )
    parser.add_argument(
        "--no-browser", action="store_true", help="启动后不自动打开浏览器"
    )
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="只打印路径 / 环境的自检结果后退出，不启动服务",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug"],
        help="日志级别",
    )
    return parser


# --------------------------------------------------------------------------- #
# 端口
# --------------------------------------------------------------------------- #


def _port_free(host: str, port: int) -> bool:
    """试绑一下看看端口空不空。绑定后立刻释放——存在极小的竞争窗口，
    但比「先启动再报 address already in use」对用户友好得多。"""
    with socket.socket() as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def pick_port(host: str, preferred: int, tries: int = 15) -> int:
    """从 ``preferred`` 起顺延找空闲端口；连试 ``tries`` 个都被占就让系统派一个。

    用户双击 exe 时经常会开两份（或者上一个没关干净），写死端口会直接启动失败，
    而失败信息对非技术用户毫无意义。
    """
    for offset in range(tries):
        candidate = preferred + offset
        if _port_free(host, candidate):
            return candidate
    with socket.socket() as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


# --------------------------------------------------------------------------- #
# 日志 / 横幅
# --------------------------------------------------------------------------- #


def _restore_app_settings() -> None:
    """把存过的 Chrome 窗口档位写回环境变量。读不出来不算致命，按默认走。"""
    try:
        from .services.app_settings_store import restore_on_startup

        restore_on_startup()
    except Exception:  # noqa: BLE001 - 设置读不了不该拦住启动
        logging.getLogger(__name__).warning("应用设置读取失败，按默认继续", exc_info=True)


def _setup_logging(level: str) -> None:
    """控制台 + ``data/logs/boss.log``。日志文件写不了不该拦住服务。"""
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        from logging.handlers import RotatingFileHandler

        handlers.append(
            RotatingFileHandler(
                log_dir() / "boss.log", maxBytes=1 << 20, backupCount=3, encoding="utf-8"
            )
        )
    except OSError:  # pragma: no cover - 磁盘满 / 权限问题
        pass
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


def _banner(url: str, info: dict[str, str]) -> None:
    print("=" * 60)
    print("  BOSS 自动投递助手已启动")
    print(f"  控制台地址   {url}")
    print(f"  数据目录     {info.get('data_dir', C.DATA_DIR)}")
    if info.get("portable") == "0":
        print("  ⚠ 程序所在目录写不进去，数据已改存到上面这个目录")
    if info.get("warning") == "temp_run":
        print("  ⚠ 程序在临时目录里运行——请先把 exe 解压到桌面再打开，否则数据会丢")
    print("  关掉这个窗口就是退出程序")
    print("=" * 60)


def _self_check(info: dict[str, str]) -> int:
    """把打包后「开发态验不了」的东西打出来：资源根、前端目录、库路径、Chrome。

    在 cmd 里跑 ``BossAutoDelivery.exe --self-check`` 就能核对，不用真开浏览器。
    """
    import boss_db

    from .runtime import resource_root

    static = C.STATIC_DIR
    print(f"打包形态     {'exe（frozen）' if is_frozen() else '源码（dev）'}")
    print(f"资源根       {resource_root()}")
    print(f"前端目录     {static}  【{'正常' if (static / 'index.html').is_file() else '缺失！'}】")
    print(f"数据目录     {info.get('data_dir', C.DATA_DIR)}")
    print(f"状态库       {boss_db.resolve_db_path()}")
    print(f"日志目录     {log_dir()}")

    try:
        from boss_jobs.cdp_stoken import find_chrome

        print(f"Chrome       {find_chrome()}")
    except Exception as exc:  # noqa: BLE001 - 自检就是要如实报告任何失败
        print(f"Chrome       没找到 —— {exc}")

    ok = (static / "index.html").is_file()
    print("-" * 60)
    print("自检结果     通过" if ok else "自检结果     前端资源缺失，打包参数有问题")
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# 启动
# --------------------------------------------------------------------------- #


def _open_browser_when_ready(server, url: str, timeout: float = 30.0) -> None:
    """等 uvicorn 真的起来了再开浏览器。

    原来是在 ``uvicorn.run`` **之前**开，用户会先看到一个连接被拒的页面，
    得手动刷新——对不懂的人是「这软件坏了」。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if getattr(server, "should_exit", False):
            return
        if getattr(server, "started", False):
            break
        time.sleep(0.15)
    else:
        return  # 超时还没起来：多半是启动就失败了，横幅里有日志
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001 - 拉不起浏览器不影响服务
        pass


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    info = bootstrap()
    # 存过的 Chrome 窗口档位要写回环境变量。必须早于任何取令牌的调用——
    # boss_jobs 是在调用时才读的，晚一步就白设。
    _restore_app_settings()
    _setup_logging(args.log_level)

    try:
        import uvicorn
    except ImportError:  # pragma: no cover - 依赖没装
        print("缺少 uvicorn：请先 `pip install -r requirements.txt`")
        return 1

    if args.self_check:
        return _self_check(info)

    port = pick_port(args.host, args.port)
    url = f"http://{args.host}:{port}"

    if is_frozen():
        # 冻结环境里不能用 Uvicorn 的字符串导入 + reload：那会 re-exec
        # sys.executable 起子进程，而 exe 并不是一个有意义的 Python 解释器。
        from .app import create_app

        config = uvicorn.Config(
            create_app(), host=args.host, port=port, log_level=args.log_level
        )
    else:
        config = uvicorn.Config(
            "boss_web.app:app_factory",
            factory=True,
            host=args.host,
            port=port,
            reload=args.reload,
            log_level=args.log_level,
        )

    server = uvicorn.Server(config)
    if not args.no_browser:
        threading.Thread(
            target=_open_browser_when_ready, args=(server, url), daemon=True
        ).start()

    _banner(url, info)
    server.run()
    return 0
