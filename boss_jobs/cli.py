"""职位获取命令行入口。

    python -m boss_jobs fetch                      # 分页抓取，每页清洗入库，页间睡 1s
    python -m boss_jobs fetch --max-pages 3        # 只翻 3 页
    python -m boss_jobs fetch --interval 2.0       # 页间睡 2 秒（更保守）
    python -m boss_jobs fetch --db my.db           # 指定库路径
    python -m boss_jobs stats                      # 看库里的汇总
    python -m boss_jobs list --city 广州 --limit 20
    python -m boss_jobs list --json                # 职位按 JSON 输出

登录态默认读项目根目录的 ``session.json``（``boss_login`` 落的那份）。

搜索流还会**自动**补 ``__zp_stoken__``：每次抓搜索页前判一次过期，过期了就
拉起一台 Chrome（CDP）让站点自己算一枚、落盘到 ``stoken.json``、再镜像进
``session.json``，下次直接用（令牌约 64 小时）。没有单独的「取令牌」命令。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import __version__
from .cdp_stoken import CHROME_BIN_ENV
from .client import JobClient, create_client
from .config import (
    BASE_URL,
    DEFAULT_DB_PATH,
    DEFAULT_MAX_PAGES,
    DEFAULT_PAGE_INTERVAL,
    DEFAULT_SESSION_PATH,
    STOKEN_COOKIE,
    STOKEN_ENV,
)
from .errors import JobApiError, JobDataError, JobError, JobTransportError
from .stoken import StokenError
from .store import open_store

# 筛选条件的配置文件（没有就留空 = 全部「不限」）
from boss_filter import DEFAULT_FILTER_PATH, JobSearchFilter, load_search_filter

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="boss_jobs",
        description="BOSS直聘职位分页获取（即时清洗 + 入库 + 翻页节流）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--version", action="version", version=f"boss_jobs {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    parser.add_argument("--base-url", default=BASE_URL, help=f"站点地址（默认 {BASE_URL}）")
    parser.add_argument("--timeout", type=float, default=None, help="请求超时秒数")
    parser.add_argument("--retries", type=int, default=None, help="网络层重试次数")
    parser.add_argument(
        "--session",
        default=str(DEFAULT_SESSION_PATH),
        help=f"登录态文件（默认 {DEFAULT_SESSION_PATH}）",
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help=f"SQLite 路径（默认 {DEFAULT_DB_PATH}）",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser("fetch", help="分页抓取职位：抓一页→清洗→入库→睡一会→下一页")
    fetch.add_argument(
        "--max-pages",
        type=int,
        default=DEFAULT_MAX_PAGES,
        help=f"最多翻几页；0=翻到空页（默认 {DEFAULT_MAX_PAGES}）",
    )
    fetch.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_PAGE_INTERVAL,
        help=f"翻页间隔秒数，防风控（默认 {DEFAULT_PAGE_INTERVAL}）",
    )
    fetch.add_argument("--start-page", type=int, default=1, help="起始页码（默认 1）")
    fetch.add_argument(
        "--filter",
        default=None,
        help=(
            "搜索条件配置文件（JSON）。"
            f"默认 {DEFAULT_FILTER_PATH}；文件不存在 = 条件全空（不限），不报错"
        ),
    )
    fetch.add_argument("--json", action="store_true", help="抓完把统计按 JSON 输出")

    stats = sub.add_parser("stats", help="看库里的汇总（条数、页数、城市分布）")
    stats.add_argument("--json", action="store_true", help="按 JSON 输出")

    lst = sub.add_parser("list", help="列出已入库的职位")
    lst.add_argument("--limit", type=int, default=20, help="最多几条（默认 20）")
    lst.add_argument("--offset", type=int, default=0, help="跳过前几条（默认 0）")
    lst.add_argument("--city", default=None, help="按城市精确过滤，如 广州")
    lst.add_argument("--keyword", default=None, help="岗位名/公司名模糊匹配")
    lst.add_argument("--json", action="store_true", help="按 JSON 输出")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)

    try:
        if args.command == "fetch":
            return _cmd_fetch(args)
        if args.command == "stats":
            return _cmd_stats(args)
        if args.command == "list":
            return _cmd_list(args)
    except JobApiError as exc:
        if exc.is_session_expired:
            print(
                f"✗ 登录态失效：{exc.message}\n"
                f"  会话文件：{args.session}\n"
                f"  重新登录：python -m boss_login login",
                file=sys.stderr,
            )
        elif exc.is_browser_check:
            print(
                f"✗ 撞上安全网关（code {exc.code}）：{exc.message}\n"
                f"  缺的是 {STOKEN_COOKIE} 安全网关令牌。本工具会自动拉起 Chrome（CDP）\n"
                f"  让站点自己算一枚补上并落盘（见 boss_jobs.cdp_stoken），\n"
                f"  仍被拒多半是登录态失效，或那台 Chrome 站点也不认。\n"
                f"  急用的话，也可以从浏览器拷一枚塞进 session.json 的 cookies，"
                f"或设 {STOKEN_ENV} 环境变量。",
                file=sys.stderr,
            )
        elif exc.is_risk_control:
            print(
                f"✗ 账号风控（code {exc.code}）：{exc.message}\n"
                f"  这不是缺令牌，是账号维度的风险状态，要走人机验证（GeeTest）后才恢复。\n"
                f"  客户端不去绕。",
                file=sys.stderr,
            )
        else:
            print(f"✗ 接口返回失败：{exc.message}（code {exc.code}）", file=sys.stderr)
        return EXIT_ERROR
    except StokenError as exc:
        print(
            f"✗ 取 {STOKEN_COOKIE} 失败：{exc}\n"
            f"  这枚令牌要真浏览器才生成得出来（Chrome + CDP，见 boss_jobs.cdp_stoken）。\n"
            f"  装好 Google Chrome，或用 {CHROME_BIN_ENV} 指到它的可执行文件；\n"
            f"  也可以手工从浏览器拷一枚塞进 session.json 的 cookies，或设 {STOKEN_ENV} 环境变量。",
            file=sys.stderr,
        )
        return EXIT_ERROR
    except (JobTransportError, JobDataError) as exc:
        print(f"✗ {exc.message}", file=sys.stderr)
        return EXIT_ERROR
    except JobError as exc:
        print(f"✗ {exc.message}", file=sys.stderr)
        return EXIT_ERROR
    except ValueError as exc:
        print(f"✗ 参数不对：{exc}", file=sys.stderr)
        return EXIT_USAGE

    return EXIT_OK


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #


def _cmd_fetch(args: argparse.Namespace) -> int:
    client = _build_client(args)
    # 筛选条件来自配置文件；没有文件 = 全空 = 不限（走推荐流，不搜关键词）
    search_filter = load_search_filter(args.filter)
    if search_filter.is_blank:
        search_filter = None          # 空条件没有筛选语义，走推荐流
        source_note = "推荐流 special/zone（无筛选条件）"
    else:
        source_note = f"搜索流 search/joblist（条件文件 {args.filter or DEFAULT_FILTER_PATH}）"

    with open_store(args.db) as store:
        report = client.crawl(
            store=store,
            max_pages=args.max_pages,
            page_interval=args.interval,
            start_page=args.start_page,
            search_filter=search_filter,
        )
        stats = report.stats
        summary = {
            "run_id": stats.run_id,
            "source": source_note,
            "pages": stats.pages,
            "raw_count": stats.raw_count,
            "kept_count": stats.kept_count,
            "dropped_count": stats.dropped_count,
            "inserted": stats.inserted,
            "updated": stats.updated,
            "stopped_reason": stats.stopped_reason,
            "db_path": str(Path(args.db)),
            "jobs_in_db": store.count_jobs(),
        }

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"抓取来源  {source_note}")
        for line in stats.summary_lines():
            print(line)
        print(f"库内总数  {summary['jobs_in_db']} → {summary['db_path']}")
    return EXIT_OK


def _cmd_stats(args: argparse.Namespace) -> int:
    with open_store(args.db) as store:
        summary = store.summary()
        pages = store.list_pages(limit=10)

    if args.json:
        print(json.dumps({**summary, "recent_pages": pages}, ensure_ascii=False, indent=2))
        return EXIT_OK

    print(f"库路径    {summary['db_path']}")
    print(f"职位条数  {summary['jobs']}")
    print(
        f"抓取流水  {summary['pages']} 页 / "
        f"接口 {summary['raw_seen']} 条 → 洗后 {summary['kept_seen']} 条（丢 {summary['dropped_seen']}）"
    )
    if summary["top_cities"]:
        print("城市分布  " + "、".join(
            f"{row['city']}×{row['count']}" for row in summary["top_cities"]
        ))
    if pages:
        print("最近几页：")
        for row in pages:
            print(
                f"  第 {row['page']} 页  原始 {row['raw_count']} → "
                f"洗后 {row['kept_count']}（丢 {row['dropped_count']}）  "
                f"新增 {row['inserted_count']} / 更新 {row['updated_count']}  "
                f"hasMore={row['has_more']}  {row['fetched_at']}"
            )
    return EXIT_OK


def _cmd_list(args: argparse.Namespace) -> int:
    with open_store(args.db) as store:
        jobs = store.list_jobs(
            limit=args.limit,
            offset=args.offset,
            city=args.city,
            keyword=args.keyword,
        )

    if args.json:
        print(json.dumps([job.to_dict() for job in jobs], ensure_ascii=False, indent=2))
        return EXIT_OK

    if not jobs:
        print("（库里没有匹配的职位）")
        return EXIT_OK
    for index, job in enumerate(jobs, start=1):
        print(f"{index:3d}. {job.summary}")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #


def _build_client(args: argparse.Namespace) -> JobClient:
    kwargs: dict[str, object] = {}
    if args.timeout is not None:
        kwargs["timeout"] = args.timeout
    if args.retries is not None:
        kwargs["retries"] = args.retries
    return create_client(
        session_path=args.session,
        base_url=args.base_url,
        **kwargs,
    )


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )


def _force_utf8_streams() -> None:
    """Windows 控制台默认 GBK，中文会直接 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
