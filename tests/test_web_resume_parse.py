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




def test_build_messages_shape():
    """验证 build_messages 的消息形状与重试提示。"""
    msgs = build_messages("# 张三\n## 工作经历\nxxx")
    assert msgs[0]["role"] == "system"
    assert "JSON" in msgs[0]["content"]
    assert "固定模板" in msgs[1]["content"]
    assert "# 张三" in msgs[1]["content"]
    retry = build_messages("x", retry=True)
    assert "严格只输出 JSON" in retry[1]["content"]


def test_normalize_fills_template_shape():
    """验证 normalize 补齐模板字段并去重截断。"""
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
    """按序回脚本的假 LLM。"""
    def __init__(self, replies):
        """初始化。"""
        self.replies = list(replies)
        self.calls = 0
        self.messages = []
        self.kwargs = []
        self.config = type("C", (), {"model": "fake-model"})()

    def chat(self, messages, **kw):
        """回下一条脚本文本并记录调用。"""
        self.calls += 1
        self.messages.append(messages)
        self.kwargs.append(kw)
        return self.replies.pop(0) if self.replies else "{}"


class _MetadataLLM(_FakeLLM):
    """带回 finish_reason 的假 LLM。"""
    def __init__(self, replies):
        """初始化。"""
        super().__init__(replies)
        self.finish_reasons = []

    def chat_with_metadata(self, messages, **kw):
        """回脚本文本与 finish_reason。"""
        from boss_web.services.llm_client import ChatResult

        self.calls += 1
        self.messages.append(messages)
        self.kwargs.append(kw)
        reply = self.replies.pop(0) if self.replies else "{}"
        reason = self.finish_reasons.pop(0) if self.finish_reasons else None
        return ChatResult(reply, reason)


def test_parse_resume_llm_ok():
    """验证 LLM 解析成功并带上 token 上限。"""
    llm = _FakeLLM([json.dumps({"basic": {"name": "A"}, "summary": "s"}, ensure_ascii=False)])
    data = parse_resume_llm("简历原文", llm=llm)
    assert data["basic"]["name"] == "A"
    assert llm.calls == 1
    from boss_web import config as C

    assert llm.kwargs == [{"max_tokens": C.LLM_RESUME_PARSE_MAX_TOKENS}]


def test_parse_resume_llm_retries_then_fails():
    """验证连续非 JSON 重试后仍失败。"""
    llm = _FakeLLM(["这不是 JSON", "仍然不是 JSON"])
    with pytest.raises(ResumeParseError):
        parse_resume_llm("简历原文", llm=llm)
    assert llm.calls == 2


def test_parse_resume_llm_retry_succeeds():
    """验证重试后解析成功。"""
    llm = _FakeLLM(
        ["废话", json.dumps({"summary": "ok"}, ensure_ascii=False)]
    )
    data = parse_resume_llm("简历原文", llm=llm)
    assert data["summary"] == "ok"
    assert llm.calls == 2
    from boss_web import config as C

    assert llm.kwargs == [
        {"max_tokens": C.LLM_RESUME_PARSE_MAX_TOKENS},
        {"max_tokens": C.LLM_RESUME_PARSE_MAX_TOKENS},
    ]


def test_parse_resume_llm_retries_with_higher_limit_after_truncation():
    """验证截断后用更高 token 上限重试。"""
    from boss_web import config as C

    llm = _MetadataLLM(["{\"summary\": \"partial\"}", json.dumps({"summary": "ok"})])
    llm.finish_reasons = ["length", "stop"]

    data = parse_resume_llm("简历原文", llm=llm)

    assert data["summary"] == "ok"
    assert llm.kwargs == [
        {"max_tokens": C.LLM_RESUME_PARSE_MAX_TOKENS},
        {"max_tokens": C.LLM_RESUME_PARSE_RETRY_MAX_TOKENS},
    ]


def test_parse_resume_llm_reports_truncation_after_retry():
    """验证重试仍截断时报长度上限错误。"""
    from boss_web import config as C

    llm = _MetadataLLM(["截断的 JSON", "仍然截断"])
    llm.finish_reasons = ["length", "length"]

    with pytest.raises(ResumeParseError, match="输出达到长度上限"):
        parse_resume_llm("简历原文", llm=llm)

    assert llm.kwargs == [
        {"max_tokens": C.LLM_RESUME_PARSE_MAX_TOKENS},
        {"max_tokens": C.LLM_RESUME_PARSE_RETRY_MAX_TOKENS},
    ]


def test_parse_resume_llm_propagates_upstream_error():
    """验证上游错误原样上抛。"""
    from boss_web.errors import UpstreamError

    class FailingLLM(_FakeLLM):
        """chat 会抛上游错误的假 LLM。"""
        def chat_with_metadata(self, messages, **kw):
            """抛出 UpstreamError。"""
            raise UpstreamError("AI 服务暂时不可用")

    with pytest.raises(UpstreamError):
        parse_resume_llm("简历原文", llm=FailingLLM([]))


def test_parse_and_stamp_shape():
    """验证 parse_and_stamp 的落库形状。"""
    llm = _FakeLLM([json.dumps({"summary": "s"}, ensure_ascii=False)])
    payload = parse_and_stamp("原文", model="m1", llm=llm)
    assert set(payload) == {"parsed_at", "model", "data"}
    assert payload["model"] == "m1"
    assert payload["data"]["summary"] == "s"


def test_parse_empty_content():
    """验证空白简历直接报解析错误。"""
    llm = _FakeLLM([])
    with pytest.raises(ResumeParseError):
        parse_resume_llm("   ", llm=llm)




@pytest.fixture()
def db(tmp_path, monkeypatch):
    """提供隔离的临时 BOSS 数据库。"""
    import boss_db

    monkeypatch.setenv("BOSS_DB", str(tmp_path / "t.db"))
    boss_db.close_all()
    yield
    boss_db.close_all()


def test_save_and_load_llm_parse(db):
    """验证 LLM 解析结果存取与简历列表标记。"""
    from boss_web.services.resume_store import save_resume

    draft = save_resume("# 简历\n正文", filename="a.md")
    assert load_llm_parse(draft.resume_id) is None

    payload = {"parsed_at": 1.0, "model": "m", "data": {"summary": "s"}}
    save_llm_parse(draft.resume_id, payload)
    assert load_llm_parse(draft.resume_id) == payload

    from boss_web.services.resume_store import list_resumes

    items = list_resumes()
    one = next(i for i in items if i["resume_id"] == draft.resume_id)
    assert one["title"] == "a.md"
    assert one["llm"]["has_data"] is True


def test_update_greeting(db):
    """验证只改指定招呼语且不动其它项。"""
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
    assert payload["matches"][0]["greeting"] == "你好"
    assert payload["matches"][1]["greeting"] == "改过的招呼语"

    with pytest.raises(KeyError):
        update_greeting("an_x", "nope", "x")




def test_match_task_start_sets_total_before_worker(monkeypatch):
    """验证 start 先定 total 再起 worker。"""
    import threading

    from boss_web.services.match_task import MatchTaskManager

    jobs = [object(), object()]
    picked = []
    worker_jobs = []
    worker_started = threading.Event()
    manager = MatchTaskManager()

    def pick_jobs(task):
        """返回预置职位列表。"""
        picked.append(task)
        return jobs

    def run(task, selected_jobs):
        """记录选中职位并置位事件。"""
        worker_jobs.extend(selected_jobs)
        worker_started.set()

    monkeypatch.setattr("boss_web.services.match_task._pick_jobs", pick_jobs)
    monkeypatch.setattr(manager, "_run", run)

    task = manager.start(resume_id="resume", job_ids=["j1", "j2"])
    snapshot = task.snapshot()

    assert snapshot["total"] == 2
    assert len(picked) == 1
    assert worker_started.wait(timeout=1)
    assert worker_jobs == jobs


def test_match_task_start_empty_jobs_is_error(monkeypatch):
    """验证无职位时任务直接报错。"""
    from boss_web.services.match_task import MatchTaskManager

    monkeypatch.setattr("boss_web.services.match_task._pick_jobs", lambda task: [])
    task = MatchTaskManager().start(resume_id="resume")

    snapshot = task.snapshot()
    assert snapshot["total"] == 0
    assert snapshot["status"] == "error"
    assert "没有可匹配的职位" in snapshot["error"]
    assert snapshot["ended_at"] is not None


def test_match_one_has_pros_and_cons():
    """验证匹配结果含 pros/cons 且 JD 注入 prompt。"""
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
            "不是 JSON",
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
            ),
        ]
    )
    result = _match_one(llm, "简历摘要", job)
    assert result["match_score"] == 85
    assert result["pros"] == ["亮点A"]
    assert result["cons"] == ["短板B"]
    assert result["greeting"] == "你好"
    from boss_web import config as C

    assert llm.kwargs == [
        {"max_tokens": C.LLM_MATCH_MAX_TOKENS},
        {"max_tokens": C.LLM_MATCH_MAX_TOKENS},
    ]
    prompt = llm.messages[0][1]["content"]
    assert "100–150 字" in prompt
    assert "60 字以内" not in prompt
    assert "负责后端开发" in _job_desc_block(job)


def test_legacy_analyzer_uses_match_token_limit():
    """验证旧分析器也用匹配 token 上限。"""
    from boss_jobs.models import Job
    from boss_web.services.resume_analyzer import _analyze_one
    from boss_web.services.resume_store import ResumeDraft
    from boss_web import config as C

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
        job_desc="负责后端开发",
    )
    draft = ResumeDraft("rs_x", "简历", "", 1.0, "原文")
    response = json.dumps({"match_score": 80, "verdict": "合适"}, ensure_ascii=False)
    llm = _FakeLLM(["非 JSON", response])

    result = _analyze_one(llm, draft, job)

    assert result["match_score"] == 80
    assert llm.kwargs == [
        {"max_tokens": C.LLM_MATCH_MAX_TOKENS},
        {"max_tokens": C.LLM_MATCH_MAX_TOKENS},
    ]


def test_job_desc_fallback_when_missing():
    """验证缺 JD 时回退用标签与技能。"""
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




def test_api_parse_requires_llm_config():
    """验证未配 LLM 时 parse 接口 422。"""
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
    """验证招呼语 PATCH 往返与 404。"""
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




def _seed_greet_world():
    """一份简历 + 一个职位 + 一份 analysis，够 regenerate 用。"""
    from boss_jobs.models import Job, PageResult
    from boss_jobs.store import JobStore
    from boss_web.services.resume_store import save_resume

    draft = save_resume("# 张三\nPython 后端", filename="a.md")
    save_llm_parse(
        draft.resume_id, {"parsed_at": 1.0, "model": "m", "data": {"summary": "会 Python"}}
    )
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
        job_desc="负责后端开发",
    )
    with JobStore() as store:
        store.save_page(PageResult(page=1, jobs=(job,), has_more=False, raw_count=1, dropped=()))
    save_analysis(
        {
            "analysis_id": "an_re",
            "resume_id": draft.resume_id,
            "matches": [{"encrypt_job_id": "j1", "job_name": "Python", "greeting": "旧招呼"}],
        }
    )


class _ScriptLLM:
    """按序回脚本化文本；顺手记下每次的 prompt，方便断言注入了 JD。"""

    def __init__(self, replies):
        """初始化。"""
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages, **kw):
        """回下一条脚本并记录 prompt。"""
        self.calls.append(messages)
        return self.replies.pop(0) if self.replies else ""


def test_regenerate_returns_draft_without_saving(db):
    """生成的招呼语只回给前端，库里那条还是旧的——用户可能反复生成再挑一条。"""
    from boss_web.services.match_task import regenerate_greeting
    from boss_web.services.resume_store import load_analysis

    _seed_greet_world()
    llm = _ScriptLLM(["您好，我有 3 年 Python 后端经验，想聊聊这个岗位"])
    text = regenerate_greeting(analysis_id="an_re", encrypt_job_id="j1", llm=llm)
    assert "Python" in text
    assert "负责后端开发" in llm.calls[0][1]["content"]
    assert load_analysis("an_re")["matches"][0]["greeting"] == "旧招呼"

    llm2 = _ScriptLLM(["第二版招呼语"])
    assert regenerate_greeting(analysis_id="an_re", encrypt_job_id="j1", llm=llm2) == "第二版招呼语"
    assert load_analysis("an_re")["matches"][0]["greeting"] == "旧招呼"


def test_regenerate_cleans_json_and_quotes(db):
    """模型偶尔包 JSON / 引号，照样收成正文。"""
    from boss_web.services.match_task import _clean_greeting, regenerate_greeting

    _seed_greet_world()
    llm = _ScriptLLM([json.dumps({"greeting": "「您好，想聊聊」"}, ensure_ascii=False)])
    assert regenerate_greeting(analysis_id="an_re", encrypt_job_id="j1", llm=llm) == "您好，想聊聊"
    assert _clean_greeting("```\n纯文本正文\n```") == "纯文本正文"


def test_regenerate_missing_analysis_or_job(db):
    """验证缺 analysis 或职位时报对应错误。"""
    from boss_web.services.match_task import regenerate_greeting

    _seed_greet_world()
    with pytest.raises(FileNotFoundError):
        regenerate_greeting(analysis_id="nope", encrypt_job_id="j1", llm=_ScriptLLM([]))
    with pytest.raises(KeyError):
        regenerate_greeting(analysis_id="an_re", encrypt_job_id="nope", llm=_ScriptLLM([]))


def test_api_greeting_regenerate(db):
    """POST .../greeting/regenerate：回新文案，PATCH 才落库。"""
    from fastapi.testclient import TestClient

    from boss_web import create_app
    from boss_web.services import match_task as mt
    from boss_web.services.llm_config_store import LLMConfig, save_config
    from boss_web.services.resume_store import load_analysis

    _seed_greet_world()
    save_config(LLMConfig(api_key="sk-x", base_url="https://x/v1", model="m"))
    c = TestClient(create_app())

    fake = _ScriptLLM(["API 生成的草稿"])

    class _FakeClient:
        """把 chat 转发到脚本 LLM 的假客户端。"""
        def __init__(self, cfg, **kw):
            """初始化。"""
            pass

        def chat(self, messages, **kw):
            """转发到脚本 LLM。"""
            return fake.chat(messages, **kw)

    orig = mt.LLMClient
    mt.LLMClient = _FakeClient
    try:
        r = c.post("/api/match/an_re/greeting/regenerate", json={"encrypt_job_id": "j1"})
    finally:
        mt.LLMClient = orig

    assert r.status_code == 200
    assert r.json()["greeting"] == "API 生成的草稿"
    assert load_analysis("an_re")["matches"][0]["greeting"] == "旧招呼"

    r = c.patch(
        "/api/match/an_re/greeting",
        json={"encrypt_job_id": "j1", "greeting": "API 生成的草稿"},
    )
    assert r.status_code == 200
    assert load_analysis("an_re")["matches"][0]["greeting"] == "API 生成的草稿"


def test_api_greeting_regenerate_requires_llm_config(db):
    """LLM 没配好 → 422，不给半截草稿。"""
    from fastapi.testclient import TestClient

    from boss_web import create_app

    _seed_greet_world()
    c = TestClient(create_app())
    r = c.post("/api/match/an_re/greeting/regenerate", json={"encrypt_job_id": "j1"})
    assert r.status_code == 422
    assert "LLM" in r.json()["message"]




def _seed_failed_world():
    """一条匹配失败的 match + 一条正常的 match，够 rematch 用。"""
    from boss_jobs.models import Job, PageResult
    from boss_jobs.store import JobStore
    from boss_web.services.resume_store import save_resume

    draft = save_resume("# 张三\nPython 后端", filename="a.md")
    save_llm_parse(
        draft.resume_id, {"parsed_at": 1.0, "model": "m", "data": {"summary": "会 Python"}}
    )
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
        job_desc="负责后端开发",
    )
    with JobStore() as store:
        store.save_page(PageResult(page=1, jobs=(job,), has_more=False, raw_count=1, dropped=()))
    save_analysis(
        {
            "analysis_id": "an_fail",
            "resume_id": draft.resume_id,
            "job_count": 2,
            "top_score": 80.0,
            "matches": [
                {
                    "encrypt_job_id": "j1",
                    "job_name": "Python",
                    "brand_name": "某公司",
                    "security_id": "sid",
                    "lid": "lid1",
                    "match_score": 0,
                    "verdict": "匹配失败",
                    "pros": [],
                    "cons": [],
                    "advice": "",
                    "greeting": "",
                    "error": "LLM 没回合法 JSON",
                    "deliver_status": "sending",
                },
                {
                    "encrypt_job_id": "j2",
                    "job_name": "Java",
                    "match_score": 80,
                    "verdict": "合适",
                    "greeting": "你好",
                    "error": "",
                },
            ],
        }
    )


def _analysis_row_count() -> int:
    """返回 analysis 表行数。"""
    import boss_db

    conn = boss_db.acquire()
    return int(conn.execute("SELECT COUNT(*) AS n FROM analysis").fetchone()["n"])


def test_rematch_overwrites_failed_item_only(db):
    """重跑成功：只改那条 match，analysis 行数不变（只改不增），快照/发送状态复用。"""
    from boss_web.services.match_task import rematch_job
    from boss_web.services.resume_store import load_analysis

    _seed_failed_world()
    llm = _ScriptLLM(
        [
            json.dumps(
                {
                    "match_score": 88,
                    "matched_skills": ["Python"],
                    "missing_skills": ["K8s"],
                    "verdict": "合适",
                    "pros": ["亮点"],
                    "cons": ["短板"],
                    "advice": "补 K8s",
                    "greeting": "您好，想聊聊",
                },
                ensure_ascii=False,
            )
        ]
    )
    item = rematch_job(analysis_id="an_fail", encrypt_job_id="j1", llm=llm)
    assert item["match_score"] == 88
    assert item["error"] == ""
    assert item["greeting"] == "您好，想聊聊"
    assert "负责后端开发" in llm.calls[0][1]["content"]
    assert item["job_name"] == "Python"
    assert item["security_id"] == "sid"
    assert item["lid"] == "lid1"
    assert item["deliver_status"] == "sending"

    payload = load_analysis("an_fail")
    assert len(payload["matches"]) == 2
    assert payload["matches"][0]["match_score"] == 88
    assert payload["matches"][1]["job_name"] == "Java"
    assert payload["matches"][1]["greeting"] == "你好"

    assert _analysis_row_count() == 1
    import boss_db

    row = boss_db.acquire().execute(
        "SELECT top_score FROM analysis WHERE analysis_id = 'an_fail'"
    ).fetchone()
    assert float(row["top_score"]) == 88.0


def test_rematch_failure_keeps_existing_fields(db):
    """重跑又失败：只换 error，已有内容（含手改招呼语）不掀掉。"""
    from boss_web.services.match_task import rematch_job
    from boss_web.services.resume_store import load_analysis, update_greeting

    _seed_failed_world()
    update_greeting("an_fail", "j1", "用户手改的招呼语")

    class _Boom:
        """chat 必炸的假 LLM。"""
        def chat(self, messages, **kw):
            """抛出 RuntimeError。"""
            raise RuntimeError("LLM 超时")

    item = rematch_job(analysis_id="an_fail", encrypt_job_id="j1", llm=_Boom())
    assert "LLM 超时" in item["error"]
    assert item["greeting"] == "用户手改的招呼语"
    assert item["match_score"] == 0

    assert load_analysis("an_fail")["matches"][0]["error"] == "LLM 超时"
    assert _analysis_row_count() == 1


def test_rematch_missing_refs(db):
    """验证重匹配缺引用时报错。"""
    from boss_web.services.match_task import rematch_job

    _seed_failed_world()
    with pytest.raises(FileNotFoundError):
        rematch_job(analysis_id="nope", encrypt_job_id="j1", llm=_ScriptLLM([]))
    with pytest.raises(KeyError):
        rematch_job(analysis_id="an_fail", encrypt_job_id="nope", llm=_ScriptLLM([]))


def test_rematch_patches_running_task_without_insert(db):
    """分析还没落库（在跑的匹配任务）→ 改内存那条，同样一行不新插。"""
    from boss_web.services import match_task as mt
    from boss_web.services.resume_store import save_resume

    _seed_failed_world()
    draft = save_resume("# 张三\nPython 后端", filename="b.md")
    task = mt.MatchTask(draft.resume_id, [])
    task.matches.append(
        {
            "encrypt_job_id": "j1",
            "job_name": "Python",
            "security_id": "sid",
            "match_score": 0,
            "verdict": "匹配失败",
            "greeting": "",
            "error": "boom",
        }
    )
    with mt.match_tasks._lock:
        mt.match_tasks._task = task
    try:
        llm = _ScriptLLM(
            [json.dumps({"match_score": 70, "verdict": "还行", "greeting": "你好"}, ensure_ascii=False)]
        )
        item = mt.rematch_job(analysis_id=task.analysis_id, encrypt_job_id="j1", llm=llm)
    finally:
        with mt.match_tasks._lock:
            mt.match_tasks._task = None

    assert item["match_score"] == 70
    assert item["error"] == ""
    assert task.matches[0]["match_score"] == 70
    assert task.matches[0]["security_id"] == "sid"
    assert _analysis_row_count() == 1


def test_api_rematch(db):
    """POST .../rematch：回改后的那条，库里原地覆盖（不新增）。"""
    from fastapi.testclient import TestClient

    from boss_web import create_app
    from boss_web.services import match_task as mt
    from boss_web.services.llm_config_store import LLMConfig, save_config
    from boss_web.services.resume_store import load_analysis

    _seed_failed_world()
    save_config(LLMConfig(api_key="sk-x", base_url="https://x/v1", model="m"))
    fake = _ScriptLLM(
        [
            json.dumps(
                {"match_score": 77, "verdict": "可以", "greeting": "您好", "pros": ["A"]},
                ensure_ascii=False,
            )
        ]
    )

    class _FakeClient:
        """把 chat 转发到脚本 LLM 的假客户端。"""
        def __init__(self, cfg, **kw):
            """初始化。"""
            pass

        def chat(self, messages, **kw):
            """转发到脚本 LLM。"""
            return fake.chat(messages, **kw)

    orig = mt.LLMClient
    mt.LLMClient = _FakeClient
    try:
        c = TestClient(create_app())
        r = c.post("/api/match/an_fail/rematch", json={"encrypt_job_id": "j1"})
    finally:
        mt.LLMClient = orig

    assert r.status_code == 200
    assert r.json()["item"]["match_score"] == 77
    assert load_analysis("an_fail")["matches"][0]["match_score"] == 77
    assert _analysis_row_count() == 1

    r = c.post("/api/match/an_fail/rematch", json={"encrypt_job_id": "missing"})
    assert r.status_code == 404
    r = c.post("/api/match/nope/rematch", json={"encrypt_job_id": "j1"})
    assert r.status_code == 404


_MATCH_JOB_KWARGS = {
    "job_name": "Python",
    "brand_name": "某公司",
    "location": "广州",
    "salary_desc": "20-30K",
    "job_experience": "3-5年",
    "job_degree": "本科",
    "brand_industry": "互联网",
    "brand_scale_name": "100-499人",
    "encrypt_job_id": "j1",
    "job_desc": "负责后端开发\n要求 Python",
}

_CUSTOM_REQ = "写 80 字，风格 {轻松}，不要『我们』}，可以用 { } 任意"

_MATCH_REPLY = json.dumps(
    {
        "match_score": 85,
        "matched_skills": ["Python"],
        "missing_skills": [],
        "verdict": "合适",
        "pros": ["亮点A"],
        "cons": ["短板B"],
        "advice": "补 K8s",
        "greeting": "你好",
    },
    ensure_ascii=False,
)


def test_招呼语约束里的花括号不炸匹配提示词(db):
    """用户写的 `{}` 走运行期注入，原样进提示词，不许把 `.format()` 炸掉。"""
    from boss_jobs.models import Job
    from boss_web.services import app_settings_store
    from boss_web.services.match_task import _match_one

    app_settings_store.apply_greeting_requirement(_CUSTOM_REQ)
    llm = _FakeLLM([_MATCH_REPLY])
    result = _match_one(llm, "简历摘要", Job(**_MATCH_JOB_KWARGS))

    assert result["match_score"] == 85
    prompt = llm.messages[0][1]["content"]
    assert _CUSTOM_REQ in prompt
    assert "100 到 150 个字" not in prompt, "自定义要顶掉内置默认"


def test_招呼语约束里的花括号不炸重生成提示词(db):
    """重生成路径同样原样注入用户约束，含 `{}` 也不炸。"""
    from boss_web.services import app_settings_store
    from boss_web.services.match_task import regenerate_greeting

    _seed_greet_world()
    app_settings_store.apply_greeting_requirement(_CUSTOM_REQ)

    llm = _ScriptLLM(["您好，想聊聊"])
    assert regenerate_greeting(analysis_id="an_re", encrypt_job_id="j1", llm=llm)
    prompt = llm.calls[0][1]["content"]
    assert _CUSTOM_REQ in prompt
    assert "100 到 150 个字" not in prompt, "自定义要顶掉内置默认"


def test_自定义招呼语约束两条路径都立即生效(db):
    """保存后下次组装就用新值——匹配与重生成都现读设置，不用重启。"""
    from boss_jobs.models import Job
    from boss_web.services import app_settings_store
    from boss_web.services.greeting_prompt import GREETING_REQUIREMENT
    from boss_web.services.match_task import _match_one, regenerate_greeting

    job = Job(**_MATCH_JOB_KWARGS)

    llm = _FakeLLM([_MATCH_REPLY])
    _match_one(llm, "简历摘要", job)
    assert GREETING_REQUIREMENT in llm.messages[0][1]["content"]

    app_settings_store.apply_greeting_requirement("改后的约束：60 字以内")

    llm = _FakeLLM([_MATCH_REPLY])
    _match_one(llm, "简历摘要", job)
    assert "改后的约束：60 字以内" in llm.messages[0][1]["content"]
    assert GREETING_REQUIREMENT not in llm.messages[0][1]["content"]

    _seed_greet_world()
    greeter = _ScriptLLM(["第二版"])
    regenerate_greeting(analysis_id="an_re", encrypt_job_id="j1", llm=greeter)
    assert "改后的约束：60 字以内" in greeter.calls[0][1]["content"]


def test_简历解析路径也读自定义招呼语约束(db):
    """旧的 `/api/resume/analyze` 与匹配任务共用同一段约束。"""
    from boss_jobs.models import Job
    from boss_web.services import app_settings_store
    from boss_web.services.resume_analyzer import _analyze_one
    from boss_web.services.resume_store import ResumeDraft

    app_settings_store.apply_greeting_requirement(_CUSTOM_REQ)
    draft = ResumeDraft("rs_x", "简历", "", 1.0, "原文")
    llm = _FakeLLM([_MATCH_REPLY])

    _analyze_one(llm, draft, Job(**_MATCH_JOB_KWARGS))

    prompt = llm.messages[0][1]["content"]
    assert _CUSTOM_REQ in prompt
