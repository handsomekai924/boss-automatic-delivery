"""筛选条件命令行入口。

    python -m boss_filter show                      # 打接口拿筛选项，打摘要
    python -m boss_filter show --json               # 摘要按 JSON 输出
    python -m boss_filter show --fallback           # 不打接口，只用写死表
    python -m boss_filter export --out filters.json # 导出完整选项表

接口路径未核实或线上已变更时，用 ``--base-url`` / ``--endpoint-conditions``
临时覆盖。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import __version__
from .client import FilterClient, get_filter_conditions
from .config import BASE_URL
from .errors import FilterApiError, FilterDataError, FilterError, FilterTransportError
from .models import FilterConditions

#: 退出码，便于脚本判断失败原因
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="boss_filter",
        description="BOSS直聘职位筛选条件获取（城市/求职类型/薪资/经验/学历/行业/规模）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--version", action="version", version=f"boss_filter {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    parser.add_argument("--base-url", default=BASE_URL, help=f"站点地址（默认 {BASE_URL}）")
    parser.add_argument("--timeout", type=float, default=None, help="请求超时秒数")
    parser.add_argument("--retries", type=int, default=None, help="网络层重试次数")
    parser.add_argument(
        "--endpoint-conditions",
        default=None,
        help="覆盖条件接口路径（默认 /wapi/zpgeek/pc/all/filter/conditions.json）",
    )
    parser.add_argument(
        "--endpoint-city",
        default=None,
        help="覆盖城市接口路径（默认 /wapi/zpCommon/data/city.json）",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    show = sub.add_parser("show", help="获取并展示筛选条件摘要")
    show.add_argument("--json", action="store_true", help="按 JSON 输出（完整选项表）")
    show.add_argument("--fallback", action="store_true", help="不打接口，只用项目内写死表")
    show.add_argument(
        "--detail",
        action="store_true",
        help="逐项列出可选值，而不是只给条数",
    )

    export = sub.add_parser("export", help="导出完整筛选选项表")
    export.add_argument("--out", default="-", help="输出文件路径，`-` 表示标准输出")
    export.add_argument("--fallback", action="store_true", help="不打接口，只用项目内写死表")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)

    try:
        conditions = _load_conditions(args)
    except FilterDataError as exc:
        print(f"✗ 数据解析失败：{exc.message}", file=sys.stderr)
        return EXIT_USAGE
    except FilterApiError as exc:
        hint = "（登录态可能已失效，先 `python -m boss_login login`）" if exc.is_session_expired else ""
        print(f"✗ 接口返回失败{hint}：{exc.message}", file=sys.stderr)
        return EXIT_ERROR
    except FilterTransportError as exc:
        print(f"✗ 网络异常：{exc.message}", file=sys.stderr)
        print("  可加 --fallback 只用写死表。", file=sys.stderr)
        return EXIT_ERROR
    except FilterError as exc:
        print(f"✗ 获取筛选条件失败：{exc.message}", file=sys.stderr)
        return EXIT_ERROR

    handlers = {"show": _cmd_show, "export": _cmd_export}
    return handlers[args.command](args, conditions)


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #


def _cmd_show(args: argparse.Namespace, conditions: FilterConditions) -> int:
    if args.json:
        json.dump(conditions.to_dict(), sys.stdout, ensure_ascii=False, indent=2)
        print()
        return EXIT_OK

    for line in conditions.summary_lines():
        print(line)
    if args.detail:
        print()
        _print_detail(conditions)
    return EXIT_OK


def _cmd_export(args: argparse.Namespace, conditions: FilterConditions) -> int:
    text = json.dumps(conditions.to_dict(), ensure_ascii=False, indent=2)
    if args.out == "-":
        print(text)
        return EXIT_OK
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text + "\n", encoding="utf-8")
    print(f"✓ 已写入 {target}")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# 装配
# --------------------------------------------------------------------------- #


def _load_conditions(args: argparse.Namespace) -> FilterConditions:
    if getattr(args, "fallback", False):
        from .fallback import build_fallback_conditions

        return build_fallback_conditions()

    endpoints = {}
    if args.endpoint_conditions:
        endpoints["conditions"] = args.endpoint_conditions
    if args.endpoint_city:
        endpoints["city"] = args.endpoint_city

    client_kwargs: dict = {"base_url": args.base_url}
    if endpoints:
        client_kwargs["endpoints"] = endpoints
    if args.timeout is not None:
        client_kwargs["timeout"] = args.timeout
    if args.retries is not None:
        client_kwargs["retries"] = args.retries

    client = FilterClient(**client_kwargs)
    return get_filter_conditions(client=client, use_fallback=True)


# --------------------------------------------------------------------------- #
# 展示辅助
# --------------------------------------------------------------------------- #


def _print_detail(conditions: FilterConditions) -> None:
    def dump(title: str, options) -> None:
        print(f"[{title}]")
        for opt in options:
            extra = ""
            if opt.low_salary is not None:
                extra = f"  ({opt.low_salary}-{opt.high_salary}K)"
            print(f"  {opt.code:>10}  {opt.name}{extra}")

    dump("求职类型", conditions.job_types)
    dump("薪资待遇", conditions.salaries)
    dump("工作经验", conditions.experiences)
    dump("学历要求", conditions.degrees)
    dump("公司规模", conditions.scales)
    for group in conditions.industries:
        print(f"[公司行业 / {group.name}]")
        for opt in group.options:
            print(f"  {opt.code:>10}  {opt.name}")
    if conditions.hot_cities:
        dump("热点城市", conditions.hot_cities)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
