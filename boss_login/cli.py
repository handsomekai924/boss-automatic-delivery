"""命令行入口。

    python -m boss_login login  --phone 13800138000     # 完整流程（发码 → 交互式输入验证码 → 登录）
    python -m boss_login whoami                         # 查看当前登录用户
    python -m boss_login logout                         # 退出并清除本地登录态
    python -m boss_login probe                          # 探测真实接口路径

接口路径未经离线核实，如线上已变更，用 ``--endpoint-send`` / ``--endpoint-login`` 覆盖，
或先跑 ``probe`` 确认。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from . import __version__, create_client, persist_login
from .client import normalize_phone
from .config import AUTH_COOKIES, RISK_CODE_SLIDER, SLIDER_HELPER_PORT, SLIDER_HELPER_TIMEOUT
from .errors import (
    AccountBlocked,
    ApiError,
    BossLoginError,
    RateLimited,
    RiskControlRequired,
    SessionExpired,
    TransportError,
    ValidationError,
)
from .session import DEFAULT_SESSION_PATH, clear_session, load_session
from .verify import (
    SliderChallenge,
    SliderHelperError,
    SliderSolution,
    SliderSolver,
    solve_via_helper,
)

#: 退出码，便于脚本判断失败原因
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_VALIDATION = 2
EXIT_RATE_LIMITED = 3
EXIT_RISK_CONTROL = 4
EXIT_BLOCKED = 5

BANNER = """\
─────────────────────────────────────────────────────────────
 BOSS直聘 短信登录客户端 · 仅供本人账号使用
─────────────────────────────────────────────────────────────"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="boss_login",
        description="BOSS直聘用户端手机验证码登录（Python 实现）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--version", action="version", version=f"boss_login {__version__}")
    _add_global_options(parser, suppress_defaults=False)

    sub = parser.add_subparsers(dest="command", required=True)
    # 全局开关在子命令后也认（`boss_login login -v` 和 `boss_login -v login` 都行）。
    # 用 parents + SUPPRESS：没在子命令这层给的选项不去覆盖顶层已经给过的值。
    global_opts = _global_options_parent()

    login = sub.add_parser("login", help="完整登录流程（发码 → 输入验证码 → 登录）", parents=[global_opts])
    login.add_argument("--phone", required=True, help="手机号")
    login.add_argument("--dial", default="86", help="国家区号，默认 86")
    login.add_argument("--scene", default="login", choices=["login", "register", "bind"])
    login.add_argument("--code", default=None, help="直接提供验证码（省略则交互式输入）")
    login.add_argument("--verify-token", default=None, help="人工完成安全验证后取得的票据")
    login.add_argument("--no-save", action="store_true", help="登录成功后不写入本地登录态")
    _add_slider_options(login)

    sub.add_parser("whoami", help="查看当前登录用户", parents=[global_opts])

    logout = sub.add_parser("logout", help="退出登录并清除本地登录态", parents=[global_opts])
    logout.add_argument("--local-only", action="store_true", help="只清本地，不调服务端登出")

    probe = sub.add_parser("probe", help="探测短信接口的真实路径", parents=[global_opts])
    probe.add_argument("--json", action="store_true", help="以 JSON 输出")

    return parser


def _global_options_parent() -> argparse.ArgumentParser:
    """给子命令挂一份全局开关，带 SUPPRESS 默认值。

    argparse 的子解析器会用自己那份默认值覆盖顶层同名选项，所以这里必须
    ``default=argparse.SUPPRESS``：不写就完全不动那个属性，顶层的值得以保留。
    """
    parent = argparse.ArgumentParser(add_help=False)
    _add_global_options(parent, suppress_defaults=True)
    return parent


def _add_global_options(parser: argparse.ArgumentParser, *, suppress_defaults: bool) -> None:
    """全局开关，顶层与各子命令共用一套文案。"""
    default = argparse.SUPPRESS if suppress_defaults else None

    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        **({"default": argparse.SUPPRESS} if suppress_defaults else {}),
        help="输出调试日志（含请求重试细节）",
    )
    parser.add_argument(
        "--session-file",
        default=str(DEFAULT_SESSION_PATH) if not suppress_defaults else default,
        help=f"登录态文件路径（默认 {DEFAULT_SESSION_PATH}）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0 if not suppress_defaults else default,
        help="单次请求超时秒数",
    )
    parser.add_argument(
        "--base-url",
        default=default,
        help="站点根地址（默认 https://www.zhipin.com，可指向本地假服务端做离线演练）",
    )
    parser.add_argument("--endpoint-send", default=default, help="覆盖发送验证码接口路径")
    parser.add_argument("--endpoint-login", default=default, help="覆盖登录接口路径")


def _add_slider_options(parser: argparse.ArgumentParser) -> None:
    """滑块相关的开关，给 login 用（命中滑块时自动衔接）。"""
    parser.add_argument(
        "--no-helper",
        action="store_true",
        help="不拉起本地帮助页（命中滑块时直接报错退出）",
    )
    parser.add_argument(
        "--helper-port",
        type=int,
        default=None,
        help="本地帮助页端口（默认 8766；0 表示交给系统分配）",
    )
    parser.add_argument(
        "--helper-timeout",
        type=float,
        default=None,
        help="等人工拖完滑块的上限秒数（默认 300）",
    )
    parser.add_argument(
        "--no-open-browser",
        action="store_true",
        help="不自动打开浏览器，只打印帮助页地址",
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)

    endpoints = {}
    if args.endpoint_send:
        endpoints["send_sms_code"] = args.endpoint_send
    if args.endpoint_login:
        endpoints["login_by_sms"] = args.endpoint_login

    client = create_client(
        session_path=args.session_file,
        timeout=args.timeout,
        endpoints=endpoints or None,
        **({"base_url": args.base_url} if args.base_url else {}),
    )

    handlers = {
        "login": _cmd_login,
        "whoami": _cmd_whoami,
        "logout": _cmd_logout,
        "probe": _cmd_probe,
    }
    try:
        return handlers[args.command](args, client)
    except ValidationError as exc:
        print(f"✗ 参数有误：{exc.message}", file=sys.stderr)
        return EXIT_VALIDATION
    except RateLimited as exc:
        print(f"✗ 发送过于频繁：{exc.message}", file=sys.stderr)
        return EXIT_RATE_LIMITED
    except SliderHelperError as exc:
        print(f"✗ 滑块没过成：{exc.message}", file=sys.stderr)
        print(
            "  可以重跑一次（会重新拉取挑战），或用 --helper-port 换个端口避开占用。",
            file=sys.stderr,
        )
        return EXIT_RISK_CONTROL
    except RiskControlRequired as exc:
        _print_risk_control(exc)
        return EXIT_RISK_CONTROL
    except AccountBlocked as exc:
        print(f"✗ 账号或 IP 已被限制：{exc.message}", file=sys.stderr)
        print("  请稍后再试；如持续出现，请联系客服 400-065-5799。", file=sys.stderr)
        return EXIT_BLOCKED
    except SessionExpired as exc:
        print(f"✗ 未登录或登录态已失效：{exc.message}", file=sys.stderr)
        return EXIT_ERROR
    except TransportError as exc:
        print(f"✗ 网络异常：{exc.message}", file=sys.stderr)
        return EXIT_ERROR
    except ApiError as exc:
        print(f"✗ 接口返回失败（code={exc.code}）：{exc.message}", file=sys.stderr)
        return EXIT_ERROR
    except BossLoginError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return EXIT_ERROR


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #


def _cmd_login(args: argparse.Namespace, client: Any) -> int:
    print(BANNER)
    phone = normalize_phone(args.phone)
    solver = _make_solver(args)

    def code_provider(attempt: int, ticket: Any) -> str | None:
        if args.code and attempt == 1:
            return args.code
        print()
        print(f"  已向 {ticket.phone_masked if ticket else phone} 发送验证码（第 {attempt} 次尝试）")
        value = _prompt_code()
        if value is None:
            return None
        return value

    def on_event(name: str, payload: dict[str, Any]) -> None:
        if name == "code_sent":
            print(f"✓ 验证码已发送至 {payload['phone']}，{payload['retry_after']} 秒后可重发")
        elif name == "cooldown_reused":
            print(f"⏳ 仍在冷却期（剩余 {payload['retry_after']} 秒），沿用刚才那条验证码")
        elif name == "code_rejected":
            print(f"✗ {payload['message']}")
        elif name == "code_resent":
            print(f"↻ 已重新发送验证码至 {payload['phone']}")
        elif name == "slider_required":
            print("⚠ 触发滑块验证，正在拉取挑战…")
        elif name == "slider_challenge":
            print(f"  已取到挑战 gt={payload.get('gt', '')[:12]}…")
        elif name == "slider_helper_started":
            print(f"  滑块帮助页：{payload.get('url')}")
            print("  请在浏览器里拖动滑块（官方极验组件），完成后本页会自动关闭流程。")
        elif name == "slider_solved":
            print("  ✓ 滑块已拖完，票据已拿到")
        elif name == "slider_retry":
            print("  ↻ 带着票据重试刚才被拦下的请求…")

    result = client.run_sms_login(
        phone,
        code_provider,
        dial_code=args.dial,
        scene=args.scene,
        verify_token=args.verify_token,
        slider_solver=solver,
        on_event=on_event,
    )

    print()
    print("✓ 登录成功")
    if result.user.name:
        print(f"  用户：{result.user.name}（ID: {result.user.user_id or '未知'}）")
    print(f"  手机：{result.user.phone_masked}")
    if result.is_new_user:
        print("  说明：该手机号首次验证，已自动完成注册")
    print(f"  token：{_short(result.token) if result.token else '（以 Cookie 鉴权）'}")
    # 真实站点的登录响应体里没有 token，鉴权靠 Set-Cookie；名字可能不在
    # 已知名单里。把这次实际拿到的 Cookie 名打出来，好据此补 AUTH_COOKIES。
    if result.new_cookies:
        print(f"  本次登录种下的 Cookie：{sorted(result.new_cookies)}")
    elif result.cookies:
        print(f"  Cookie：{sorted(result.cookies)}")

    if args.no_save:
        print("  本地登录态未保存（--no-save）")
    else:
        path = persist_login(client, result, session_path=args.session_file)
        print(f"  登录态已保存至 {path}")
    return EXIT_OK


def _cmd_whoami(args: argparse.Namespace, client: Any) -> int:
    path = Path(args.session_file) if args.session_file else DEFAULT_SESSION_PATH
    stored = load_session(args.session_file)

    if not client.is_logged_in():
        # 一句话「没有登录态」没法排查：是文件没落盘，还是落了但凭证不认？
        print("✗ 本地没有登录态，请先执行：python -m boss_login login --phone <手机号>")
        print(f"  查过的位置：{path}")
        if not path.exists():
            print("  原因：文件不存在——登录流程没有落盘。")
            print("  多半是 login 那步就抛了错（比如接口回了成功却没换到凭证）。")
            print("  把 login 命令的完整输出贴出来，对着响应形状改。")
        elif stored.is_empty:
            print("  原因：文件存在，但里面既没有 token 也没有 Cookie。")
        else:
            names = sorted(stored.cookies)
            print(
                f"  原因：文件里 token={'有' if stored.token else '无'}、"
                f"Cookie={names or '（无）'}，但没有一个是已知鉴权 Cookie。"
            )
            print(f"  已知鉴权 Cookie 名：{list(AUTH_COOKIES)}")
            print("  上面那些名字若确实是登录凭证，把它们补进 config.AUTH_COOKIES 即可。")
        return EXIT_ERROR

    try:
        user = client.fetch_user_info()
    except SessionExpired as exc:
        print("✗ 本地有登录态，但服务端已不认（需要重新登录）")
        print(f"  服务端返回：{exc}")
        print(f"  登录态文件：{path}")
        return EXIT_ERROR

    print("✓ 当前登录用户")
    print(f"  用户：{user.name or '未知'}（ID: {user.user_id or '未知'}）")
    if stored.phone_masked:
        print(f"  手机：{stored.phone_masked}")
    if stored.age_seconds:
        print(f"  登录态保存于 {stored.age_seconds / 60:.1f} 分钟前")
    return EXIT_OK


def _cmd_logout(args: argparse.Namespace, client: Any) -> int:
    if args.local_only:
        client.set_auth_token("")
        cookies = getattr(client.http, "cookies", None)
        if cookies is not None and hasattr(cookies, "clear"):
            cookies.clear()
    else:
        client.logout()
    removed = clear_session(args.session_file)
    print("✓ 已退出登录" + ("，本地登录态已清除" if removed else "（本地本就没有登录态）"))
    return EXIT_OK


def _cmd_probe(args: argparse.Namespace, client: Any) -> int:
    # 进度信息走 stderr：--json 时 stdout 必须是干净的 JSON，否则没法直接管道给 jq
    print("正在探测候选接口（每个路径发一次空表单 POST，仅用于确认路由是否存在）…", file=sys.stderr)
    report = client.probe_endpoints()

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return EXIT_OK

    for item in report:
        mark = "✓" if item.get("alive") else "✗"
        status = item.get("status") if item.get("status") is not None else "ERR"
        print(f"  {mark} [{status}] {item['path']}")
        if item.get("alive") and item.get("snippet"):
            print(f"       └ {item['snippet'][:100]}")
        elif item.get("error"):
            print(f"       └ {item['error'][:100]}")

    print()
    print("把可用的路径写回 boss_login/config.py 的 ENDPOINTS，或用参数临时覆盖：")
    print("  python -m boss_login login --phone <手机号> --endpoint-send <路径>")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


def _prompt_code() -> str | None:
    """读取验证码，输入可见（验证码是短时效凭证，且便于核对），空输入表示放弃。"""
    try:
        value = input("  请输入收到的 6 位验证码（直接回车放弃）：").strip()
    except EOFError:
        return None
    return value or None


def _make_solver(args: argparse.Namespace) -> SliderSolver | None:
    """按命令行参数造滑块解题回调。

    默认拉起本地帮助页，由你在浏览器里拖官方极验组件；``--no-helper`` 则不解题，
    命中滑块时直接抛 :class:`RiskControlRequired` 走原有提示。
    """
    if getattr(args, "no_helper", False):
        return None

    # None 才走默认值：--helper-port 0 是「交给系统分配」，不是「没传」
    port = SLIDER_HELPER_PORT if args.helper_port is None else args.helper_port
    timeout = SLIDER_HELPER_TIMEOUT if args.helper_timeout is None else args.helper_timeout

    def solve(challenge: SliderChallenge) -> SliderSolution:
        return solve_via_helper(
            challenge,
            port=port,
            timeout=timeout,
            open_browser=not getattr(args, "no_open_browser", False),
        )

    return solve


def _print_risk_control(exc: RiskControlRequired) -> None:
    print("✗ 触发了风控，需要人工完成安全验证后才能继续。", file=sys.stderr)
    print(f"  原因：{exc.message}（code={exc.code}）", file=sys.stderr)
    if exc.verify_page:
        print(f"  验证页：{exc.verify_page}", file=sys.stderr)
    if exc.seed or exc.ts or exc.name:
        print(f"  滑块参数：seed={exc.seed} ts={exc.ts} name={exc.name}", file=sys.stderr)
    elif exc.raw:
        # 没解析出滑块参数时，把原始 zpData 打出来——真实站点可能换了字段名，
        # 看不到它就无从适配。
        print(f"  原始返回：{json.dumps(exc.raw, ensure_ascii=False)}", file=sys.stderr)

    # 两条路的处理方式完全不同，不能混着说。--verify-token 是 SECURITY_CHECK(37)
    # 那条的；滑块（400061）的票据是 challenge/validate/seccode 三件套，由帮助页
    # 自动回传，压根不走 --verify-token。
    if exc.code in RISK_CODE_SLIDER:
        print(
            "  处理方式：滑块由你本人拖，客户端会自动衔接。\n"
            "  重跑一次即可——命中滑块会重新拉起帮助页（http://127.0.0.1:8766/），\n"
            "  拖完自动重试。加 --no-helper 则不解题，只把 gt/challenge 打给你。",
            file=sys.stderr,
        )
    else:
        print(
            "  处理方式：在浏览器中打开站点并用同一手机号登录，完成安全验证后，\n"
            "  把请求里的验证票据通过 --verify-token 传回来。",
            file=sys.stderr,
        )


def _short(value: str, keep: int = 8) -> str:
    return value if len(value) <= keep * 2 else f"{value[:keep]}…{value[-keep:]}"


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
