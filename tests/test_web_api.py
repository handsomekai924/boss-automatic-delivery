"""网页层的接口冒烟测试（不打真网）。"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from boss_web import create_app


@pytest.fixture
def client():
    """提供隔离库下的 TestClient。"""
    return TestClient(create_app())


def test_health(client: TestClient):
    """验证 /api/health 回服务名。"""
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["service"] == "boss_web"


def test_index_serves_spa(client: TestClient):
    """验证根路径吐出 SPA 首页。"""
    r = client.get("/")
    assert r.status_code == 200
    assert "BOSS" in r.text


def test_auth_status(client: TestClient):
    """验证 /api/auth/status 带登录态字段。"""
    r = client.get("/api/auth/status")
    assert r.status_code == 200
    assert "logged_in" in r.json()


def test_slider_refresh_unknown_task_404(client: TestClient):
    """验证刷新不存在的滑块任务回 404。"""
    r = client.post("/api/auth/slider/nope/refresh")
    assert r.status_code == 404


def test_slider_refresh_换新挑战重建帮助页(client: TestClient):
    """「重新加载」必须换一张新 challenge：极验票据一次性，重载旧的必然 onError。"""
    from boss_login.verify import SliderChallenge
    from boss_web.services import login_task as lt

    calls: list[int] = []

    def refresher() -> SliderChallenge:
        """换一张新 challenge 的刷新器。"""
        calls.append(1)
        return SliderChallenge(
            captcha_type=1, captcha_name="极验验证", gt="gt-x", challenge=f"ch-{len(calls)}"
        )

    task = lt.LoginTask(task_id="refresh-1", phone="13800138000")
    task.status = lt.ST_NEED_SLIDER
    task.slider_html = "<html>old</html>"
    task.slider_refresher = refresher
    lt.login_tasks._tasks[task.task_id] = task
    try:
        r = client.post(f"/api/auth/slider/{task.task_id}/refresh")
        assert r.status_code == 200
        assert calls == [1]
        assert task.slider_needed is True
        page = client.get(f"/api/auth/slider/{task.task_id}")
        assert page.status_code == 200
        assert "ch-1" in page.text
        assert "old" not in page.text
    finally:
        lt.login_tasks._tasks.pop(task.task_id, None)


def test_slider_refresh_without_active_slider_conflicts(client: TestClient):
    """验证没有待验证滑块时刷新冲突 409。"""
    from boss_web.services import login_task as lt

    task = lt.LoginTask(task_id="refresh-2", phone="13800138000")
    lt.login_tasks._tasks[task.task_id] = task
    try:
        r = client.post(f"/api/auth/slider/{task.task_id}/refresh")
        assert r.status_code == 409
    finally:
        lt.login_tasks._tasks.pop(task.task_id, None)




def test_qr_image_without_payload_404(client: TestClient):
    """验证二维码无 payload 时图片回 404。"""
    from boss_web.services import login_task as lt

    task = lt.LoginTask(task_id="qr-empty", phone="")
    lt.login_tasks._tasks[task.task_id] = task
    try:
        r = client.get(f"/api/auth/qr/{task.task_id}/image")
        assert r.status_code == 404
    finally:
        lt.login_tasks._tasks.pop(task.task_id, None)


def test_qr_image_renders_png(client: TestClient):
    """快照里有 payload 就能出 PNG——给前端 <img src> 用。"""
    from boss_web.services import login_task as lt

    task = lt.LoginTask(task_id="qr-img", phone="")
    task.qr_payload = "qr-uuid-1234"
    lt.login_tasks._tasks[task.task_id] = task
    try:
        r = client.get(f"/api/auth/qr/{task.task_id}/image")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
        assert r.headers.get("cache-control") == "no-store"
    finally:
        lt.login_tasks._tasks.pop(task.task_id, None)


def test_qr_snapshot_carries_image_url(client: TestClient):
    """验证扫码快照带上图片地址与扫描状态。"""
    from boss_web.services import login_task as lt

    task = lt.LoginTask(task_id="qr-snap", phone="", kind="qr")
    task.qr_payload = "qr-uuid"
    task.status = lt.ST_NEED_SCAN
    lt.login_tasks._tasks[task.task_id] = task
    try:
        snap = task.snapshot()
        assert snap["status"] == "need_scan"
        assert snap["kind"] == "qr"
        assert snap["qr"]["payload"] == "qr-uuid"
        assert snap["qr"]["image_url"].startswith("/api/auth/qr/qr-snap/image")
        assert snap["qr"]["scanned"] is False
    finally:
        lt.login_tasks._tasks.pop(task.task_id, None)


def test_qr_conflict_when_sms_running(client: TestClient):
    """同一时刻只跑一个登录任务：短信没跑完就不该另起扫码。"""
    from boss_web.services import login_task as lt

    task = lt.LoginTask(task_id="busy", phone="13800138000")
    task.status = lt.ST_NEED_CODE
    lt.login_tasks._current = task
    try:
        r = client.post("/api/auth/qr", json={})
        assert r.status_code == 409
    finally:
        lt.login_tasks._current = None


def test_jobs_list(client: TestClient):
    """验证职位列表返回 items/total。"""
    r = client.get("/api/jobs?limit=5")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body and "total" in body


def test_jobs_delete_requires_existing(client: TestClient):
    """验证删除不存在的职位回 404。"""
    r = client.delete("/api/jobs/does-not-exist")
    assert r.status_code == 404


def test_jobs_clear_requires_confirm(client: TestClient):
    """验证清空职位未确认时回 422。"""
    r = client.post("/api/jobs/clear", json={"confirm": False})
    assert r.status_code == 422


def test_llm_config_masked(client: TestClient):
    """验证 LLM 配置把 api_key 打码。"""
    r = client.get("/api/llm/config")
    assert r.status_code == 200
    body = r.json()
    assert "api_key" in body and "configured" in body
    assert "sk-secret" not in body.get("api_key", "")
    assert "fixed" in body


def test_llm_put_ignores_sampling_params(client: TestClient):
    """验证写配置丢掉采样参数。"""
    r = client.put(
        "/api/llm/config",
        json={"base_url": "https://example.com/v1", "model": "m-x", "temperature": 1.9, "max_tokens": 16},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["model"] == "m-x"
    assert body["temperature"] != 1.9
    assert body["fixed"]["max_tokens"] == body["max_tokens"]


def test_llm_models_requires_key(client: TestClient):
    """验证列模型必须带 api_key。"""
    r = client.post("/api/llm/models", json={"base_url": "https://example.com/v1"})
    assert r.status_code == 422
    assert "Key" in str(r.json())


def test_filters_search_roundtrip(client: TestClient):
    """验证搜索筛选条件写读往返。"""
    r = client.put(
        "/api/filters/search",
        json={"query": "Python", "city": "101280100", "jobType": "1901", "salary": ""},
    )
    assert r.status_code == 200
    assert r.json()["query"] == "Python"
    r2 = client.get("/api/filters/search")
    assert r2.json()["city"] == "101280100"


def test_collection_config_期望流字段往返(client: TestClient):
    """expect_kind / encrypt_expect_id / mix_expect_type / expect_info / expect_name 要存得下、读得回。"""
    r = client.put(
        "/api/collection/config",
        json={
            "max_pages": 3,
            "use_search": True,
            "expect_kind": "expect",
            "encrypt_expect_id": "E1",
            "mix_expect_type": "",
            "expect_info": "",
            "expect_name": "前端开发工程师(佛山)",
        },
    )
    assert r.status_code == 200
    cfg = r.json()["collection"]
    assert cfg["expect_kind"] == "expect"
    assert cfg["encrypt_expect_id"] == "E1"
    assert cfg["expect_name"] == "前端开发工程师(佛山)"
    assert r.json()["search"]["encryptExpectId"] == "E1"

    r2 = client.get("/api/collection/config")
    assert r2.json()["collection"]["expect_kind"] == "expect"
    assert r2.json()["collection"]["encrypt_expect_id"] == "E1"


def test_collection_config_临时期望的expectInfo往返(client: TestClient):
    """temp_expect 主参数是 expectInfo JSON 串，要原样存得下、读得回（不被清洗掉）。"""
    info = '{"expectType":"1","position":"python","location":"101280100","lowSalary":"10","highSalary":"20"}'
    r = client.put(
        "/api/collection/config",
        json={
            "max_pages": 3,
            "use_search": True,
            "expect_kind": "temp_expect",
            "encrypt_expect_id": "",
            "mix_expect_type": "",
            "expect_info": info,
            "expect_name": "临时期望：python",
        },
    )
    assert r.status_code == 200
    cfg = r.json()["collection"]
    assert cfg["expect_kind"] == "temp_expect"
    assert cfg["expect_info"] == info
    assert cfg["expect_name"] == "临时期望：python"
    assert cfg["encrypt_expect_id"] == ""

    r2 = client.get("/api/collection/config")
    assert r2.json()["collection"]["expect_info"] == info


def test_collection_config_旧字段缺省不启用期望流(client: TestClient):
    """旧库缺这几把 key → 空串走搜索流；旧「推荐页」配置也归一成搜索流。"""
    r = client.put(
        "/api/collection/config",
        json={"max_pages": 5, "use_search": False},
    )
    assert r.status_code == 200
    cfg = r.json()["collection"]
    assert cfg["expect_kind"] == ""
    assert cfg["encrypt_expect_id"] == ""
    assert cfg["use_search"] is True


def test_collection_expects_桩响应解析(client: TestClient, monkeypatch):
    """GET /api/collection/expects 把三类期望摊成前端下拉要的行。"""
    from boss_jobs.models import ExpectItem, ExpectList

    class StubClient:
        """桩客户端：回一份固定的期望列表。"""

        def fetch_expect_list(self):
            """返回固定的期望列表。"""
            return ExpectList(
                mix=ExpectItem(
                    kind="mix",
                    encrypt_expect_id="MIX1",
                    position_name="前端开发工程师",
                    location_name="佛山",
                    mix_expect_type="2",
                ),
                expects=(
                    ExpectItem(kind="expect", encrypt_expect_id="E1", position_name="Python", location_name="广州"),
                ),
                part_time=(
                    ExpectItem(kind="part_time", encrypt_expect_id="PT1", position_name="派单", location_name="佛山"),
                ),
            )

    monkeypatch.setattr("boss_jobs.create_client", lambda *a, **k: StubClient())
    r = client.get("/api/collection/expects")
    assert r.status_code == 200
    body = r.json()
    assert body["mix"]["encryptExpectId"] == "MIX1"
    assert body["mix"]["mixExpectType"] == "2"
    assert body["mix"]["label"] == "前端开发工程师(佛山)"
    assert [row["encryptExpectId"] for row in body["expects"]] == ["E1"]
    assert [row["encryptExpectId"] for row in body["partTime"]] == ["PT1"]
    assert body["expects"][0]["kind"] == "expect"


def test_collection_expects_上游失败转成人话(client: TestClient, monkeypatch):
    """验证上游失败转成可读报错。"""
    from boss_jobs.errors import JobError

    class BoomClient:
        """桩客户端：一律抛 JobError。"""

        def fetch_expect_list(self):
            """抛出 JobError。"""
            raise JobError("没登录")

    monkeypatch.setattr("boss_jobs.create_client", lambda *a, **k: BoomClient())
    r = client.get("/api/collection/expects")
    assert r.status_code >= 400
    assert "求职期望" in r.text or "没登录" in r.text


def test_collection_expects_没登录不炸500(client: TestClient, monkeypatch):
    """没登录时 `create_client()` 先炸：异常要转成人话，不能掉进兜底 500。"""
    from boss_jobs.errors import JobApiError

    def boom(*_a, **_k):
        """抛出未登录的 JobApiError。"""
        raise JobApiError(
            7,
            "本地没有登录态：状态库 F:\\boss\\data\\boss.db 里没有，"
            "或 token/Cookie 都没有。先跑 `python -m boss_login login` 落一份。",
        )

    monkeypatch.setattr("boss_jobs.create_client", boom)
    r = client.get("/api/collection/expects")
    body = r.json()
    assert body["code"] != "internal", body
    assert r.status_code != 500
    assert "「登录」页" in body["message"]
    assert "python -m boss_login" not in body["message"]


def test_未处理的JobError有安全网(client: TestClient):
    """路由漏 catch 的 `JobError` 也要被 app 层翻成人话，不许兜底 500。"""
    from fastapi.testclient import TestClient as TC

    from boss_jobs.errors import JobError
    from boss_web.app import create_app

    app = create_app()

    def _boom():
        """注册用的抛错路由。"""
        raise JobError("本地没有登录态：状态库 boss.db 里没有")

    app.add_api_route("/api/__boom__", _boom, methods=["GET"], include_in_schema=False)
    app.router.routes.insert(0, app.router.routes.pop())

    r = TC(app).get("/api/__boom__")
    assert r.status_code == 502
    body = r.json()
    assert body["code"] == "upstream", body
    assert body["code"] != "internal", body
    assert "「登录」页" in body["message"]


def test_crawl_status_idle(client: TestClient):
    """验证抓取状态空闲时为 idle。"""
    r = client.get("/api/crawl/status")
    assert r.status_code == 200
    assert r.json()["status"] == "idle"


def test_resume_upload_md(client: TestClient):
    """验证上传 Markdown 简历并解析技能。"""
    content = "## 技能标签\n- Python、FastAPI\n\n## 求职意向\n后端\n".encode("utf-8")
    r = client.post(
        "/api/resume/upload",
        files={"file": ("my.md", content, "text/markdown")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["resume_id"].startswith("rs_")
    assert "Python" in body["skills"]
    client.delete(f"/api/resume/item/{body['resume_id']}")


def test_resume_rejects_bad_ext(client: TestClient):
    """验证非法扩展名简历被拒。"""
    r = client.post(
        "/api/resume/upload",
        files={"file": ("evil.exe", b"xx", "application/octet-stream")},
    )
    assert r.status_code == 422


def test_jobs_stats_reports_missing_desc(client: TestClient):
    """验证职位统计带缺描述计数。"""
    r = client.get("/api/jobs/stats")
    assert r.status_code == 200
    assert "missing_desc" in r.json()


def test_fetch_descriptions_status_idle(client: TestClient):
    """验证补描述空闲状态为 idle。"""
    r = client.get("/api/jobs/fetch-descriptions/status")
    assert r.status_code == 200
    assert r.json()["status"] == "idle"


def test_fetch_descriptions_not_eaten_by_dynamic_route(client: TestClient):
    """/fetch-descriptions/status 必须排在 /{encrypt_job_id} 之前。"""
    r = client.get("/api/jobs/fetch-descriptions/status")
    assert r.status_code == 200
    assert "task_id" in r.json()


def test_fetch_descriptions_cancel_when_idle_404(client: TestClient):
    """验证空闲时取消补抓回 404。"""
    r = client.post("/api/jobs/fetch-descriptions/cancel")
    assert r.status_code == 404


def test_fetch_descriptions_start_then_status(client: TestClient, monkeypatch):
    """起一个补抓任务：没有登录态会在任务里报 error，但接口本身要回。"""
    monkeypatch.setattr(
        "boss_web.services.desc_task.create_client",
        lambda **_kw: (_ for _ in ()).throw(RuntimeError("no session")),
    )
    r = client.post("/api/jobs/fetch-descriptions", json={"limit": 1})
    assert r.status_code == 200
    task_id = r.json()["task_id"]
    assert task_id

    s = client.get("/api/jobs/fetch-descriptions/status").json()
    assert s["task_id"] == task_id
    assert s["status"] in {"running", "error"}


def test_fetch_descriptions_interval_默认1秒(client: TestClient, monkeypatch):
    """不传 interval = 服务端 DETAIL_INTERVAL（1s），防风控；前端进度条要能拿到它。"""
    monkeypatch.setattr(
        "boss_web.services.desc_task.create_client",
        lambda **_kw: (_ for _ in ()).throw(RuntimeError("no session")),
    )
    r = client.post("/api/jobs/fetch-descriptions", json={"limit": 1})
    assert r.status_code == 200
    assert r.json()["progress"]["interval"] == 1.0


def test_fetch_descriptions_interval_可传(client: TestClient, monkeypatch):
    """验证补抓间隔可显式传入。"""
    monkeypatch.setattr(
        "boss_web.services.desc_task.create_client",
        lambda **_kw: (_ for _ in ()).throw(RuntimeError("no session")),
    )
    r = client.post("/api/jobs/fetch-descriptions", json={"limit": 1, "interval": 2.5})
    assert r.status_code == 200
    assert r.json()["progress"]["interval"] == 2.5


def test_desc_task_interval_夹到02秒下限(monkeypatch):
    """手滑填 0.01s 会被夹到 0.2s——比这更快就是自己敲风控。"""
    from boss_web.services.desc_task import DescTaskManager

    monkeypatch.setattr(
        "boss_web.services.desc_task.create_client",
        lambda **_kw: (_ for _ in ()).throw(RuntimeError("no session")),
    )
    mgr = DescTaskManager()
    task = mgr.start(limit=1, interval=0.01)
    assert task.progress["interval"] == 0.2


def test_desc_task_连环撞37就停批(tmp_path, monkeypatch):
    """连续 N 次 code 37 = 安全网关整段限速，停整批而不是拿剩下的去探墙。"""
    from boss_jobs.errors import JobApiError
    from boss_web.services.desc_task import DescTaskManager

    class AlwaysBrowserCheck:
        """桩客户端：详情接口一律回 code 37。"""

        def fetch_job_detail(self, *, security_id, lid):
            """抛出 code 37 浏览器检查异常。"""
            raise JobApiError(37, "您的环境存在异常.", raw={})

    monkeypatch.setattr(
        "boss_web.services.desc_task.create_client", lambda **_kw: AlwaysBrowserCheck()
    )
    monkeypatch.setattr(
        "boss_web.services.desc_task.JobStore",
        lambda *a, **k: _StoreWithMissing([_fake_job(i) for i in range(10)]),
    )
    real_sleep = time.sleep
    monkeypatch.setattr("boss_web.services.desc_task.time.sleep", lambda _s: None)

    mgr = DescTaskManager()
    task = mgr.start(limit=0, interval=0.0)
    for _ in range(200):
        if task.status != "running":
            break
        real_sleep(0.01)

    snap = task.snapshot()
    kinds = [e["event"] for e in snap["events"]]
    assert "stopped" in kinds
    stopped = next(e for e in snap["events"] if e["event"] == "stopped")
    assert "连续" in stopped["reason"] and "code 37" in stopped["reason"]
    from boss_jobs.config import BROWSER_CHECK_GIVEUP

    assert snap["progress"]["failed"] == BROWSER_CHECK_GIVEUP
    assert snap["progress"]["done"] == BROWSER_CHECK_GIVEUP


def _fake_job(i: int):
    """造一条待补描述的假职位。"""
    from boss_jobs.models import Job

    return Job.from_api(
        {
            "encryptJobId": f"j{i}",
            "jobName": f"岗位{i}",
            "brandName": "甲",
            "securityId": f"s{i}",
            "lid": "l",
        }
    )


class _StoreWithMissing:
    """最小 JobStore 桩：只实现补抓任务用到的那几个方法。"""

    def __init__(self, jobs):
        """初始化。"""
        self._jobs = list(jobs)

    def list_jobs_missing_desc(self, *, limit: int = 20, offset: int = 0):
        """返回待补描述的职位切片。"""
        return self._jobs[:limit]

    def update_job_desc(self, *a, **k):
        """假装写入描述成功。"""
        return True

    def update_blacklist_state(self, *a, **k):
        """假装更新黑名单状态成功。"""
        return True

    def close(self):
        """关闭。"""
        pass
