"""CDP 网络抓包：附着到带调试口的 Chrome，把整个浏览器实例的请求落盘。

用法：

    chrome.exe --remote-debugging-port=9222 --user-data-dir=<临时目录>
    python tools/capture_net.py --preset api --out data/capture/try1
    # 在页面里点几下，然后 Ctrl+C 收工

产物在会话目录里：``index.jsonl`` 一行一个请求（请求头/参数/响应头/正文路径），
``bodies/`` 是响应体，``post/`` 是 POST 正文，``websocket.jsonl`` 是 WS 握手与逐帧流水。

**默认档（``--preset all``）严格照「非 JS/CSS/HTML」的字面意思**，图片和字体也会落盘，
抓久了目录会很大。实抓接口和 MQTT over WS 用 ``--preset api``，输出小一个数量级。

**本工具默认只附着，退出时绝不关你那台 Chrome。** 只有 ``--launch``（自己拉起来的那台）
才走 ``boss_jobs.cdp_stoken`` 的退出收尾。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from boss_db import DATA_DIR  # noqa: E402
from boss_jobs import cdp_capture as CC  # noqa: E402
from boss_jobs.cdp_stoken import (  # noqa: E402
    CDP_PORT_ENV,
    DEFAULT_CDP_PORT,
    probe_debug,
)

EXIT_OK = 0
EXIT_ERROR = 1

_EPILOG = """\
预设档位：
  all    默认。只排 HTML/CSS/JS（Document/Stylesheet/Script），其余全留
  media  在 all 之上再排图片/字体/媒体/预检
  api    白名单，只留 XHR/Fetch/EventSource/WebSocket/Prefetch/Other

例子：
  python tools/capture_net.py --preset api                       # 只抓接口与 WS
  python tools/capture_net.py --include-types XHR,Fetch --url /wapi/   # 只要业务接口
  python tools/capture_net.py --duration 30 --out data/capture/login   # 定时抓 30 秒
"""


def _force_utf8_streams() -> None:
    """Windows 控制台默认 GBK，中文会直接 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="capture_net.py",
        description="附着到带调试口的 Chrome，把整个浏览器实例的网络请求落盘",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_EPILOG,
    )
    parser.add_argument(
        "--out", default=None, help="输出会话目录（默认 data/capture/<时间戳>）"
    )
    parser.add_argument("--ws", default="", help="直接给 browser 级 ws 地址，跳过探测")
    parser.add_argument(
        "--port", type=int, default=None, help=f"调试口端口（默认 {CDP_PORT_ENV} 或 {DEFAULT_CDP_PORT}）"
    )
    parser.add_argument(
        "--preset",
        choices=("all", "media", "api"),
        default="all",
        help="过滤档位：all 只排 HTML/CSS/JS（默认）；media 再排图片/字体；api 只留接口与 WS",
    )
    parser.add_argument("--include-types", default="", help="白名单，逗号分隔；优先于排除")
    parser.add_argument(
        "--exclude-types",
        default=None,
        help="排除列表，逗号分隔；传空串 = 一个都不排（抓全量）",
    )
    parser.add_argument("--url", default="", help="只留 URL 含此子串的请求")
    parser.add_argument("--url-regex", default="", help="只留 URL 匹配此正则的请求")
    parser.add_argument("--url-exclude", default="", help="剔除 URL 含此子串的请求")
    parser.add_argument(
        "--flush-every", type=int, default=20, help="websocket.jsonl 攒几行 flush 一次（1 = 逐行）"
    )
    parser.add_argument(
        "--no-post-data",
        action="store_true",
        help="POST 正文缺在事件里时不用 getRequestPostData 回头补",
    )
    parser.add_argument(
        "--wait-for-debugger",
        action="store_true",
        help="新 target 先暂停、订阅好了再放行（能抓全首屏；工具被强杀会把页面卡在调试器上）",
    )
    parser.add_argument("--duration", type=float, default=None, help="抓这么多秒后自动收工")
    parser.add_argument("--max-requests", type=int, default=None, help="抓够这么多条请求就收工")
    parser.add_argument(
        "--launch", action="store_true", help="探测不到调试口时自己拉一台 Chrome"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="打开调试日志")
    return parser


def _port_hint(explicit: int | None) -> str:
    if explicit:
        return str(explicit)
    return os.environ.get(CDP_PORT_ENV, "") or str(DEFAULT_CDP_PORT)


def _warn_unknown_types(args: argparse.Namespace) -> None:
    """类型名拼错的话，用户只会看到「啥也没抓到」，不如当场说一声。"""
    for flag, raw in (("--include-types", args.include_types), ("--exclude-types", args.exclude_types)):
        if not raw:
            continue
        for name in (part.strip() for part in raw.split(",")):
            if name and name not in CC.ALL_RESOURCE_TYPES:
                print(f"⚠ {flag} 里的 {name!r} 不是已知的资源类型，不会被匹配", file=sys.stderr)


def _launch_and_probe(port: int | None, *, timeout: float = 20.0) -> str:
    """自己拉一台 Chrome 再等调试口起来。返回 ws 地址，超时回空串。"""
    from boss_jobs.cdp_stoken import StokenError, launch_chrome

    try:
        launch_chrome(port=port)
    except StokenError as exc:
        print(f"✗ 拉 Chrome 失败：{exc}", file=sys.stderr)
        return ""
    deadline = time.time() + timeout
    while time.time() < deadline:
        ws_url = probe_debug(port)
        if ws_url:
            return ws_url
        time.sleep(0.5)
    return ""


def _print_summary(summary: CC.CaptureSummary) -> None:
    print()
    print(f"✓ 收工（{summary.stop_reason}）")
    print(f"  请求：{summary.requests_total} 条命中，写入 {summary.requests_written} 行，"
          f"过滤掉 {summary.requests_filtered} 条")
    print(f"  正文：{summary.bodies_written} 份，共 {summary.body_bytes} 字节")
    print(f"  WS  ：{summary.ws_connections} 条连接，"
          f"发出 {summary.ws_frames_sent} 帧 / 收到 {summary.ws_frames_recv} 帧")
    if summary.pending or summary.failed:
        print(f"  未完成 {summary.pending} 条，失败 {summary.failed} 条")
    print(f"  输出：{summary.out_dir}")
    if summary.requests_filtered == 0 and summary.requests_written > 200:
        print("  提示：默认档会把图片/字体一并落盘，想瘦身用 --preset api")


def main(argv: list[str] | None = None) -> int:
    _force_utf8_streams()
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    _warn_unknown_types(args)

    filters = CC.filters_for_preset(
        args.preset,
        include_types=args.include_types,
        exclude_types=args.exclude_types,
        url_substr=args.url,
        url_regex=args.url_regex,
        url_exclude=args.url_exclude,
    )
    out_dir = (
        Path(args.out)
        if args.out
        else DATA_DIR / "capture" / time.strftime("%Y%m%d-%H%M%S")
    )
    options = CC.CaptureOptions(
        out_dir=out_dir,
        filters=filters,
        flush_every=args.flush_every,
        fetch_post_data=not args.no_post_data,
    )

    ws_url = args.ws or probe_debug(args.port)
    if not ws_url and args.launch:
        ws_url = _launch_and_probe(args.port)
    if not ws_url:
        print(f"✗ 没找到调试口（端口 {_port_hint(args.port)}）", file=sys.stderr)
        print("  先开一台带调试口的 Chrome：", file=sys.stderr)
        print(
            "    chrome.exe --remote-debugging-port=9222 --user-data-dir=<临时目录>",
            file=sys.stderr,
        )
        print("  或者加 --launch 让本工具自己拉。", file=sys.stderr)
        return EXIT_ERROR

    print(f"✓ 附着 {ws_url}")
    print(f"  输出 {out_dir}（档位 {args.preset}，Ctrl+C 收工）")
    try:
        summary = CC.run_capture(
            ws_url=ws_url,
            options=options,
            wait_for_debugger=args.wait_for_debugger,
            duration=args.duration,
            max_requests=args.max_requests,
        )
    except CC.CaptureError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return EXIT_ERROR
    _print_summary(summary)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
