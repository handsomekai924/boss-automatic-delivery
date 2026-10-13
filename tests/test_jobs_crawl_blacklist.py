"""采集链路的黑名单集成测试：走完整的 ``JobClient.crawl``。

规矩：假 HTTP 会话，不打真站点；隔离库。验的是**顺序**——
公司规则在详情之前、JD 规则在详情之后、``store_blacklisted=false`` 连入库都不做。
"""

from __future__ import annotations

from typing import Any

from boss_jobs.client import CrawlBlacklistContext, JobClient
from boss_jobs.store import JobStore


# --------------------------------------------------------------------------- #
# 假 HTTP / 页面脚本
# --------------------------------------------------------------------------- #


class FakeResponse:
    def __init__(self, payload=None) -> None:
        self.status_code = 200
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload


class FakeHttp:
    def __init__(self, responses) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.headers: dict[str, str] = {}
        self.cookies = None

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self._responses:
            raise AssertionError(f"没有预置响应可给，却收到第 {len(self.calls)} 次请求：{url}")
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item if isinstance(item, FakeResponse) else FakeResponse(item)


def api_item(encrypt_job_id: str, *, job_name: str = "Python 开发", brand_name: str = "甲公司", **overrides) -> dict:
    item = {
        "encryptJobId": encrypt_job_id,
        "jobName": job_name,
        "brandName": brand_name,
        "securityId": f"sec-{encrypt_job_id}",
        "lid": "L1",
    }
    item.update(overrides)
    return item


def ok_page(items, *, has_more: bool = False) -> dict:
    return {"code": 0, "message": "Success", "zpData": {"jobList": items, "hasMore": has_more}}


def detail_ok(desc: str) -> dict:
    return {"code": 0, "zpData": {"jobInfo": {"postDescription": desc}}}


def hit(rule_id: str, rule_type: str, *, pattern: str, matched_text: str, source: str = "brand_name") -> dict:
    return {
        "rule_id": rule_id,
        "rule_type": rule_type,
        "pattern": pattern,
        "match_mode": "contains" if rule_type == "jd_keyword" else "exact",
        "match_scope": source,
        "matched_text": matched_text,
    }


def company_matcher(pattern: str):
    def match(job) -> list[dict[str, Any]]:
        if pattern and pattern in (job.brand_name or ""):
            return [hit("r-co", "company", pattern=pattern, matched_text=job.brand_name)]
        return []

    return match


def jd_matcher(pattern: str):
    def match(job, desc: str) -> list[dict[str, Any]]:
        hits: list[dict[str, Any]] = []
        if pattern and pattern in (job.job_name or ""):
            hits.append(hit("r-jd", "jd_keyword", pattern=pattern, matched_text=job.job_name, source="job_name"))
        if pattern and pattern in (desc or ""):
            hits.append(hit("r-jd", "jd_keyword", pattern=pattern, matched_text=desc[:40], source="job_desc"))
        return hits

    return match


def empty_matcher(job, desc: str = "") -> list[dict[str, Any]]:
    return []


class RecordingContext:
    """把 ``record_hit`` 的调用记下来，好断言审计动作（stored / skipped）。"""

    def __init__(self, **kwargs) -> None:
        self.hits: list[dict[str, Any]] = []
        kwargs.setdefault("record_hit", self._record)
        self.ctx = CrawlBlacklistContext(**kwargs)

    def _record(self, job, match, *, action: str = "stored", jd_status: str = "") -> None:
        self.hits.append(
            {
                "job_id": job.encrypt_job_id,
                "rule_id": (match or {}).get("rule_id"),
                "action": action,
                "jd_status": jd_status,
            }
        )

    def actions_for(self, job_id: str) -> list[str]:
        return [h["action"] for h in self.hits if h["job_id"] == job_id]


class _StubFilter:
    """最小 search_filter 桩——推荐页 special/zone 已移除，crawl 必须给一条流。"""

    def __init__(self, params=None):
        self._params = dict(params or {"query": "python", "page": "1"})

    def to_params(self):
        return dict(self._params)

    def for_page(self, page):
        return _StubFilter({**self._params, "page": str(page)})


def run_crawl(tmp_path, responses, *, context=None, enrich_details=False, max_pages=1, events=None, expect_filter=None, search_filter=None):
    """跑一轮 crawl，回 ``(store, report, http)``。**不关库**——调用方自己收尾。

    黑名单行为与路由无关；不显式给流就走最小搜索流桩。
    """
    http = FakeHttp(responses)
    client = JobClient(http=http, sleeper=lambda _s: None)
    store = JobStore(tmp_path / "jobs.db")
    report = client.crawl(
        store=store,
        max_pages=max_pages,
        page_interval=0.0,
        detail_interval=0.0,
        enrich_details=enrich_details,
        blacklist_context=context,
        on_progress=(events.append if events is not None else None),
        expect_filter=expect_filter,
        search_filter=search_filter if search_filter is not None else (
            None if expect_filter is not None else _StubFilter()
        ),
    )
    return store, report, http


def detail_urls(http) -> list[str]:
    return [c["url"] for c in http.calls if "detail" in c["url"]]


# --------------------------------------------------------------------------- #
# 公司命中：不入库 vs 入库并标记
# --------------------------------------------------------------------------- #


def test_公司命中_不入库时连详情都不抓(tmp_path):
    """``store_blacklisted=false``：命中那条不打详情、不入库；普通条照常。"""
    rec = RecordingContext(
        store_blacklisted=False,
        company_matches=company_matcher("甲公司"),
    )
    events: list[dict] = []
    store, report, http = run_crawl(
        tmp_path,
        [ok_page([api_item("j1", brand_name="甲公司"), api_item("j2", brand_name="正常公司")]),
         detail_ok("正常公司的 JD")],
        context=rec.ctx,
        enrich_details=True,
        events=events,
    )
    try:
        # 只有 j2 抓了详情；j1 命中公司规则，连详情都不该请求
        detail_calls = detail_urls(http)
        assert len(detail_calls) == 1
        assert store.get_job("j1") is None
        j2 = store.get_job("j2")
        assert j2 is not None
        assert j2.job_desc == "正常公司的 JD"
        assert report.stats.skipped == 1
        assert report.stats.company_hits == 1
        assert report.stats.inserted == 1
        assert rec.actions_for("j1") == ["skipped"]
        kinds = [e.get("event") for e in events]
        assert "company_blacklist_skipped" in kinds
    finally:
        store.close()


def test_公司命中_入库并标记时仍抓JD(tmp_path):
    """``store_blacklisted=true``：照抓详情，行里带上公司命中与阻断标记。"""
    rec = RecordingContext(
        store_blacklisted=True,
        company_matches=company_matcher("甲公司"),
    )
    store, report, http = run_crawl(
        tmp_path,
        [ok_page([api_item("j1", brand_name="甲公司")]), detail_ok("写代码")],
        context=rec.ctx,
        enrich_details=True,
    )
    try:
        assert len(detail_urls(http)) == 1
        job = store.get_job("j1")
        assert job is not None
        assert job.company_blacklisted is True
        assert job.auto_delivery_blocked is True
        assert job.job_desc == "写代码"
        assert job.jd_status == "completed"
        assert rec.actions_for("j1") == ["stored"]
        assert report.stats.company_hits == 1
        assert report.stats.skipped == 0
        assert report.stats.inserted == 1
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# JD 命中：入库并标记 vs 不入库
# --------------------------------------------------------------------------- #


def test_JD命中_入库并标记(tmp_path):
    rec = RecordingContext(
        store_blacklisted=True,
        has_jd_rules=True,
        jd_scopes=("job_desc",),
        company_matches=company_matcher("不命中的公司"),
        jd_matches=jd_matcher("外包"),
    )
    events: list[dict] = []
    store, report, http = run_crawl(
        tmp_path,
        [ok_page([api_item("j1", job_name="Java 工程师")]), detail_ok("岗位外包，常驻现场")],
        context=rec.ctx,
        enrich_details=True,
        events=events,
    )
    try:
        job = store.get_job("j1")
        assert job is not None
        assert job.jd_blacklisted is True
        assert job.auto_delivery_blocked is True
        assert job.jd_rule_checked is True
        assert rec.actions_for("j1") == ["stored"]
        assert report.stats.jd_hits == 1
        kinds = [e.get("event") for e in events]
        assert "jd_blacklist_hit" in kinds
    finally:
        store.close()


def test_JD命中_不入库时整条丢掉(tmp_path):
    """``store_blacklisted=false`` 的 JD 命中：详情抓了，但不落 ``jobs`` 行。"""
    rec = RecordingContext(
        store_blacklisted=False,
        has_jd_rules=True,
        jd_scopes=("job_desc",),
        company_matches=empty_matcher,
        jd_matches=jd_matcher("外包"),
    )
    store, report, http = run_crawl(
        tmp_path,
        [ok_page([api_item("j1", job_name="Java 工程师")]), detail_ok("岗位外包")],
        context=rec.ctx,
        enrich_details=True,
    )
    try:
        assert len(detail_urls(http)) == 1
        assert store.get_job("j1") is None
        assert report.stats.inserted == 0
        assert rec.actions_for("j1") == ["skipped"]
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# 不补详情 / 详情失败 / 详情为空
# --------------------------------------------------------------------------- #


def test_关详情_有JD规则时状态是未检查并阻断(tmp_path):
    rec = RecordingContext(
        has_jd_rules=True,
        jd_scopes=("job_desc",),
        company_matches=empty_matcher,
    )
    store, report, http = run_crawl(
        tmp_path,
        [ok_page([api_item("j1")])],
        context=rec.ctx,
        enrich_details=False,
    )
    try:
        assert detail_urls(http) == []
        job = store.get_job("j1")
        assert job is not None
        assert job.jd_rule_checked is False
        assert job.jd_status == "unchecked"
        assert job.auto_delivery_blocked is True
    finally:
        store.close()


def test_关详情_没有JD规则时视为已检查且不阻断(tmp_path):
    rec = RecordingContext(company_matches=empty_matcher)
    store, report, http = run_crawl(
        tmp_path,
        [ok_page([api_item("j1")])],
        context=rec.ctx,
        enrich_details=False,
    )
    try:
        job = store.get_job("j1")
        assert job is not None
        assert job.jd_rule_checked is True
        assert job.auto_delivery_blocked is False
        assert job.company_blacklisted is False
    finally:
        store.close()


def test_详情失败_标记failed并阻断自动投递(tmp_path):
    from boss_jobs.errors import JobApiError

    rec = RecordingContext(has_jd_rules=True, jd_scopes=("job_desc",), company_matches=empty_matcher)
    store, report, http = run_crawl(
        tmp_path,
        [ok_page([api_item("j1")]), JobApiError(36, "账号异常", raw={})],
        context=rec.ctx,
        enrich_details=True,
    )
    try:
        job = store.get_job("j1")
        assert job is not None
        assert job.jd_status == "failed"
        assert job.jd_rule_checked is False
        assert job.auto_delivery_blocked is True
    finally:
        store.close()


def test_详情为空_有需正文的JD规则时算未检查(tmp_path):
    rec = RecordingContext(has_jd_rules=True, jd_scopes=("job_desc",), company_matches=empty_matcher)
    store, report, http = run_crawl(
        tmp_path,
        [ok_page([api_item("j1")]), detail_ok("")],
        context=rec.ctx,
        enrich_details=True,
    )
    try:
        job = store.get_job("j1")
        assert job is not None
        assert job.jd_status == "empty"
        assert job.jd_rule_checked is False
        assert job.auto_delivery_blocked is True
    finally:
        store.close()


def test_纯名称JD规则_空正文也能查完(tmp_path):
    """scope=job_name 不需要 JD 正文：空详情也算检查完成。"""
    rec = RecordingContext(
        has_jd_rules=True,
        jd_scopes=("job_name",),
        company_matches=empty_matcher,
        jd_matches=jd_matcher("外包"),
    )
    store, report, http = run_crawl(
        tmp_path,
        [ok_page([api_item("j1", job_name="前端 外包")]), detail_ok("")],
        context=rec.ctx,
        enrich_details=True,
    )
    try:
        job = store.get_job("j1")
        assert job is not None
        assert job.jd_rule_checked is True
        assert job.jd_blacklisted is True
        assert job.jd_status == "empty"
        assert job.auto_delivery_blocked is True
        assert report.stats.jd_hits == 1
    finally:
        store.close()


def test_关详情_纯名称JD规则就地查岗位名(tmp_path):
    """关详情时纯名称规则也要跑岗位名；不能只标「已检查」却没查过。"""
    rec = RecordingContext(
        has_jd_rules=True,
        jd_scopes=("job_name",),
        company_matches=empty_matcher,
        jd_matches=jd_matcher("外包"),
    )
    store, report, http = run_crawl(
        tmp_path,
        [ok_page([api_item("j1", job_name="前端 外包"), api_item("j2", job_name="Java 工程师")])],
        context=rec.ctx,
        enrich_details=False,
    )
    try:
        assert detail_urls(http) == []
        hit_job = store.get_job("j1")
        ok_job = store.get_job("j2")
        assert hit_job is not None and hit_job.jd_blacklisted is True
        assert hit_job.jd_rule_checked is True
        assert ok_job is not None and ok_job.jd_blacklisted is False
        assert ok_job.auto_delivery_blocked is False
        assert report.stats.jd_hits == 1
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# 重抓刷新旧状态
# --------------------------------------------------------------------------- #


def test_重抓会刷新旧的安全状态(tmp_path):
    """规则删了以后重抓：旧的 ``jd_keyword`` 快照要被清掉，不再挡投递。"""
    # 第一轮：有 JD 规则并命中
    first = RecordingContext(
        has_jd_rules=True,
        jd_scopes=("job_desc",),
        company_matches=empty_matcher,
        jd_matches=jd_matcher("外包"),
    )
    store, _, _ = run_crawl(
        tmp_path,
        [ok_page([api_item("j1")]), detail_ok("岗位外包")],
        context=first.ctx,
        enrich_details=True,
    )
    try:
        assert store.get_job("j1").jd_blacklisted is True
    finally:
        store.close()

    # 第二轮：规则已删（has_jd_rules=False），重抓同一条
    second = RecordingContext(has_jd_rules=False, company_matches=empty_matcher)
    store2, _, _ = run_crawl(
        tmp_path,
        [ok_page([api_item("j1")])],
        context=second.ctx,
        enrich_details=False,
    )
    try:
        job = store2.get_job("j1")
        assert job is not None
        assert job.jd_blacklisted is False
        assert job.auto_delivery_blocked is False
        assert all(s.get("rule_type") != "jd_keyword" for s in job.matched_rule_snapshot)
    finally:
        store2.close()


# --------------------------------------------------------------------------- #
# 期望流路径：黑名单钩子照常触发
# --------------------------------------------------------------------------- #


def test_期望流路径下公司命中照常跳过(tmp_path):
    """第三条检索路径（recommend/job/list）不能绕过黑名单——钩子在 crawl 里，不在路由上。"""
    from boss_filter.expect import JobExpectFilter

    rec = RecordingContext(
        store_blacklisted=False,
        company_matches=company_matcher("甲公司"),
    )
    expect = JobExpectFilter(kind="expect", encrypt_expect_id="E1")
    store, report, http = run_crawl(
        tmp_path,
        [ok_page([api_item("j1", brand_name="甲公司"), api_item("j2", brand_name="正常公司")]),
         detail_ok("正常公司的 JD")],
        context=rec.ctx,
        enrich_details=True,
        expect_filter=expect,
    )
    try:
        assert "recommend/job/list" in http.calls[0]["url"]
        assert store.get_job("j1") is None
        assert store.get_job("j2") is not None
        assert report.stats.company_hits == 1
        assert report.stats.skipped == 1
        assert rec.actions_for("j1") == ["skipped"]
    finally:
        store.close()
