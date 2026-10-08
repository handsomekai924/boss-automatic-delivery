"""简历 LLM 固定模板解析 + 匹配任务（pros/cons）的单元测试。"""

from __future__ import annotations

import json

import pytest

from boss_web.services.resume_parser import (
    ResumeParseError,
    build_messages,
    normalize,
    parse_and_stamp,
    parse_resume_llm,
)
from boss_web.services.resume_store import (
    save_analysis,
    save_llm_parse,
    load_llm_parse,
    update_greeting,
)


# --------------------------------------------------------------------------- #
# resume_parser
# --------------------------------------------------------------------------- #


def test_build_messages_shape():
    msgs = build_messages("# 张三\n## 工作经历\nxxx")
    assert msgs[0]["role"] == "system"
    assert "JSON" in msgs[0]["content"]
    assert "固定模板" in msgs[1]["content"]
    assert "# 张三" in msgs[1]["content"]
    retry = build_messages("x", retry=True)
    assert "严格只输出 JSON" in retry[1]["content"]


def test_normalize_fills_template_shape():
    data = normalize(
        {
            "basic": {"name": "张三", "phone": "138"},
            "intent": {"position": "Python"},
            "work": [{"company": "A", "title": "后端", "highlights": ["做了 X", "", "做了 Y"]}],
            "skills": ["Python", "Python", "FastAPI"],
            "summary": "s" * 700,
        }
    )
    assert data["basic"]["name"] == "张三"
    assert data["basic"]["email"] == ""
    assert data["intent"]["position"] == "Python"
    assert data["work"][0]["company"] == "A"
    assert data["work"][0]["highlights"] == ["做了 X", "做了 Y"]
    assert data["skills"] == ["Python", "FastAPI"]
    assert len(data["summary"]) <= 600
    assert data["project"] == []
    assert data["education"] == []


class _FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0
        self.config = type("C", (), {"model": "fake-model"})()

    def chat(self, messages, **kw):
        self.calls += 1
        return self.replies.pop(0) if self.replies else "{}"


def test_parse_resume_llm_ok():
    llm = _FakeLLM([json.dumps({"basic": {"name": "A"}, "summary": "s"}, ensure_ascii=False)])
    data = parse_resume_llm("简历原文", llm=llm)
    assert data["basic"]["name"] == "A"
    assert llm.calls == 1


def test_parse_resume_llm_retries_then_fails():
    llm = _FakeLLM(["这不是 JSON", "仍然不是 JSON"])
    with pytest.raises(ResumeParseError):
        parse_resume_llm("简历原文", llm=llm)
    assert llm.calls == 2


def test_parse_resume_llm_retry_succeeds():
    llm = _FakeLLM(
        ["废话", json.dumps({"summary": "ok"}, ensure_ascii=False)]
    )
    data = parse_resume_llm("简历原文", llm=llm)
    assert data["summary"] == "ok"
    assert llm.calls == 2


def test_parse_and_stamp_shape():
    llm = _FakeLLM([json.dumps({"summary": "s"}, ensure_ascii=False)])
    payload = parse_and_stamp("原文", model="m1", llm=llm)
    assert set(payload) == {"parsed_at", "model", "data"}
    assert payload["model"] == "m1"
    assert payload["data"]["summary"] == "s"


def test_parse_empty_content():
    llm = _FakeLLM([])
    with pytest.raises(ResumeParseError):
        parse_resume_llm("   ", llm=llm)


# --------------------------------------------------------------------------- #
# resume_store：meta.llm 落库 + 招呼语 PATCH
# --------------------------------------------------------------------------- #


@pytest.fixture()
def db(tmp_path, monkeypatch):
    import boss_db

    monkeypatch.setenv("BOSS_DB", str(tmp_path / "t.db"))
    boss_db.close_all()
    yield
    boss_db.close_all()


def test_save_and_load_llm_parse(db):
    from boss_web.services.resume_store import save_resume

    draft = save_resume("# 简历\n正文", filename="a.md")
    assert load_llm_parse(draft.resume_id) is None

    payload = {"parsed_at": 1.0, "model": "m", "data": {"summary": "s"}}
    save_llm_parse(draft.resume_id, payload)
    assert load_llm_parse(draft.resume_id) == payload

    # 其余 meta 键不被覆盖
    from boss_web.services.resume_store import list_resumes

    items = list_resumes()
    one = next(i for i in items if i["resume_id"] == draft.resume_id)
    assert one["title"] == "a.md"
    assert one["llm"]["has_data"] is True


def test_update_greeting(db):
    save_analysis(
        {
            "analysis_id": "an_x",
            "matches": [
                {"encrypt_job_id": "j1", "greeting": "你好"},
                {"encrypt_job_id": "j2", "greeting": "嗨"},
            ],
        }
    )
    item = update_greeting("an_x", "j2", "改过的招呼语")
    assert item["greeting"] == "改过的招呼语"
    from boss_web.services.resume_store import load_analysis

    payload = load_analysis("an_x")
    assert payload["matches"][0]["greeting"] == "你好"  # 只改那一条
    assert payload["matches"][1]["greeting"] == "改过的招呼语"

    with pytest.raises(KeyError):
        update_greeting("an_x", "nope", "x")


# --------------------------------------------------------------------------- #
# match_task：pros/cons + JD 注入
# --------------------------------------------------------------------------- #


def test_match_one_has_pros_and_cons():
    from boss_jobs.models import Job
    from boss_web.services.match_task import _job_desc_block, _match_one

    job = Job(
        job_name="Python",
        brand_name="某公司",
        location="广州",
        salary_desc="20-30K",
        job_experience="3-5年",
        job_degree="本科",
        brand_industry="互联网",
        brand_scale_name="100-499人",
        encrypt_job_id="j1",
        security_id="sid",
        lid="lid1",
        job_desc="负责后端开发\n要求 Python",
    )
    llm = _FakeLLM(
        [
            json.dumps(
                {
                    "match_score": 85,
                    "matched_skills": ["Python"],
                    "missing_skills": ["K8s"],
                    "verdict": "合适",
                    "pros": ["亮点A"],
                    "cons": ["短板B"],
                    "advice": "补 K8s",
                    "greeting": "你好",
                },
                ensure_ascii=False,
            )
        ]
    )
    result = _match_one(llm, "简历摘要", job)
    assert result["match_score"] == 85
    assert result["pros"] == ["亮点A"]
    assert result["cons"] == ["短板B"]
    assert result["greeting"] == "你好"
    assert "负责后端开发" in _job_desc_block(job)


def test_job_desc_fallback_when_missing():
    from boss_jobs.models import Job
    from boss_web.services.match_task import _job_desc_block

    job = Job(
        job_name="X",
        brand_name="Y",
        location="",
        salary_desc="",
        job_experience="",
        job_degree="",
        brand_industry="",
        brand_scale_name="",
        encrypt_job_id="j2",
        job_labels=("3-5年", "本科"),
        skills=("Python",),
    )
    block = _job_desc_block(job)
    assert "未抓到 JD" in block
    assert "Python" in block


# --------------------------------------------------------------------------- #
# API 层：parse / greeting PATCH
# --------------------------------------------------------------------------- #


def test_api_parse_requires_llm_config():
    from fastapi.testclient import TestClient

    from boss_web import create_app

    c = TestClient(create_app())
    body = "# 你好\n正文".encode("utf-8")
    r = c.post("/api/resume/upload", files={"file": ("a.md", body, "text/markdown")})
    assert r.status_code == 200
    rid = r.json()["resume_id"]
    r = c.post(f"/api/resume/item/{rid}/parse")
    assert r.status_code == 422
    assert "LLM" in r.json()["message"]
    c.delete(f"/api/resume/item/{rid}")


def test_api_greeting_patch_roundtrip():
    from fastapi.testclient import TestClient

    from boss_web import create_app

    c = TestClient(create_app())
    save_analysis(
        {
            "analysis_id": "an_api",
            "matches": [{"encrypt_job_id": "j9", "greeting": "旧招呼"}],
        }
    )
    r = c.patch(
        "/api/match/an_api/greeting",
        json={"encrypt_job_id": "j9", "greeting": "新招呼语"},
    )
    assert r.status_code == 200
    assert r.json()["item"]["greeting"] == "新招呼语"

    r = c.get("/api/match/analyses/an_api")
    assert r.status_code == 200
    assert r.json()["matches"][0]["greeting"] == "新招呼语"

    r = c.patch(
        "/api/match/an_api/greeting",
        json={"encrypt_job_id": "missing", "greeting": "x"},
    )
    assert r.status_code == 404
