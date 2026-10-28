"""面试题生成（interview_questions / 出题 / API）的单元测试。"""

from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from boss_jobs.models import Job, PageResult
from boss_jobs.store import JobStore
from boss_web.app import create_app
from boss_web.services import interview_generator as IG
from boss_web.services import interview_generation_task as IGT

SAMPLE = """# 张三

## 基本信息
- 姓名：张三
- 电话：13800138000

## 工作经历
### 示例科技 · 后端工程师 · 2020-2024
- 负责订单系统

## 技能标签
- Python、FastAPI、Redis
"""

QUESTION_PAYLOAD = {
    "questions": [
        {
            "question_text": "讲讲你做过的高并发订单系统？",
            "question_type": "项目经历",
            "difficulty": "中级",
            "is_frequent": True,
            "tags": ["项目经验"],
            "answer_hints": {
                "approach": "按 STAR 讲清背景与结果",
                "key_points": ["限流", "幂等"],
                "reference_answer": "先说场景规模，再说方案权衡",
            },
        },
        {
            "question_text": "Redis 缓存和数据库怎么保证一致？",
            "question_type": "技术深度",
            "difficulty": "高级",
            "is_frequent": False,
            "answer_hints": {
                "approach": "先说常见方案",
                "key_points": ["延时双删"],
                "reference_answer": "缓存失效策略",
            },
        },
    ]
}


def make_job(jid: str = "j1", *, job_desc: str = "负责订单系统，要求 Python", **kw) -> Job:
    """构造一条职位。"""
    defaults = dict(
        job_name="Python 开发",
        brand_name="示例科技",
        location="广州",
        salary_desc="15-25K",
        job_experience="3-5年",
        job_degree="本科",
        brand_industry="互联网",
        brand_scale_name="100-499人",
        encrypt_job_id=jid,
        job_desc=job_desc,
        skills=("Python", "FastAPI"),
        job_labels=("后端",),
        raw_json="{}",
    )
    defaults.update(kw)
    return Job(**defaults)


@pytest.fixture
def client():
    """提供隔离库下的 TestClient。"""
    return TestClient(create_app())


def _seed_analysis(job_ids: list[str] | None = None) -> str:
    """落一份简历 + 分析 + 岗位，返回 analysis_id。"""
    import boss_db

    conn = boss_db.acquire()
    now = time.time()
    conn.execute(
        "INSERT INTO resume (resume_id, title, source_name, created_at, content_md, meta) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("rs_1", "张三", "zhang.md", now, SAMPLE, "{}"),
    )
    ids = job_ids or ["j1"]
    matches = [
        {
            "encrypt_job_id": jid,
            "job_name": f"岗位{jid}",
            "brand_name": "示例科技",
            "job_experience": "3-5年",
            "job_degree": "本科",
            "match_score": 82,
            "pros": ["经验对口"],
            "cons": ["缺 K8s"],
        }
        for jid in ids
    ]
    payload = {
        "analysis_id": "an_1",
        "resume_id": "rs_1",
        "resume_title": "张三",
        "created_at": now,
        "status": "done",
        "matches": matches,
    }
    conn.execute(
        "INSERT INTO analysis (analysis_id, resume_id, resume_title, created_at, "
        "status, model, base_url, job_count, top_score, payload) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("an_1", "rs_1", "张三", now, "done", "m1", "https://x/v1", len(matches), 82.0,
         json.dumps(payload, ensure_ascii=False)),
    )
    conn.commit()
    with JobStore() as store:
        store.save_page(
            PageResult(
                page=1,
                jobs=tuple(make_job(jid, job_name=f"岗位{jid}") for jid in ids),
                raw_count=len(ids),
                has_more=False,
            )
        )
    return "an_1"


class FakeLLM:
    """回固定题目 JSON 的假 LLM 客户端。"""

    def __init__(self, payload: dict | None = None) -> None:
        """初始化。"""
        self.payload = payload if payload is not None else QUESTION_PAYLOAD
        self.calls: list[list[dict]] = []

    def chat(self, messages, **kw):
        """记下调用并回固定 JSON。"""
        self.calls.append(list(messages))
        return json.dumps(self.payload, ensure_ascii=False)


def test_question_count_for_score():
    """匹配分越高题量越大，且落在配置区间内。"""
    assert IG.question_count_for(95) == 15
    assert IG.question_count_for(70) == 12
    assert IG.question_count_for(10) == 10
    assert IG.question_count_for(None) == 10


def test_generate_questions_normalizes():
    """假 LLM：题型 / 难度 / 提示词被收成定长字段。"""
    qs = IG.generate_questions(
        job_data={"job_name": "Python 开发", "brand_name": "示例科技", "job_desc": "负责订单系统"},
        resume_content=SAMPLE,
        match_result={"match_score": 82, "pros": ["经验对口"], "cons": ["缺 K8s"]},
        llm=FakeLLM(),
    )
    assert len(qs) == 2
    assert qs[0].question_type == "项目经历"
    assert qs[0].is_frequent is True
    assert qs[0].answer_hints["key_points"] == ["限流", "幂等"]
    assert qs[0].answer_hints["tags"] == ["项目经验"]


def test_generate_questions_drops_bad_type():
    """题型 / 难度不合法的题被丢掉。"""
    payload = {
        "questions": [
            {"question_text": "好题", "question_type": "技术深度", "difficulty": "基础"},
            {"question_text": "坏题", "question_type": "脑筋急转弯", "difficulty": "基础"},
            {"question_text": "坏难度", "question_type": "场景", "difficulty": "地狱"},
        ]
    }
    qs = IG.generate_questions(
        job_data={"job_name": "X", "brand_name": "Y", "job_desc": "JD"},
        resume_content=SAMPLE,
        match_result={},
        llm=FakeLLM(payload),
    )
    assert [q.question_text for q in qs] == ["好题"]


def test_generate_questions_requires_jd():
    """没有 JD 直接报错，不打 LLM。"""
    llm = FakeLLM()
    with pytest.raises(ValueError):
        IG.generate_questions(
            job_data={"job_name": "X", "brand_name": "Y", "job_desc": "  "},
            resume_content=SAMPLE,
            match_result={},
            llm=llm,
        )
    assert llm.calls == []


def test_filter_new_questions_dedupes():
    """补题去重：完全相同 / 换皮（归一后相同 / 互相包含）都算重复。"""
    existing = ["Redis 缓存和数据库怎么保证一致？", "讲讲你做过的高并发订单系统"]
    kept = IG.filter_new_questions(
        [
            IG.InterviewQuestion("Redis 缓存和数据库怎么保证一致？", "技术深度", "高级", False, {}),
            IG.InterviewQuestion("redis缓存和数据库怎么保证一致", "技术深度", "高级", False, {}),
            IG.InterviewQuestion("Redis 缓存与数据库如何保证一致性？", "技术深度", "高级", False, {}),
            IG.InterviewQuestion("线上 CPU 飙高怎么排查？", "场景", "中级", False, {}),
        ],
        existing=existing,
    )
    assert [q.question_text for q in kept] == ["线上 CPU 飙高怎么排查？"]


def test_generate_questions_supplement_type_and_dedup():
    """补题：只出指定题型、避开已有题；提示词带上已有题和型别约束。"""
    payload = {
        "questions": [
            {"question_text": "怎么设计一个秒杀系统？", "question_type": "场景", "difficulty": "高级"},
            {"question_text": "线程池核心参数怎么配？", "question_type": "技术深度", "difficulty": "中级"},
            {"question_text": "Redis 缓存和数据库怎么保证一致？", "question_type": "技术深度", "difficulty": "高级"},
            {"question_text": "遇到线上事故你怎么定级？", "question_type": "场景", "difficulty": "中级"},
        ]
    }
    qs = IG.generate_questions(
        job_data={"job_name": "X", "brand_name": "Y", "job_desc": "JD"},
        resume_content=SAMPLE,
        match_result={},
        question_count=5,
        question_type="场景",
        existing_questions=["怎么设计一个秒杀系统"],
        llm=FakeLLM(payload),
    )
    assert [q.question_text for q in qs] == ["遇到线上事故你怎么定级？"]
    llm = FakeLLM(payload)
    IG.generate_questions(
        job_data={"job_name": "X", "brand_name": "Y", "job_desc": "JD"},
        resume_content=SAMPLE,
        match_result={},
        question_count=3,
        question_type="场景",
        existing_questions=["旧题A", "旧题B"],
        llm=llm,
    )
    prompt = llm.calls[0][1]["content"]
    assert "补充出题要求" in prompt
    assert "只出「场景」" in prompt
    assert "旧题A" in prompt and "旧题B" in prompt


def test_generate_questions_supplement_caps_count():
    """补题条数被夹在 1~SUPPLEMENT_MAX。"""
    payload = {
        "questions": [
            {"question_text": f"补题{i}", "question_type": "软技能", "difficulty": "基础"}
            for i in range(20)
        ]
    }
    qs = IG.generate_questions(
        job_data={"job_name": "X", "brand_name": "Y", "job_desc": "JD"},
        resume_content=SAMPLE,
        match_result={},
        question_count=99,
        question_type="软技能",
        llm=FakeLLM(payload),
    )
    assert len(qs) == IG.SUPPLEMENT_MAX == 10


def test_save_questions_append_mode():
    """补题写库是追加，不删旧题。"""
    _seed_analysis()
    IGT.save_questions(
        analysis_id="an_1",
        encrypt_job_id="j1",
        questions=[IG.InterviewQuestion("旧题", "场景", "基础", False, {})],
    )
    assert (
        IGT.save_questions(
            analysis_id="an_1",
            encrypt_job_id="j1",
            questions=[IG.InterviewQuestion("补题", "技术深度", "中级", False, {})],
            replace=False,
        )
        == 1
    )
    texts = IGT.load_existing_questions("j1")
    assert set(texts) == {"旧题", "补题"}


def test_save_questions_replaces_in_place():
    """重跑生成 = 原地替换旧题（只改不增），换分析再出也只留一套。"""
    _seed_analysis()
    qs = [
        IG.InterviewQuestion("旧题", "场景", "基础", False, {"approach": "a"}),
    ]
    assert IGT.save_questions(analysis_id="an_1", encrypt_job_id="j1", questions=qs) == 1
    qs2 = [
        IG.InterviewQuestion("新题1", "场景", "基础", False, {}),
        IG.InterviewQuestion("新题2", "场景", "中级", True, {}),
    ]
    assert IGT.save_questions(analysis_id="an_1", encrypt_job_id="j1", questions=qs2) == 2

    import boss_db

    rows = boss_db.acquire().execute(
        "SELECT question_text FROM interview_questions WHERE analysis_id = 'an_1'"
    ).fetchall()
    assert sorted(str(r["question_text"]) for r in rows) == ["新题1", "新题2"]

    qs3 = [IG.InterviewQuestion("终版", "场景", "基础", False, {})]
    assert IGT.save_questions(analysis_id="", encrypt_job_id="j1", questions=qs3) == 1
    rows = boss_db.acquire().execute(
        "SELECT analysis_id, question_text FROM interview_questions WHERE encrypt_job_id = 'j1'"
    ).fetchall()
    assert len(rows) == 1
    assert str(rows[0]["question_text"]) == "终版"


def test_api_context_and_summary(client):
    """context 找得到分析；summary 回本页徽章数。"""
    _seed_analysis()
    r = client.get("/api/interview/context?encrypt_job_id=j1")
    assert r.status_code == 200
    body = r.json()
    assert body["analysis_id"] == "an_1"
    assert body["job_name"] == "岗位j1"
    assert body["question_count"] == 0

    r = client.get("/api/interview/summary?encrypt_job_ids=j1,j2")
    items = r.json()["items"]
    assert items["j1"]["question_count"] == 0
    assert items["j2"]["question_count"] == 0


def test_api_list_patch_export(client):
    """列表筛选 → 编辑 → 导出 markdown/json。"""
    _seed_analysis()
    qs = [
        IG.InterviewQuestion(
            "讲讲项目", "项目经历", "中级", True,
            {"approach": "STAR", "key_points": ["限流"], "reference_answer": "脉络"},
        ),
        IG.InterviewQuestion("算法题", "技术深度", "基础", False, {}),
    ]
    IGT.save_questions(analysis_id="an_1", encrypt_job_id="j1", questions=qs)

    r = client.get("/api/interview/list?analysis_id=an_1&encrypt_job_id=j1")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 2
    assert data["stats"]["question_count"] == 2

    r = client.get("/api/interview/list?analysis_id=an_1&question_type=技术深度")
    assert r.json()["total"] == 1

    qid = next(q["id"] for q in data["questions"] if q["question_text"] == "讲讲项目")
    r = client.patch(
        f"/api/interview/{qid}",
        json={
            "question_text": "改过的题",
            "answer_hints": {
                "approach": "新思路",
                "key_points": [],
                "reference_answer": "一份能直接用的参考回答",
            },
        },
    )
    assert r.json()["item"]["question_text"] == "改过的题"

    r = client.get("/api/interview/list?analysis_id=an_1&q=改过的题")
    assert r.json()["total"] == 1

    r = client.get("/api/interview/export?analysis_id=an_1&format=markdown")
    assert r.status_code == 200
    text = r.text
    assert "改过的题" in text
    assert "答题思路" in text
    assert "⭐ 高频" in text
    assert "参考答案" in text
    assert "参考答案大纲" not in text

    r = client.get("/api/interview/export?analysis_id=an_1&format=json")
    rows = r.json()
    assert len(rows) == 2
    assert rows[0]["question_text"] in {"改过的题", "算法题"}


def test_reference_answer_legacy_key_compat(client):
    """旧字段名 reference_outline 出门被收成 reference_answer。"""
    _seed_analysis()
    qs = [
        IG.InterviewQuestion("旧题", "场景", "基础", False, {}),
    ]
    IGT.save_questions(analysis_id="an_1", encrypt_job_id="j1", questions=qs)
    import boss_db

    conn = boss_db.acquire()
    conn.execute(
        "UPDATE interview_questions SET answer_hints = ? WHERE analysis_id = 'an_1'",
        (json.dumps({"approach": "旧", "reference_outline": "旧大纲"}, ensure_ascii=False),),
    )
    conn.commit()
    r = client.get("/api/interview/list?analysis_id=an_1")
    hints = r.json()["questions"][0]["answer_hints"]
    assert hints["reference_answer"] == "旧大纲"
    assert "reference_outline" not in hints


def test_api_generate_task_flow(client, monkeypatch):
    """generate → 任务跑完（假出题）→ 列表能看到题。"""
    _seed_analysis()
    from boss_web.services.llm_config_store import LLMConfig, save_config

    save_config(LLMConfig(api_key="sk-1", base_url="https://x/v1", model="m1"))

    def _fake_generate(**kw):
        """替掉真出题：回两道固定题。"""
        return [
            IG.InterviewQuestion("题A", "场景", "基础", False, {}),
            IG.InterviewQuestion("题B", "软技能", "中级", True, {}),
        ]

    monkeypatch.setattr(IGT.IG, "generate_questions", _fake_generate)

    r = client.post("/api/interview/generate", json={"analysis_id": "an_1", "encrypt_job_id": "j1"})
    assert r.status_code == 200
    task_id = r.json()["task_id"]
    assert task_id.startswith("ig_")

    r = client.get(f"/api/interview/task/{task_id}")
    assert r.status_code == 200

    task = IGT.interview_tasks.current()
    assert task is not None
    for _ in range(50):
        if task.status != IGT.STATUS_RUNNING:
            break
        time.sleep(0.05)
    assert task.status == IGT.STATUS_DONE
    assert task.question_total == 2

    r = client.get("/api/interview/list?analysis_id=an_1&encrypt_job_id=j1")
    assert r.json()["stats"]["question_count"] == 2


def test_api_generate_unknown_analysis(client):
    """分析不存在 → 404。"""
    r = client.post("/api/interview/generate", json={"analysis_id": "an_nope"})
    assert r.status_code == 404


def test_api_generation_jobs_filter(client):
    """出题页岗位列表：抓取记录筛选 + 职位名模糊匹配 + 出题状态。"""
    _seed_analysis()
    import boss_db

    conn = boss_db.acquire()
    now = time.time()
    conn.execute(
        "INSERT INTO crawl_tasks (task_id, source, status, created_at, started_at, job_count) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("ct_1", "boss", "done", now, now, 2),
    )
    conn.execute(
        "INSERT INTO crawl_task_jobs (task_id, encrypt_job_id, action) VALUES (?, ?, ?)",
        ("ct_1", "j1", "insert"),
    )
    conn.commit()
    with JobStore() as store:
        store.save_page(
            PageResult(
                page=1,
                jobs=(make_job("j2", job_name="Java 后端"),),
                raw_count=1,
                has_more=False,
            )
        )

    r = client.get("/api/interview/jobs")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 2
    assert {it["encrypt_job_id"] for it in data["items"]} == {"j1", "j2"}
    j1 = next(it for it in data["items"] if it["encrypt_job_id"] == "j1")
    assert j1["job_name"] == "岗位j1"
    assert j1["question_count"] == 0

    r = client.get("/api/interview/jobs?crawl_task_id=ct_1")
    data = r.json()
    assert data["total"] == 1
    assert data["items"][0]["encrypt_job_id"] == "j1"

    r = client.get("/api/interview/jobs?title=Java")
    data = r.json()
    assert data["total"] == 1
    assert data["items"][0]["encrypt_job_id"] == "j2"

    r = client.get("/api/interview/jobs?title=没有这个职位")
    assert r.json()["total"] == 0

    IGT.save_questions(
        analysis_id="an_1",
        encrypt_job_id="j1",
        questions=[IG.InterviewQuestion("题", "场景", "基础", True, {})],
    )
    r = client.get("/api/interview/jobs?crawl_task_id=ct_1")
    assert r.json()["items"][0]["question_count"] == 1


def test_api_generation_records(client):
    """生成记录：一岗一行，带题数 / 时间。"""
    _seed_analysis()
    IGT.save_questions(
        analysis_id="an_1",
        encrypt_job_id="j1",
        questions=[
            IG.InterviewQuestion("题1", "场景", "基础", True, {}),
            IG.InterviewQuestion("题2", "技术深度", "高级", False, {}),
        ],
    )

    r = client.get("/api/interview/records")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 1
    item = data["items"][0]
    assert item["encrypt_job_id"] == "j1"
    assert item["analysis_id"] == "an_1"
    assert item["job_name"] == "岗位j1"
    assert item["question_count"] == 2
    assert item["created_at"] <= item["updated_at"]

    r = client.get("/api/interview/records?title=岗位j1")
    assert r.json()["total"] == 1
    r = client.get("/api/interview/records?title=别的")
    assert r.json()["total"] == 0


def test_api_generate_batch_without_analysis(client, monkeypatch):
    """批量出题：有分析的带上 match，没分析的按裸 JD + 简历出；重跑原地替换。"""
    _seed_analysis(job_ids=["j1", "j2"])
    from boss_web.services.llm_config_store import LLMConfig, save_config

    save_config(LLMConfig(api_key="sk-1", base_url="https://x/v1", model="m1"))

    calls: list[dict] = []

    def _fake_generate(**kw):
        """替掉真出题，记下用的 JD。"""
        calls.append(kw)
        return [IG.InterviewQuestion("批量题", "场景", "基础", False, {})]

    monkeypatch.setattr(IGT.IG, "generate_questions", _fake_generate)

    with JobStore() as store:
        store.save_page(
            PageResult(
                page=1,
                jobs=(make_job("j3", job_name="Go 开发"),),
                raw_count=1,
                has_more=False,
            )
        )

    r = client.post(
        "/api/interview/generate",
        json={"encrypt_job_ids": ["j1", "j3"], "resume_id": "rs_1"},
    )
    assert r.status_code == 200
    task = IGT.interview_tasks.current()
    assert task is not None
    for _ in range(50):
        if task.status != IGT.STATUS_RUNNING:
            break
        time.sleep(0.05)
    assert task.status == IGT.STATUS_DONE
    assert task.question_total == 2

    by_name = {c["job_data"]["job_name"]: c for c in calls}
    assert "岗位j1" in by_name
    assert "Go 开发" in by_name
    assert by_name["岗位j1"]["match_result"].get("match_score") == 82
    assert by_name["Go 开发"]["match_result"] == {}

    r = client.get("/api/interview/records")
    assert r.json()["total"] == 2

    r = client.post("/api/interview/generate", json={"encrypt_job_ids": ["j3"], "resume_id": "rs_1"})
    assert r.status_code == 200
    task = IGT.interview_tasks.current()
    for _ in range(50):
        if task.status != IGT.STATUS_RUNNING:
            break
        time.sleep(0.05)
    assert task.status == IGT.STATUS_DONE
    r = client.get("/api/interview/records?title=Go")
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["question_count"] == 1


def test_api_generate_batch_requires_jobs(client):
    """既没分析也没选岗位 → 422。"""
    r = client.post("/api/interview/generate", json={})
    assert r.status_code == 422


def test_api_supplement_type_and_limit(client, monkeypatch):
    """补题：只补指定题型、追加不覆盖、避开已有题；条数最多 10。"""
    _seed_analysis()
    from boss_web.services.llm_config_store import LLMConfig, save_config

    save_config(LLMConfig(api_key="sk-1", base_url="https://x/v1", model="m1"))

    IGT.save_questions(
        analysis_id="an_1",
        encrypt_job_id="j1",
        questions=[IG.InterviewQuestion("旧技术题", "技术深度", "中级", False, {})],
    )

    seen: dict = {}

    def _fake_generate(**kw):
        """记下补题参数；真管道里型别筛选和去重已在 generate_questions 内做掉。"""
        seen.update(kw)
        return [IG.InterviewQuestion("新技术题A", "技术深度", "中级", False, {})]

    monkeypatch.setattr(IGT.IG, "generate_questions", _fake_generate)

    r = client.post(
        "/api/interview/generate",
        json={"encrypt_job_ids": ["j1"], "question_type": "技术深度", "question_count": 11},
    )
    assert r.status_code == 422

    r = client.post(
        "/api/interview/generate",
        json={"encrypt_job_ids": ["j1"], "question_type": "脑筋急转弯", "question_count": 3},
    )
    assert r.status_code == 422

    r = client.post(
        "/api/interview/generate",
        json={"encrypt_job_ids": ["j1"], "question_type": "技术深度", "question_count": 3},
    )
    assert r.status_code == 200
    task = IGT.interview_tasks.current()
    for _ in range(50):
        if task.status != IGT.STATUS_RUNNING:
            break
        time.sleep(0.05)
    assert task.status == IGT.STATUS_DONE
    assert seen["question_type"] == "技术深度"
    assert seen["question_count"] == 3
    assert seen["existing_questions"] == ["旧技术题"]

    r = client.get("/api/interview/list?encrypt_job_id=j1")
    texts = {q["question_text"] for q in r.json()["questions"]}
    assert texts == {"旧技术题", "新技术题A"}



def test_parallel_tasks_and_no_dup_submit(client, monkeypatch):
    """可并行多个任务；同批岗位重提幂等，含在跑岗位的新批次 409。"""
    _seed_analysis(job_ids=["j1", "j2"])
    with JobStore() as store:
        store.save_page(
            PageResult(
                page=1,
                jobs=(make_job("j3", job_name="Go 开发"), make_job("j4", job_name="Java 开发")),
                raw_count=2,
                has_more=False,
            )
        )
    from boss_web.services.llm_config_store import LLMConfig, save_config

    save_config(LLMConfig(api_key="sk-1", base_url="https://x/v1", model="m1"))

    release = threading.Event()

    def _slow_generate(**kw):
        """卡住不返回，让任务一直 running。"""
        release.wait(timeout=5)
        return [IG.InterviewQuestion("题", "场景", "基础", False, {})]

    monkeypatch.setattr(IGT.IG, "generate_questions", _slow_generate)

    r = client.post("/api/interview/generate", json={"encrypt_job_ids": ["j1"], "resume_id": "rs_1"})
    assert r.status_code == 200
    t1 = r.json()["task_id"]

    r = client.post("/api/interview/generate", json={"encrypt_job_ids": ["j3"], "resume_id": "rs_1"})
    assert r.status_code == 200
    t2 = r.json()["task_id"]
    assert t1 != t2

    r = client.get("/api/interview/tasks")
    data = r.json()
    assert data["running_count"] == 2
    assert {t["task_id"] for t in data["tasks"]} >= {t1, t2}

    r = client.post("/api/interview/generate", json={"encrypt_job_ids": ["j1"], "resume_id": "rs_1"})
    assert r.status_code == 200
    assert r.json()["task_id"] == t1

    r = client.post("/api/interview/generate", json={"encrypt_job_ids": ["j1", "j4"], "resume_id": "rs_1"})
    assert r.status_code == 409

    release.set()
    for tid in (t1, t2):
        task = IGT.interview_tasks.get(tid)
        for _ in range(50):
            if task.status != IGT.STATUS_RUNNING:
                break
            time.sleep(0.05)
        assert task.status == IGT.STATUS_DONE

    r = client.get("/api/interview/tasks")
    assert r.json()["running_count"] == 0


def test_task_center_delete_retry(client, monkeypatch):
    """失败任务保留展示，可删除 / 重试；单岗失败不拖垮，占用口径只算 running。

    占位任务的岗位集要和提交的不同（同集会走幂等返回原任务）。
    """
    _seed_analysis(job_ids=["j1", "j2"])
    from boss_web.services.llm_config_store import LLMConfig, save_config

    save_config(LLMConfig(api_key="sk-1", base_url="https://x/v1", model="m1"))

    boom = {"on": True}

    def _flaky_generate(**kw):
        """第一次全炸，重试时正常。"""
        if boom["on"]:
            raise RuntimeError("AI 挂了")
        return [IG.InterviewQuestion("题", "场景", "基础", False, {})]

    monkeypatch.setattr(IGT.IG, "generate_questions", _flaky_generate)

    r = client.post("/api/interview/generate", json={"analysis_id": "an_1", "encrypt_job_id": "j1"})
    assert r.status_code == 200
    tid = r.json()["task_id"]
    task = IGT.interview_tasks.get(tid)
    for _ in range(50):
        if task.status != IGT.STATUS_RUNNING:
            break
        time.sleep(0.05)
    assert task.status == IGT.STATUS_DONE

    task.status = IGT.STATUS_ERROR
    task.error = "模拟失败"

    r = client.get("/api/interview/tasks")
    data = r.json()
    failed = [t for t in data["tasks"] if t["status"] == "error"]
    assert failed and failed[0]["task_id"] == tid
    assert "j1" not in data["busy_job_ids"]

    t3 = IGT.InterviewGenerationTask(analysis_id="", job_ids=["j1", "jx"], resume_id="rs_1")
    IGT.interview_tasks._tasks[t3.task_id] = t3
    r = client.post("/api/interview/generate", json={"encrypt_job_ids": ["j1"], "resume_id": "rs_1"})
    assert r.status_code == 409
    t3.status = IGT.STATUS_CANCELLED

    boom["on"] = False
    r = client.post(f"/api/interview/task/{tid}/retry")
    assert r.status_code == 200
    new_tid = r.json()["task_id"]
    assert new_tid != tid
    new_task = IGT.interview_tasks.get(new_tid)
    for _ in range(50):
        if new_task.status != IGT.STATUS_RUNNING:
            break
        time.sleep(0.05)
    assert new_task.status == IGT.STATUS_DONE

    r = client.get("/api/interview/tasks")
    ids = [t["task_id"] for t in r.json()["tasks"]]
    assert tid not in ids

    t2 = IGT.InterviewGenerationTask(analysis_id="", job_ids=["j9"], resume_id="rs_1")
    t2.status = IGT.STATUS_ERROR
    IGT.interview_tasks._tasks[t2.task_id] = t2
    r = client.delete(f"/api/interview/task/{t2.task_id}")
    assert r.status_code == 200
    r = client.get("/api/interview/tasks")
    assert t2.task_id not in [t["task_id"] for t in r.json()["tasks"]]
