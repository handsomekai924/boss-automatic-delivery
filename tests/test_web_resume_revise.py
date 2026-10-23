"""简历优化（resume_revisions / revise / 生成 / 保存）的单元测试。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from boss_jobs.models import Job, PageResult
from boss_jobs.store import JobStore
from boss_web.app import create_app
from boss_web.services import resume_revision as RR


def make_job(jid: str = "id-1", *, job_desc: str = "职责 A", **kw) -> Job:
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


SAMPLE = """# 张三

## 基本信息
- 姓名：张三
- 电话：13800138000
- 邮箱：zhangsan@example.com

## 求职意向
目标岗位：Python 后端

## 工作经历
### 示例科技 · 后端工程师 · 2020-2024
- 负责订单系统

## 技能标签
- Python、FastAPI、Redis
"""


@pytest.fixture
def client():
    """提供隔离库下的 TestClient。"""
    return TestClient(create_app())


def _seed(client):
    """上传一份简历 + 存 2 个岗位，返回 (resume_id, job_ids)。"""
    r = client.post(
        "/api/resume/upload",
        files={"file": ("resume.md", SAMPLE.encode("utf-8"), "text/markdown")},
    )
    rid = r.json()["resume_id"]
    with JobStore() as store:
        store.save_page(
            PageResult(page=1, jobs=(make_job("a"), make_job("b")), raw_count=2, has_more=False)
        )
    return rid, ["a", "b"]


def test_assemble_target_block_small():
    """1–5 岗：每岗完整 JD。"""
    jobs = [make_job("a", job_desc="JD A"), make_job("b", job_desc="JD B")]
    block, snap = RR.assemble_target_block(jobs)
    assert "JD A" in block
    assert "JD B" in block
    assert snap["count"] == 2
    assert len(snap["items"]) == 2


def test_assemble_target_block_big_aggregates():
    """>5 岗：聚合画像 + 代表性 5 条。"""
    jobs = [make_job(f"j{i}", job_desc=f"JD {i} " * 30) for i in range(8)]
    block, snap = RR.assemble_target_block(jobs)
    assert "目标岗位共 8 个" in block
    assert "全部已选岗位一览" in block
    assert "代表5" in block
    assert snap["count"] == 8
    assert "Python" in block


def test_generate_revision_ok():
    """假 LLM：生成出修改稿 + changes。"""
    import boss_db

    boss_db.acquire().execute(
        "INSERT INTO resume (resume_id, title, source_name, created_at, content_md, meta) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("rs_x", "x.md", "x.md", 1.0, SAMPLE, "{}"),
    )

    class FakeLLM:
        """回固定修改稿 payload 的假 LLM 客户端。"""

        config = type("C", (), {"model": "m1"})()

        def chat_with_metadata(self, messages, **kw):
            """回带 changes 的简历优化 JSON。"""
            payload = {
                "revised_md": SAMPLE + "\n## 新增说明\n对齐高并发要求",
                "changes": [
                    {"section": "技能标签", "type": "keyword", "summary": "补高并发", "reason": "对齐 JD"}
                ],
            }
            from boss_web.services.llm_client import ChatResult
            return ChatResult(json.dumps(payload, ensure_ascii=False), None)

    res = RR.generate_revision(resume_id="rs_x", jobs=[make_job("a")], llm=FakeLLM())
    assert "revised_md" in res
    assert res["changes"][0]["type"] == "keyword"
    assert res["original_md"].startswith("# 张三")


def test_generate_revision_rejects_hallucination_empty():
    """空修改稿 → 报错。"""
    import boss_db

    class FakeLLM:
        """回空修改稿、用来触发幻觉校验的假 LLM。"""

        config = type("C", (), {"model": "m1"})()

        def chat_with_metadata(self, messages, **kw):
            """回 `revised_md` 为空串的 JSON。"""
            from boss_web.services.llm_client import ChatResult
            return ChatResult(json.dumps({"revised_md": ""}, ensure_ascii=False), None)

    boss_db.acquire().execute(
        "INSERT INTO resume (resume_id, title, source_name, created_at, content_md, meta) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("rs_1", "x.md", "x.md", 1.0, SAMPLE, "{}"),
    )
    with pytest.raises(ValueError):
        RR.generate_revision(resume_id="rs_1", jobs=[make_job("a")], llm=FakeLLM())


def test_markdown_to_text():
    """去 Markdown 标题 / 列表符号。"""
    md = "# 标题\n\n## 子标题\n- 项一\n1. 项二\n正文"
    txt = RR.markdown_to_text(md)
    assert txt.startswith("标题")
    assert "项一" in txt
    assert "项二" in txt
    assert "#" not in txt
    assert "- 项一" not in txt


def test_build_diff_detects_change():
    """difflib 对改动行标记 replace。"""
    diff = RR.build_diff("# A\n## 技能\nPython\n", "# A\n## 技能\nPython、FastAPI\n")
    tags = {d["tag"] for d in diff}
    assert "replace" in tags or "insert" in tags


def test_api_revise_start_and_save(client, monkeypatch):
    """走通 start → 生成（假 LLM）→ save 写盘 + 入库。"""
    rid, job_ids = _seed(client)

    from boss_web.services.llm_config_store import LLMConfig, save_config

    save_config(LLMConfig(api_key="sk-1", base_url="https://x/v1", model="m1"))

    import boss_web.services.resume_revision_task as RRT

    captured = {}

    def _fake_generate(**kw):
        """替掉真生成：记下调用参数并回一份固定修改稿。"""
        captured.update(kw)
        return {
            "revised_md": SAMPLE + "\n## 补充\n对齐高并发",
            "changes": [{"section": "技能标签", "type": "keyword", "summary": "补高并发", "reason": "of JD"}],
            "original_md": SAMPLE,
            "resume_title": "resume.md",
        }

    import time as _t

    monkeypatch.setattr(RRT.RR, "generate_revision", _fake_generate)
    monkeypatch.setattr(RRT, "_model_name", lambda: "m1")

    r = client.post("/api/resume/revise/start", json={"resume_id": rid, "job_ids": job_ids})
    assert r.status_code == 200
    task_id = r.json()["task_id"]
    assert task_id.startswith("rv_")

    for _ in range(50):
        s = client.get("/api/resume/revise/status").json()
        if s.get("done"):
            break
        _t.sleep(0.05)
    assert s.get("done"), s

    detail = client.get(f"/api/resume/revisions/{task_id}").json()
    assert detail["status"] == "draft"
    assert "补充" in detail["revised_md"]
    assert detail["diff"]

    r = client.post(f"/api/resume/revisions/{task_id}/save", json={"format": "md"})
    assert r.status_code == 200
    data = r.json()
    assert data["saved_resume_id"].startswith("rs_")
    assert data["saved_path"].endswith(".md")
    assert client.get(f"/api/resume/revisions/{task_id}").json()["status"] == "saved"

    r = client.post(f"/api/resume/revisions/{task_id}/save", json={"format": "md"})
    assert r.status_code == 409


def test_api_revise_requires_resume(client):
    """职位置好、简历不存在 → 404。"""
    with JobStore() as store:
        store.save_page(PageResult(page=1, jobs=(make_job("a"),), raw_count=1))
    r = client.post("/api/resume/revise/start", json={"resume_id": "rs_nobody", "job_ids": ["a"]})
    assert r.status_code == 404


def test_api_revise_empty_jobs_disallowed(client):
    """job_ids 空且无 crawl_task → 422。"""
    rid, _ = _seed(client)
    r = client.post("/api/resume/revise/start", json={"resume_id": rid, "job_ids": []})
    assert r.status_code == 422
    assert "目标岗位" in r.json()["message"]
