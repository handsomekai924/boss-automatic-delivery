"""网页控制台的命令行入口：`python -m boss_web`。

打包成 exe 后这个入口由 `run_boss.py` 调用，差别在 uvicorn 启动方式（见 `main` 的 `frozen` 分支）。
"""

from __future__ import annotations

import argparse
import logging
import socket
import sys
import threading
import time
import webbrowser

from . import config as C
from .runtime import bootstrap, is_frozen, log_dir


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。"""
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


def _port_free(host: str, port: int) -> bool:
    """试绑一下看端口空不空；绑定后立刻释放。"""
    with socket.socket() as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def pick_port(host: str, preferred: int, tries: int = 15) -> int:
    """从 `preferred` 起顺延找空闲端口；连试 `tries` 个都被占就让系统派一个。"""
    for offset in range(tries):
        candidate = preferred + offset
        if _port_free(host, candidate):
            return candidate
    with socket.socket() as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _restore_app_settings() -> None:
    """把存过的 Chrome 窗口档位写回环境变量；读失败按默认继续。"""
    try:
        from .services.app_settings_store import restore_on_startup

        restore_on_startup()
    except Exception:  # noqa: BLE001 - 设置读不了不该拦住启动
        logging.getLogger(__name__).warning("应用设置读取失败，按默认继续", exc_info=True)


def _setup_logging(level: str) -> None:
    """配置控制台 + `data/logs/boss.log` 日志。"""
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
    """打印启动横幅。"""
    from . import __version__

    print("=" * 60)
    print(f"  BOSS 自动投递助手已启动  v{__version__}")
    print(f"  控制台地址   {url}")
    print(f"  数据目录     {info.get('data_dir', C.DATA_DIR)}")
    if info.get("portable") == "0":
        print("  ⚠ 程序所在目录写不进去，数据已改存到上面这个目录")
    if info.get("warning") == "temp_run":
        print("  ⚠ 程序在临时目录里运行——请先把 exe 解压到桌面再打开，否则数据会丢")
    print("  登录状态     正在校验…")
    print("  版本对比     正在查 Gitee 发行版…")
    print("  关掉这个窗口就是退出程序")
    print("=" * 60)


def _print_session_status(result: dict) -> None:
    """把登录态自检结果打到控制台。"""
    from .services.session_check import format_console_lines

    for line in format_console_lines(result):
        print(line)
    sys.stdout.flush()


def _print_version_status(status: dict) -> None:
    """把版本信息按行打到控制台（当前 / 发行 / 是否需要更新）。"""
    from .services.update_check import format_console_lines

    for line in format_console_lines(status):
        print(line)
    sys.stdout.flush()


def _self_check(info: dict[str, str]) -> int:
    """打印路径 / 环境自检结果；前端资源缺失时返回 1。

    Args:
        info: `bootstrap` 返回的路径摘要。

    Returns:
        0 表示前端资源在位，1 表示缺失。
    """
    import boss_db

    from . import __version__
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

    try:
        from .services.update_check import build_status

        _print_version_status(build_status(force=True))
    except Exception as exc:  # noqa: BLE001 - 自检不该被网络问题判失败
        print(f"  当前版本     v{__version__}")
        print("  发行版本     ——")
        print(f"  是否需要更新 没测到（{exc}）")

    ok = (static / "index.html").is_file()
    print("-" * 60)
    print("自检结果     通过" if ok else "自检结果     前端资源缺失，打包参数有问题")
    return 0 if ok else 1


def _open_browser_when_ready(
    server,
    url: str,
    ready: "threading.Event | None" = None,
    timeout: float = 30.0,
) -> None:
    """等服务起来、登录态自检出结果后再打开浏览器。

    Args:
        server: uvicorn Server 实例。
        url: 控制台地址。
        ready: 登录态自检完成信号；出结果或超时后再开浏览器。
        timeout: 最长等待秒数。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if getattr(server, "should_exit", False):
            return
        if getattr(server, "started", False) and (ready is None or ready.is_set()):
            break
        time.sleep(0.15)
    else:
        return
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001 - 拉不起浏览器不影响服务
        pass


def main(argv: list[str] | None = None) -> int:
    """启动网页控制台；`--self-check` 时只做自检。

    Args:
        argv: 命令行参数；`None` 时读 `sys.argv`。

    Returns:
        进程退出码。
    """
    args = build_parser().parse_args(argv)
    info = bootstrap()
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
    try:
        from .services.updater import bind_server

        bind_server(server)
    except Exception:  # noqa: BLE001 - 挂不上不该拦住启动
        logging.getLogger(__name__).warning("挂载自更新退出钩子失败", exc_info=True)
    try:
        from .services.update_check import schedule_startup_check

        schedule_startup_check(on_done=_print_version_status)
    except Exception:  # noqa: BLE001 - 预热失败绝不拦住启动
        logging.getLogger(__name__).warning("版本检测预热失败", exc_info=True)
    try:
        from .services.update_check import schedule_periodic_check

        schedule_periodic_check()
    except Exception:  # noqa: BLE001 - 周期检测起不来也不该拦住启动
        logging.getLogger(__name__).warning("版本周期检测启动失败", exc_info=True)

    session_ready = threading.Event()
    try:
        from .services.session_check import schedule_startup_check as schedule_session_check

        session_ready = schedule_session_check(on_done=_print_session_status)
    except Exception:  # noqa: BLE001 - 自检失败绝不拦住启动
        session_ready.set()
        logging.getLogger(__name__).warning("登录态自检预热失败", exc_info=True)

    if not args.no_browser:
        threading.Thread(
            target=_open_browser_when_ready,
            args=(server, url, session_ready),
            daemon=True,
        ).start()

    _banner(url, info)
    server.run()
    return 0
