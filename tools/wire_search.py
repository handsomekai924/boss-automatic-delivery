"""接通三件事的端到端脚本：JobSearchFilter → fetch_search_page → 会话带 __zp_stoken__。

跑一遍真实站点的搜索流水线，并把每一段的耗时打出来：

    1. 装配会话（session.json）
    2. **全自动**获取 ``__zp_stoken__``（拉 Chrome，CDP，让站点自己算一枚并落盘）
    3. 拿筛选条件（boss_filter.get_filter_conditions）
    4. 装配 JobSearchFilter（**配置文件** → 查询串）
    5. fetch_search_page 抓一页搜索结果并清洗

筛选条件来自配置文件 ``search_filter.json``（没有就留空 = 全部「不限」），
命令行参数只做覆盖。``__zp_stoken__`` 的来源见 :mod:`boss_jobs.cdp_stoken`。

用法::

    python tools/wire_search.py                          # 读 search_filter.json
    python tools/wire_search.py --query python --city 广州   # 覆盖配置里的值
    python tools/wire_search.py --skip-stoken             # 只测条件装配
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from boss_filter import (  # noqa: E402
    DEFAULT_FILTER_PATH,
    FILTER_ENV,
    JobSearchFilter,
    get_filter_conditions,
    load_search_filter,
)
from boss_filter.models import FilterConditions  # noqa: E402
from boss_jobs import STOKEN_COOKIE, create_client  # noqa: E402
from boss_jobs.errors import JobApiError, JobError  # noqa: E402
from boss_jobs.stoken import StokenError  # noqa: E402


class Timer:
    """分段计时，最后汇总成表。"""

    def __init__(self) -> None:
        self.spans: list[tuple[str, float]] = []
        self._t0 = time.perf_counter()

    def mark(self, label: str) -> float:
        now = time.perf_counter()
        elapsed = now - self._t0
        self.spans.append((label, elapsed))
        self._t0 = now
        return elapsed

    @property
    def total(self) -> float:
        return sum(e for _, e in self.spans)

    def report(self) -> str:
        lines = ["分段耗时："]
        for label, elapsed in self.spans:
            lines.append(f"  {label:<36} {elapsed * 1000:8.1f} ms")
        lines.append(f"  {'合计':<36} {self.total * 1000:8.1f} ms")
        return "\n".join(lines)


def _split_codes(value: str | None) -> tuple[str, ...] | None:
    """"104,105" → ("104","105")；没传返回 None（表示不覆盖）。"""
    if value is None:
        return None
    return tuple(x for x in value.split(",") if x)


def build_filter(
    base: JobSearchFilter,
    conditions: FilterConditions,
    *,
    query: str | None,
    city: str | None,
    salary: str | None,
    experience: str | None,
    degree: str | None,
) -> tuple[JobSearchFilter, str]:
    """配置文件打底，命令行覆盖；城市中文名就地换成 code。

    返回 ``(filter, 来源说明)``。
    """
    overrides: dict[str, object] = {}
    notes: list[str] = []
    if query is not None:
        overrides["query"] = query
        notes.append("query")
    if city is not None:
        city_code = city
        if city and not city.isdigit():
            node = conditions.find_city_by_name(city)
            if node is None:
                raise SystemExit(f"筛选表里找不到城市 {city!r}，请改用 code（广州=101280100）")
            city_code = node.code
        overrides["city"] = city_code
        notes.append("city")
    if salary is not None:
        overrides["salary"] = salary
        notes.append("salary")
    for key, raw in (("experience", experience), ("degree", degree)):
        codes = _split_codes(raw)
        if codes is not None:
            overrides[key] = codes
            notes.append(key)

    merged = replace(base, **overrides) if overrides else base
    # 页码永远从 1 起步（配置文件不存页码）
    merged = replace(merged, page=1)
    source = "命令行覆盖 " + ",".join(notes) if notes else "配置文件"
    return merged, source


def _force_utf8_streams() -> None:
    """Windows 控制台默认 GBK，中文会直接 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    _force_utf8_streams()
    parser = argparse.ArgumentParser(description="接通 JobSearchFilter + fetch_search_page + __zp_stoken__")
    parser.add_argument(
        "--filter",
        default=None,
        help=f"搜索条件配置文件（默认 {DEFAULT_FILTER_PATH}；没有就留空 = 不限）",
    )
    parser.add_argument("--query", default=None, help="搜索关键词（覆盖配置）")
    parser.add_argument("--city", default=None, help="城市 code 或中文名（覆盖配置）")
    parser.add_argument("--salary", default=None, help="薪资档 code，如 405=10-20K（覆盖配置）")
    parser.add_argument("--experience", default=None, help="经验 code，逗号分隔（覆盖配置）")
    parser.add_argument("--degree", default=None, help="学历 code，逗号分隔（覆盖配置）")
    parser.add_argument("--max-pages", type=int, default=1, help="抓几页（默认 1）")
    parser.add_argument("--interval", type=float, default=1.0, help="页间间隔秒")
    parser.add_argument("--stoken", default="", help="显式传 __zp_stoken__（跳过自动计算）")
    parser.add_argument("--skip-stoken", action="store_true", help="不碰安全网关，只测条件装配")
    parser.add_argument("--json", action="store_true", help="把结果按 JSON 输出")
    args = parser.parse_args(argv)

    timer = Timer()
    summary: dict[str, object] = {}

    # 1. 会话装配
    client = create_client(
        page_interval=args.interval,
        stoken=args.stoken or None,
        auto_stoken=not args.skip_stoken and not args.stoken,
    )
    session_span = timer.mark("会话装配 session.json")

    # 2. __zp_stoken__ 全自动获取
    stoken_len = 0
    stoken_note = "跳过"
    if args.skip_stoken:
        timer.mark("__zp_stoken__（跳过）")
    elif args.stoken:
        stoken_len = len(client._http.cookies.get(STOKEN_COOKIE) or "")
        stoken_note = "命令行显式传入"
        timer.mark("__zp_stoken__（显式传入）")
    else:
        provider = client.stoken_provider
        if provider is None:
            timer.mark("__zp_stoken__（未挂 Provider）")
        else:
            try:
                # 先判过期再决定要不要拉 Chrome：新鲜就直接复用
                record = provider.store.load() if getattr(provider, "store", None) else None
                was_fresh = bool(record and record.is_fresh())
                token = provider.ensure()
                stoken_len = len(token)
                stoken_note = (
                    f"复用本地账本（剩 {record.ttl_left / 60:.0f} 分钟）"
                    if was_fresh
                    else "拉 Chrome（CDP）让站点自算并落盘"
                )
            except StokenError as exc:
                stoken_note = f"获取失败：{exc}"
            timer.mark("__zp_stoken__ 全自动获取")
        if stoken_len:
            client._set_cookie(STOKEN_COOKIE, client._http.cookies.get(STOKEN_COOKIE) or "")

    stoken_value = client._http.cookies.get(STOKEN_COOKIE) or ""
    stoken_len = stoken_len or len(stoken_value)

    # 3. 筛选条件（代码里的 7 类可选值表，跟用户配置无关）
    try:
        conditions = get_filter_conditions()
    except Exception as exc:  # noqa: BLE001 - 兜底表救回来
        print(f"拿筛选条件失败：{exc}，退回内置兜底表", file=sys.stderr)
        conditions = get_filter_conditions(html_path=str(ROOT / ".saved_web" / "求职_找工作_招聘信息-BOSS直聘.html"))
    filter_span = timer.mark("拿筛选条件 get_filter_conditions")

    # 4. JobSearchFilter 装配：配置文件打底 + 命令行覆盖
    base = load_search_filter(args.filter)
    filter_file = args.filter or os.environ.get(FILTER_ENV) or str(DEFAULT_FILTER_PATH)
    search_filter, build_source = build_filter(
        base,
        conditions,
        query=args.query,
        city=args.city,
        salary=args.salary,
        experience=args.experience,
        degree=args.degree,
    )
    params = search_filter.to_params()
    build_span = timer.mark("装配 JobSearchFilter")

    resolved = search_filter.resolve(conditions)
    print(f"条件来源  {build_source}（{filter_file}"
          f"{'，文件不存在=全空' if not Path(filter_file).exists() else ''}）")
    print("搜索条件：")
    for line in resolved.summary_lines():
        print("  " + line)
    print(f"查询串：{params}")
    print(f"{STOKEN_COOKIE}：{stoken_note}（长度 {stoken_len or '（缺）'}）")
    print()

    # 5. fetch_search_page
    jobs_total = 0
    pages_done = 0
    stopped = ""
    search_ms = 0.0
    if args.skip_stoken:
        stopped = "跳过搜索（--skip-stoken）"
        timer.mark("fetch_search_page（跳过）")
    else:
        t_search = time.perf_counter()
        try:
            for page in range(1, args.max_pages + 1):
                result = client.fetch_search_page(search_filter, page=page)
                pages_done += 1
                jobs_total += len(result.jobs)
                timer.mark(f"fetch_search_page 第 {page} 页（{len(result.jobs)} 条）")
                for job in result.jobs[:3]:
                    print(f"  · {job.summary}")
                if result.is_empty or (not result.has_more and result.raw_count < 15):
                    stopped = f"第 {page} 页收手（empty={result.is_empty}, hasMore={result.has_more}）"
                    break
        except JobApiError as exc:
            if exc.is_risk_control:
                stopped = (
                    "code 36 账号风控「您的账户存在异常行为」："
                    "要走人机验证（GeeTest）后才能继续，客户端不去绕。"
                )
            elif exc.is_browser_check:
                stopped = (
                    f"code 37 安全网关：自动补 {STOKEN_COOKIE} 后仍被拒。"
                    f"多半是登录态失效，或那台 Chrome 站点也不认。"
                )
            else:
                stopped = f"接口报错 code={exc.code}：{exc.message}"
            timer.mark(f"fetch_search_page 失败（{exc.code}）")
        except JobError as exc:
            stopped = f"抓取失败：{exc}"
            timer.mark("fetch_search_page 失败")
        search_ms = (time.perf_counter() - t_search) * 1000

    print()
    print(timer.report())
    print()

    summary = {
        "session_ms": round(session_span * 1000, 1),
        "stoken_ms": round(next((e for lbl, e in timer.spans if "__zp_stoken__" in lbl), 0.0) * 1000, 1),
        "conditions_ms": round(filter_span * 1000, 1),
        "build_filter_ms": round(build_span * 1000, 1),
        "search_ms": round(search_ms, 1),
        "total_ms": round(timer.total * 1000, 1),
        "pages": pages_done,
        "jobs": jobs_total,
        "stoken_present": stoken_len > 0,
        "stoken_len": stoken_len,
        "stoken_note": stoken_note,
        "params": params,
        "conditions_source": conditions.source,
        "stopped": stopped,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if (jobs_total or args.skip_stoken) else 1


if __name__ == "__main__":
    raise SystemExit(main())
