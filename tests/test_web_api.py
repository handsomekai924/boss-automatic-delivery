"""网页层的接口冒烟测试（不打真网）。"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from boss_web import create_app


@pytest.fixture
def client():
    # 状态库由 conftest 的 isolated_db 统一指到 tmp_path
    return TestClient(create_app())


def test_health(client: TestClient):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["service"] == "boss_web"


def test_index_serves_spa(client: TestClient):
    r = client.get("/")
    assert r.status_code == 200
    assert "BOSS" in r.text


def test_auth_status(client: TestClient):
    r = client.get("/api/auth/status")
    assert r.status_code == 200
    assert "logged_in" in r.json()


def test_jobs_list(client: TestClient):
    r = client.get("/api/jobs?limit=5")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body and "total" in body


def test_jobs_delete_requires_existing(client: TestClient):
    r = client.delete("/api/jobs/does-not-exist")
    assert r.status_code == 404


def test_jobs_clear_requires_confirm(client: TestClient):
    r = client.post("/api/jobs/clear", json={"confirm": False})
    assert r.status_code == 422


def test_llm_config_masked(client: TestClient):
    r = client.get("/api/llm/config")
    assert r.status_code == 200
    body = r.json()
    assert "api_key" in body and "configured" in body
    assert "sk-secret" not in body.get("api_key", "")
    # 采样参数是系统固定值，随配置一起回显
    assert "fixed" in body


def test_llm_put_ignores_sampling_params(client: TestClient):
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
    r = client.post("/api/llm/models", json={"base_url": "https://example.com/v1"})
    assert r.status_code == 422
    assert "Key" in str(r.json())


def test_filters_search_roundtrip(client: TestClient):
    r = client.put(
        "/api/filters/search",
        json={"query": "Python", "city": "101280100", "jobType": "1901", "salary": ""},
    )
    assert r.status_code == 200
    assert r.json()["query"] == "Python"
    r2 = client.get("/api/filters/search")
    assert r2.json()["city"] == "101280100"


def test_crawl_status_idle(client: TestClient):
    r = client.get("/api/crawl/status")
    assert r.status_code == 200
    assert r.json()["status"] == "idle"


def test_resume_upload_md(client: TestClient):
    content = "## 技能标签\n- Python、FastAPI\n\n## 求职意向\n后端\n".encode("utf-8")
    r = client.post(
        "/api/resume/upload",
        files={"file": ("my.md", content, "text/markdown")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["resume_id"].startswith("rs_")
    assert "Python" in body["skills"]
    # 清理
    client.delete(f"/api/resume/item/{body['resume_id']}")


def test_resume_rejects_bad_ext(client: TestClient):
    r = client.post(
        "/api/resume/upload",
        files={"file": ("evil.exe", b"xx", "application/octet-stream")},
    )
    assert r.status_code == 422


def test_jobs_stats_reports_missing_desc(client: TestClient):
    r = client.get("/api/jobs/stats")
    assert r.status_code == 200
    assert "missing_desc" in r.json()


def test_fetch_descriptions_status_idle(client: TestClient):
    r = client.get("/api/jobs/fetch-descriptions/status")
    assert r.status_code == 200
    assert r.json()["status"] == "idle"


def test_fetch_descriptions_not_eaten_by_dynamic_route(client: TestClient):
    """/fetch-descriptions/status 必须排在 /{encrypt_job_id} 之前。"""
    r = client.get("/api/jobs/fetch-descriptions/status")
    assert r.status_code == 200
    assert "task_id" in r.json()


def test_fetch_descriptions_cancel_when_idle_404(client: TestClient):
    r = client.post("/api/jobs/fetch-descriptions/cancel")
    assert r.status_code == 404


def test_fetch_descriptions_start_then_status(client: TestClient, monkeypatch):
    """起一个补抓任务：没有登录态会在任务里报 error，但接口本身要回。"""
    # 免掉真拉 Chrome / 真起 HTTP
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
    assert s["status"] in {"running", "error"}  # 线程可能已经收尾


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
        def fetch_job_detail(self, *, security_id, lid):
            raise JobApiError(37, "您的环境存在异常.", raw={})

    monkeypatch.setattr(
        "boss_web.services.desc_task.create_client", lambda **_kw: AlwaysBrowserCheck()
    )
    # 缺描述的职位塞满，够撞到 giveup 阈值
    monkeypatch.setattr(
        "boss_web.services.desc_task.JobStore",
        lambda *a, **k: _StoreWithMissing([_fake_job(i) for i in range(10)]),
    )
    monkeypatch.setattr("boss_web.services.desc_task.time.sleep", lambda _s: None)

    mgr = DescTaskManager()
    task = mgr.start(limit=0, interval=0.0)
    # 线程很快收尾；轮询到不再 running
    for _ in range(200):
        if task.status != "running":
            break
        time.sleep(0.01)

    snap = task.snapshot()
    kinds = [e["event"] for e in snap["events"]]
    assert "stopped" in kinds
    stopped = next(e for e in snap["events"] if e["event"] == "stopped")
    assert "连续" in stopped["reason"] and "code 37" in stopped["reason"]
    # 只碰了 giveup 条，没把 10 条都砸一遍
    from boss_jobs.config import BROWSER_CHECK_GIVEUP

    assert snap["progress"]["failed"] == BROWSER_CHECK_GIVEUP
    assert snap["progress"]["done"] == BROWSER_CHECK_GIVEUP


def _fake_job(i: int):
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
        self._jobs = list(jobs)

    def list_jobs_missing_desc(self, *, limit: int = 20, offset: int = 0):
        return self._jobs[:limit]

    def update_job_desc(self, *a, **k):
        return True

    def close(self):
        pass
