"""职位获取命令行入口。

    python -m boss_jobs fetch                      # 分页抓取，每页清洗入库，页间睡 1s
    python -m boss_jobs fetch --max-pages 3        # 只翻 3 页
    python -m boss_jobs fetch --interval 2.0       # 页间睡 2 秒（更保守）
    python -m boss_jobs fetch --db my.db           # 指定状态库路径
    python -m boss_jobs fetch --filter my.json     # 搜索流：用这份 JSON 条件
    python -m boss_jobs fetch --expect part_time   # 期望流：兼职期望
    python -m boss_jobs fetch --expect recommend   # 期望流：推荐（混合）期望
    python -m boss_jobs fetch --expect expect:<id> # 期望流：指定期望
    python -m boss_jobs fetch --expect temp:<file> # 期望流：临时期望（expectInfo JSON）
    python -m boss_jobs stats                      # 看库里的汇总
    python -m boss_jobs list --city 广州 --limit 20
    python -m boss_jobs list --json                # 职位按 JSON 输出

登录态默认读状态库 ``data/boss.db`` 的 ``doc('session')``（``boss_login`` 落的那份）。

搜索流 / 期望流还会**自动**补 ``__zp_stoken__``：每次抓页前判一次过期，过期了就
拉起一台 Chrome（CDP）让站点自己算一枚、落到 ``doc('stoken')``、再镜像进
``doc('session')``，下次直接用（令牌约 64 小时）。没有单独的「取令牌」命令。

``--expect``（期望流）与 ``--filter``（搜索流）**互斥**——两者都在选「这次走哪条
检索流」。期望流可以用库里的共享筛选维度（城市/薪资/经验/…，经
``JobExpectFilter.from_search`` 搬过去），但**永远不带关键词**（站点点期望 tab 时
就把 ``query`` 删了）。要「期望 + 关键词」就走搜索流（库里配好 ``encryptExpectId``）。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import replace
from pathlib import Path

from . import __version__
from .cdp_stoken import CHROME_BIN_ENV
from .client import JobClient, create_client
from .config import (
    BASE_URL,
    DEFAULT_DB_PATH,
    DEFAULT_MAX_PAGES,
    DEFAULT_PAGE_INTERVAL,
    STOKEN_COOKIE,
    STOKEN_ENV,
)
from .errors import JobApiError, JobDataError, JobError, JobTransportError
from .stoken import StokenError
from .store import open_store

# 筛选条件：不传 --filter 就用状态库里那份；传了就只读那份 JSON（不写库）
from boss_filter import JobSearchFilter, load_search_filter, search_filter_from_file
from boss_filter.expect import JobExpectFilter

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

#: ``--expect`` 认的取值（写进 help，跟解析规则同一处维护）。
EXPECT_SPECS: tuple[str, ...] = (
    "recommend",
    "part_time",
    "expect:<encryptExpectId>",
    "temp:<expectInfo.json>",
)


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
        "--db",
        default=None,
        help=f"状态库路径（默认 {DEFAULT_DB_PATH}，可用环境变量 BOSS_DB 覆盖）",
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
            "本次抓取用这份 JSON 条件（只读，不写库），走搜索流 search/joblist。"
            "不传则用状态库里存的条件。条件至少要有一个维度——旧「推荐页」"
            "（全部不限，special/zone）已下线，全空会报错。"
            "与 --expect 互斥"
        ),
    )
    fetch.add_argument(
        "--expect",
        default=None,
        metavar="SPEC",
        help=(
            "走求职期望流 recommend/job/list.json。取值："
            + " / ".join(EXPECT_SPECS)
            + "。recommend/part_time 不带 id 时在线拉期望列表取默认条目；"
            "筛选维度沿用库里的搜索条件（不含关键词）。与 --filter 互斥"
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
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    if getattr(args, "expect", None) and getattr(args, "filter", None):
        parser.error(
            "--expect（期望流 recommend/job/list）与 --filter（搜索流 search/joblist）互斥："
            "一次抓取只能走一条检索流。要「期望 + 关键词」就只用 --filter"
            "（库里配好 encryptExpectId），要「期望 + 筛选」就只用 --expect"
        )

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
                f"  状态库：{args.db or DEFAULT_DB_PATH}\n"
                f"  重新登录：python -m boss_login login",
                file=sys.stderr,
            )
        elif exc.is_browser_check:
            print(
                f"✗ 撞上安全网关（code {exc.code}）：{exc.message}\n"
                f"  缺的是 {STOKEN_COOKIE} 安全网关令牌。本工具会自动拉起 Chrome（CDP）\n"
                f"  让站点自己算一枚补上并落到状态库（见 boss_jobs.cdp_stoken），\n"
                f"  仍被拒多半是登录态失效，或那台 Chrome 站点也不认。\n"
                f"  急用的话，也可以从浏览器拷一枚塞进状态库 doc('session') 的 cookies，"
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
            f"  也可以手工从浏览器拷一枚塞进状态库 doc('session') 的 cookies，或设 {STOKEN_ENV} 环境变量。",
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
    expect_filter: JobExpectFilter | None = None
    search_filter: JobSearchFilter | None = None

    if args.expect:
        # 期望流：主参数按 --expect 装配，共享筛选维度从库里的搜索条件搬
        # （经 JobExpectFilter.from_search），但**不搬 query**——期望流永远不带关键词。
        expect_filter = _assemble_expect_filter(args.expect, client=client, db=args.db)
        source_note = f"期望流 recommend/job/list（{args.expect}）"
    elif args.filter:
        search_filter = search_filter_from_file(args.filter)
        source_note = f"搜索流 search/joblist（条件文件 {args.filter}）"
    else:
        # --filter 是「这次就用这份」的只读入参；不传则用库里的条件。
        search_filter = load_search_filter(args.db)
        source_note = "搜索流 search/joblist（库里的搜索条件）"

    # 推荐页 special/zone 已下线（2026-10-13），「全部不限」没有检索语义了
    if expect_filter is None and search_filter is not None and search_filter.is_blank:
        raise ValueError(
            "搜索条件全空（关键词和筛选维度都没选）。"
            "旧「推荐页」special/zone 检索流已下线，不再支持「全部不限」抓取："
            "请在 --filter 条件文件或采集配置里至少填一个维度（关键词 / 城市 / 薪资…），"
            "或用 --expect 走求职期望流"
        )

    with open_store(args.db) as store:
        report = client.crawl(
            store=store,
            max_pages=args.max_pages,
            page_interval=args.interval,
            start_page=args.start_page,
            search_filter=search_filter,
            expect_filter=expect_filter,
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
            "db_path": str(store.path),
            "jobs_in_db": store.count_jobs(),
        }

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"抓取来源  {source_note}")
        if expect_filter is not None:
            for line in expect_filter.summary_lines():
                print(line)
        for line in stats.summary_lines():
            print(line)
        print(f"库内总数  {summary['jobs_in_db']} → {summary['db_path']}")
    return EXIT_OK


def _parse_expect_spec(spec: str) -> tuple[str, str, str]:
    """把 ``--expect`` 的取值拆成 ``(kind, encrypt_id, file_path)``。

    认的取值见 :data:`EXPECT_SPECS`。``recommend`` / ``part_time`` 不带 id 时
    留空、由 :meth:`JobExpectFilter.from_expects` 取列表默认条目；
    ``expect:<id>`` 显式点名；``temp:<file>`` 读临时期望的 expectInfo JSON。
    """
    text = (spec or "").strip()
    if text in ("recommend", "part_time"):
        return text, "", ""
    if text == "expect":
        return "expect", "", ""
    if text.startswith("expect:"):
        return "expect", text.split(":", 1)[1].strip(), ""
    if text == "temp" or text.startswith("temp:"):
        path = text.split(":", 1)[1].strip() if ":" in text else ""
        return "temp_expect", "", path
    raise ValueError(
        f"--expect 取值不认识：{spec!r}（可选：{' / '.join(EXPECT_SPECS)}）"
    )


def _assemble_expect_filter(spec: str, *, client: JobClient, db: str | None) -> JobExpectFilter:
    """按 ``--expect`` 装配一次期望流检索。

    共享筛选维度（城市/薪资/经验/学历/行业/规模，兼职态 payType/partTime）从
    库里的搜索条件搬过来——**一份筛选条件两用**，只是期望流换上 ``encryptExpectId``。
    ``query`` 不搬（见模块头）。
    """
    kind, explicit_id, temp_file = _parse_expect_spec(spec)
    base = load_search_filter(db)

    if kind == "temp_expect":
        if not temp_file:
            raise ValueError("临时期望要给文件：--expect temp:<expectInfo.json>")
        path = Path(temp_file)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise ValueError(f"临时期望文件 {path} 不存在") from None
        except OSError as exc:
            raise ValueError(f"读不了临时期望文件 {path}：{exc}") from exc
        try:
            data = json.loads(text) if text.strip() else {}
        except ValueError as exc:
            raise ValueError(f"临时期望文件 {path} 不是合法 JSON：{exc}") from exc
        # 已经是序列化好的 expectInfo 串就原样透传；是对象则按前端形状序列化
        expect_info = data if isinstance(data, str) else json.dumps(
            {
                "expectType": (data or {}).get("expectType", ""),
                "position": (data or {}).get("position", ""),
                "location": (data or {}).get("location", ""),
                "lowSalary": (data or {}).get("lowSalary", ""),
                "highSalary": (data or {}).get("highSalary", ""),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return JobExpectFilter.from_search(
            base, kind="temp_expect", expect_info=expect_info
        )

    mix_expect_type = ""
    if explicit_id:
        encrypt_expect_id = explicit_id
        # 推荐 tab 还要 mixExpectType；id 对得上列表里的混合期望才带得出来
        if kind == "recommend":
            mix = client.fetch_expect_list().mix
            if mix is not None and mix.encrypt_expect_id == explicit_id:
                mix_expect_type = mix.mix_expect_type
    else:
        # 默认取哪个的规则在 from_expects：partTimeExpectList[0] / expectList[0] / mix
        picked = JobExpectFilter.from_expects(client.fetch_expect_list(), kind)
        encrypt_expect_id = picked.encrypt_expect_id
        mix_expect_type = picked.mix_expect_type
        if kind == "recommend" and picked.city:
            # 前端 getFormData().city = mixLocation || defaultCity：混合期望优先
            base = replace(base, city=picked.city)

    return JobExpectFilter.from_search(
        base,
        kind=kind,
        encrypt_expect_id=encrypt_expect_id,
        mix_expect_type=mix_expect_type,
    )


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
        session_path=args.db,
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
