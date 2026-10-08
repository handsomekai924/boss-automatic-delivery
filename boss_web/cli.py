"""网页控制台的命令行入口：``python -m boss_web``。"""

from __future__ import annotations

import argparse
import logging
import webbrowser

from . import config as C


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="boss_web",
        description="BOSS 直聘可视化控制台（登录 / 抓取 / LLM / 简历分析）",
    )
    parser.add_argument("--host", default=C.DEFAULT_HOST, help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=C.DEFAULT_PORT, help="端口（默认 8787）")
    parser.add_argument("--reload", action="store_true", help="改代码自动重启（开发用）")
    parser.add_argument(
        "--no-browser", action="store_true", help="启动后不自动打开浏览器"
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug"],
        help="uvicorn 日志级别",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    C.ensure_data_dirs()

    try:
        import uvicorn
    except ImportError:  # pragma: no cover - 依赖没装
        print("缺少 uvicorn：请先 `pip install -r requirements.txt`")
        return 1

    url = f"http://{args.host}:{args.port}"
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - 拉不起浏览器不影响服务
            pass

    print(f"BOSS 控制台已就绪 → {url}   （Ctrl+C 退出）")
    uvicorn.run(
        "boss_web.app:app_factory",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
    )
    return 0
