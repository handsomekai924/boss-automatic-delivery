"""网页层的接口冒烟测试（不打真网）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from boss_web import config as C
from boss_web import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    # LLM 配置读写指到临时文件，别把真实的 data/llm_config.json 冲掉
    monkeypatch.setattr(C, "LLM_CONFIG_PATH", tmp_path / "llm_config.json")
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
