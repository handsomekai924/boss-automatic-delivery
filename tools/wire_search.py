"""接通三件事的端到端脚本：JobSearchFilter → fetch_search_page → 会话带 __zp_stoken__。

跑一遍真实站点的搜索流水线，并把每一段的耗时打出来：

    1. 装配会话（session.json）
    2. **全自动**获取 ``__zp_stoken__``（拿挑战 → 下 security-js → ABC.z 算令牌）
    3. 拿筛选条件（boss_filter.get_filter_conditions）
    4. 装配 JobSearchFilter（选中值 → 查询串）
    5. fetch_search_page 抓一页搜索结果并清洗

``__zp_stoken__`` 的来源与算法见 :mod:`boss_jobs.stoken`，本脚本只是把它
接进流水线并计时——**不需要**人工回浏览器拷令牌。

用法::

    python tools/wire_search.py
    python tools/wire_search.py --query python --city 101280100 --max-pages 2
    python tools/wire_search.py --skip-stoken   # 只测条件装配，不碰安全网关
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from boss_filter import JobSearchFilter, get_filter_conditions  # noqa: E402
from boss_filter.models import FilterConditions  # noqa: E402
from boss_jobs import STOKEN_COOKIE, create_client  # noqa: E402
from boss_jobs.errors import JobApiError, JobError  # noqa: E402
from boss_jobs.stoken import StokenError, StokenProvider, mint_offline  # noqa: E402


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


def build_filter(
    conditions: FilterConditions,
    *,
    query: str,
    city: str,
    salary: str,
    experience: tuple[str, ...],
    degree: tuple[str, ...],
) -> JobSearchFilter:
    """从命令行选项 + 筛选表code装配一次搜索条件。

    城市名容错：传了中文名（如「广州」）就地换成 code，传 code 原样用。
    """
    city_code = city
    if city and not city.isdigit():
        node = conditions.find_city_by_name(city)
        if node is None:
            raise SystemExit(f"筛选表里找不到城市 {city!r}，请改用 code（广州=101280100）")
        city_code = node.code

    return JobSearchFilter.from_codes(
        query=query,
        city=city_code,
        salary=salary,
        experience=experience,
        degree=degree,
        page=1,
    )


def _force_utf8_streams() -> None:
    """Windows 控制台默认 GBK，中文会直接 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    _force_utf8_streams()
    parser = argparse.ArgumentParser(description="接通 JobSearchFilter + fetch_search_page + __zp_stoken__")
    parser.add_argument("--query", default="python", help="搜索关键词")
    parser.add_argument("--city", default="101280100", help="城市 code 或中文名（默认广州）")
    parser.add_argument("--salary", default="", help="薪资档 code，如 405=10-20K")
    parser.add_argument("--experience", default="", help="经验 code，逗号分隔")
    parser.add_argument("--degree", default="", help="学历 code，逗号分隔")
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
        provider: StokenProvider | None = client.stoken_provider
        if provider is None:
            timer.mark("__zp_stoken__（未挂 Provider）")
        else:
            try:
                # 先算后用：把「拿挑战 + 下脚本 + 算令牌」单独计时
                challenge = provider.fetch_challenge()
                token = provider.mint(challenge)
                provider.apply(token)
                stoken_len = len(token)
                stoken_note = f"在线铸币（name={challenge.name}，脚本 {challenge.name}.js）"
            except StokenError as exc:
                # 拿不到真挑战（多半 code 36 风控）时，退到本地缓存脚本铸一枚，
                # 把「算法这条路通了」和「纯铸币耗时」量出来。样例令牌不会被服务端认。
                try:
                    token = mint_offline(cache_dir=provider.cache_dir, node_bin=provider.node_bin)
                    stoken_len = len(token)
                    stoken_note = (
                        f"离线铸币（真挑战拿不到：{exc}）——样例挑战，"
                        f"仅验证算法与耗时，服务端不会认"
                    )
                except StokenError as exc2:
                    stoken_note = f"获取失败：{exc}；离线铸币也失败：{exc2}"
            timer.mark("__zp_stoken__ 全自动获取")
        if stoken_len:
            client._set_cookie(STOKEN_COOKIE, client._http.cookies.get(STOKEN_COOKIE) or "")

    stoken_value = client._http.cookies.get(STOKEN_COOKIE) or ""
    stoken_len = stoken_len or len(stoken_value)

    # 3. 筛选条件
    try:
        conditions = get_filter_conditions()
    except Exception as exc:  # noqa: BLE001 - 兜底表救回来
        print(f"拿筛选条件失败：{exc}，退回内置兜底表", file=sys.stderr)
        conditions = get_filter_conditions(html_path=str(ROOT / ".saved_web" / "求职_找工作_招聘信息-BOSS直聘.html"))
    filter_span = timer.mark("拿筛选条件 get_filter_conditions")

    # 4. JobSearchFilter 装配
    search_filter = build_filter(
        conditions,
        query=args.query,
        city=args.city,
        salary=args.salary,
        experience=tuple(x for x in args.experience.split(",") if x),
        degree=tuple(x for x in args.degree.split(",") if x),
    )
    params = search_filter.to_params()
    build_span = timer.mark("装配 JobSearchFilter")

    resolved = search_filter.resolve(conditions)
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
                    f"多半是 security-js 的环境指纹跟请求头对不上。"
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
