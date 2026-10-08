"""简历 Markdown 规则解析的单元测试。"""

from __future__ import annotations

from boss_web.services.resume_store import (
    extract_basic,
    extract_skills,
    parse_markdown,
    parse_resume,
)

SAMPLE = """# 张三

## 基本信息
- 姓名：张三
- 电话：13800138000
- 邮箱：zhangsan@example.com

## 求职意向
目标岗位：Python 后端 / FastAPI
城市：广州

## 工作经历
### 示例科技 · 后端工程师 · 2020-2024
- 负责订单系统
- 用 FastAPI 重写网关

## 项目经历
### 开放平台
- 设计鉴权

## 教育经历
- 华南理工大学 · 计算机 · 本科

## 技能标签
- Python、FastAPI、Redis
- PostgreSQL / Docker

## 自我评价
热爱工程化，喜欢把复杂问题拆小。
"""


def test_parse_markdown_sections():
    sections, others, notes = parse_markdown(SAMPLE)
    assert "基本信息" in sections
    assert "工作经历" in sections
    assert "技能标签" in sections
    assert "自我评价" in sections
    assert "未识别到「求职意向」章节" not in notes
    assert "项目经历" in sections


def test_parse_markdown_alias_and_missing():
    text = "# 履历\n\n## 个人总结\n我觉得还行\n"
    sections, _others, notes = parse_markdown(text)
    assert "自我评价" in sections  # 别名命中
    assert any("工作经历" in n for n in notes)


def test_extract_skills():
    sections, _, _ = parse_markdown(SAMPLE)
    skills = extract_skills(sections)
    assert "Python" in skills
    assert "FastAPI" in skills
    assert "Redis" in skills
    assert "Docker" in skills


def test_extract_basic():
    sections, _, _ = parse_markdown(SAMPLE)
    basic = extract_basic(sections)
    assert basic["phone"] == "13800138000"
    assert basic["email"] == "zhangsan@example.com"
    assert basic["name"] == "张三"


def test_parse_resume_bundle():
    draft = parse_resume(
        SAMPLE,
        resume_id="rs_x",
        title="resume.md",
        source_path="x.md",
        created_at=1.0,
    )
    assert draft.resume_id == "rs_x"
    assert draft.skills
    assert draft.basic["phone"]
    d = draft.to_dict()
    assert "raw" not in d
    assert draft.to_dict(include_raw=True)["raw"]
