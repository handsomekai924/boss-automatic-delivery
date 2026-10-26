"""面试题生成：基于 JD + 简历 + 匹配结果生成面试准备题目。

按 `docs/interview-question-generation.md` 实现：
- 题型：技术深度 / 项目经历 / 场景 / 软技能
- 难度：基础 / 中级 / 高级
- 输出：结构化 JSON（题目、类型、难度、高频、思路、知识点、参考答案）

Attributes:
    QUESTION_TYPES: 四类题型的固定取值。
    DIFFICULTIES: 三级难度的固定取值。
    SUPPLEMENT_MAX: 补题单次上限（用户自填数量的最大值）。
    SYSTEM_PROMPT: 系统提示词（定面试官立场 + 输出格式）。
    USER_TEMPLATE: 用户提示词模板（装 JD / 简历 / 匹配分析）。
    _RESUME_INPUT_LIMIT: 简历原文参与生成的字数上限，超出截尾。
    _JD_INPUT_LIMIT: JD 参与生成的字数上限。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from .. import config as C
from ..errors import ValidationWebError
from .llm_client import LLMClient, extract_json
from .llm_config_store import LLMConfig, load_config

QUESTION_TYPES: tuple[str, ...] = ("技术深度", "项目经历", "场景", "软技能")
DIFFICULTIES: tuple[str, ...] = ("基础", "中级", "高级")
#: 补题单次上限。
SUPPLEMENT_MAX = 10

_RESUME_INPUT_LIMIT = 8000
_JD_INPUT_LIMIT = 4000

SYSTEM_PROMPT = (
    "你是资深技术面试官，擅长根据岗位要求和候选人背景设计面试题。\n"
    "只输出一个 JSON 对象，不要任何解释文字或代码围栏。\n\n"
    "输出格式：\n"
    "{\n"
    '  "questions": [\n'
    "    {\n"
    '      "question_text": "具体问题",\n'
    '      "question_type": "技术深度|项目经历|场景|软技能",\n'
    '      "difficulty": "基础|中级|高级",\n'
    '      "is_frequent": true/false,\n'
    '      "tags": ["算法", "系统设计", "项目经验", "业务理解", "行为面试"],\n'
    '      "answer_hints": {\n'
    '        "approach": "答题思路框架",\n'
    '        "key_points": ["关键知识点1", "关键知识点2"],\n'
    '        "reference_answer": "参考答案（可直接用的完整回答，突出得分点）"\n'
    "      }\n"
    "    }\n"
    "  ]\n"
    "}\n\n"
    "硬约束（必须遵守）：\n"
    "1. 题型只能四选一：技术深度 / 项目经历 / 场景 / 软技能。\n"
    "2. 难度只能三选一：基础 / 中级 / 高级。\n"
    "3. 结合候选人简历的真实经验出题，不问与岗位和简历都无关的纯理论题。\n"
    "4. 侧重实战场景；针对简历薄弱环节（匹配分析里的劣势）出补充题。\n"
    "5. is_frequent 标注该类岗位面试中的高频考点。\n"
    "6. answer_hints 给答题思路、关键知识点和参考答案；参考答案要完整能直接用，"
    "控制篇幅、突出得分点。\n"
    "7. 题型分布按用户要求；用户没指定时四类题型都要覆盖。"
    "高匹配度岗位偏深入，低匹配度岗位偏基础。"
)

USER_TEMPLATE = """请生成该岗位的面试准备题目（共约 {question_count} 道）。

## 职位信息
职位名称：{job_name}
公司：{brand_name}
经验要求：{job_experience}
学历要求：{job_degree}
技能标签：{job_skills}
职位描述：
{job_desc}

## 候选人简历
{resume_content}

## 匹配分析
匹配度：{match_score}
优势：{pros}
劣势：{cons}
{supplement_block}"""

SUPPLEMENT_BLOCK = """
## 补充出题要求
- 只出「{question_type}」这一种题型，共 {question_count} 道，不要混入其它题型。
- 下面是这个岗位已有的题目，**不要出重复或换皮的题**（题意相同、只换措辞都算重复）：

{existing_list}
"""


@dataclass
class InterviewQuestion:
    """一道面试题。

    Attributes:
        question_text: 题目正文。
        question_type: 题型（技术深度 / 项目经历 / 场景 / 软技能）。
        difficulty: 难度（基础 / 中级 / 高级）。
        is_frequent: 是否高频考点。
        answer_hints: 答题思路 / 知识点 / 参考答案（可含 tags）。
    """

    question_text: str
    question_type: str
    difficulty: str
    is_frequent: bool
    answer_hints: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转成落库 / 出参形状。"""
        return {
            "question_text": self.question_text,
            "question_type": self.question_type,
            "difficulty": self.difficulty,
            "is_frequent": self.is_frequent,
            "answer_hints": self.answer_hints,
        }


def question_count_for(match_score: Any) -> int:
    """按匹配度定题量：高分多出题，低分出基础题量（配置区间内）。

    Args:
        match_score: 匹配分（0-100）；不是数字按 0 处理。

    Returns:
        题量（`INTERVIEW_QUESTION_MIN` ~ `INTERVIEW_QUESTION_MAX`）。
    """
    low = int(C.INTERVIEW_QUESTION_MIN)
    high = int(C.INTERVIEW_QUESTION_MAX)
    try:
        score = float(match_score or 0)
    except (TypeError, ValueError):
        score = 0.0
    if score >= 80:
        return high
    if score >= 60:
        return max(low, (low + high) // 2)
    return low


def build_messages(
    *,
    job_data: dict[str, Any],
    resume_content: str,
    match_result: dict[str, Any],
    question_count: int,
    question_type: str = "",
    existing_questions: Sequence[str] = (),
) -> list[dict[str, str]]:
    """装提示词消息。

    Args:
        job_data: 职位信息（job_name / brand_name / job_desc / …）。
        resume_content: 简历全文（超长截尾）。
        match_result: 匹配结果（match_score / pros / cons）。
        question_count: 目标题量。
        question_type: 补题时只出这个题型；空 = 四类都要覆盖。
        existing_questions: 已有题目正文，补题时喂给模型避开重复。

    Returns:
        system + user 两条消息。
    """
    pros = match_result.get("pros") or []
    cons = match_result.get("cons") or []
    skills = job_data.get("job_skills") or []
    if isinstance(skills, str):
        skills_text = skills
    else:
        skills_text = "、".join(str(s) for s in skills[:12]) or "-"
    supplement_block = ""
    if question_type:
        existing_list = (
            "\n".join(f"- {text}" for text in existing_questions if str(text).strip())
            or "- （暂无）"
        )
        supplement_block = SUPPLEMENT_BLOCK.format(
            question_type=question_type,
            question_count=question_count,
            existing_list=existing_list,
        )
    user = USER_TEMPLATE.format(
        question_count=question_count,
        job_name=str(job_data.get("job_name") or "-"),
        brand_name=str(job_data.get("brand_name") or "-"),
        job_experience=str(job_data.get("job_experience") or "-"),
        job_degree=str(job_data.get("job_degree") or "-"),
        job_skills=skills_text,
        job_desc=_clip(str(job_data.get("job_desc") or ""), _JD_INPUT_LIMIT),
        resume_content=_clip(str(resume_content or ""), _RESUME_INPUT_LIMIT),
        match_score=match_result.get("match_score") if match_result.get("match_score") is not None else "-",
        pros="、".join(str(p) for p in pros[:6]) or "-",
        cons="、".join(str(c) for c in cons[:6]) or "-",
        supplement_block=supplement_block,
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def _clip(text: str, limit: int) -> str:
    """超长截尾并明示（截尾后段不参与生成）。"""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n（原文已截断，后段未参与出题）"


def _normalize_hints(raw: Any) -> dict[str, Any]:
    """把 answer_hints 收成定长、定字段的对象。"""
    if not isinstance(raw, dict):
        raw = {}
    key_points = raw.get("key_points")
    if isinstance(key_points, list):
        points = [str(p).strip() for p in key_points if str(p).strip()][:10]
    elif key_points:
        points = [str(key_points).strip()]
    else:
        points = []
    tags = raw.get("tags")
    tag_list = [str(t).strip() for t in tags][:6] if isinstance(tags, list) else []
    hints: dict[str, Any] = {
        "approach": str(raw.get("approach") or "").strip()[:500],
        "key_points": points,
        "reference_answer": str(
            raw.get("reference_answer") or raw.get("reference_outline") or ""
        ).strip()[:1500],
    }
    if tag_list:
        hints["tags"] = tag_list
    return hints


def _normalize_question(raw: Any) -> InterviewQuestion | None:
    """把 LLM 回的单题收成 `InterviewQuestion`；不合法回 `None`。"""
    if not isinstance(raw, dict):
        return None
    text = str(raw.get("question_text") or "").strip()
    if not text:
        return None
    qtype = str(raw.get("question_type") or "").strip()
    if qtype not in QUESTION_TYPES:
        # LLM 偶尔写近义词，收不进四类的丢掉而不是瞎归类。
        return None
    difficulty = str(raw.get("difficulty") or "").strip()
    if difficulty not in DIFFICULTIES:
        return None
    hints = _normalize_hints(raw.get("answer_hints"))
    # tags 允许放在顶层，也允许放在 answer_hints 里。
    if not hints.get("tags") and isinstance(raw.get("tags"), list):
        hints["tags"] = [str(t).strip() for t in raw.get("tags")][:6]
    return InterviewQuestion(
        question_text=text[:500],
        question_type=qtype,
        difficulty=difficulty,
        is_frequent=bool(raw.get("is_frequent")),
        answer_hints=hints,
    )


def parse_questions(data: dict[str, Any]) -> list[InterviewQuestion]:
    """从 LLM JSON 里收题目清单；坏题丢掉，全坏回空列表。"""
    rows = data.get("questions")
    if not isinstance(rows, list):
        raise ValueError("AI 返回缺少 questions 字段")
    out: list[InterviewQuestion] = []
    for row in rows:
        q = _normalize_question(row)
        if q is not None:
            out.append(q)
    return out


def _norm_title(text: str) -> str:
    """题目正文归一（去空白/标点/大小写），用来判重。"""
    return re.sub(r"[\s\W_]+", "", str(text or "")).lower()


def _char_dice(a: str, b: str) -> float:
    """字符集合 Dice 相似度（换皮题判重用）。"""
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return 2.0 * len(sa & sb) / (len(sa) + len(sb))


#: 归一后字符 Dice 超过这个值当同一道题。
_DUP_DICE = 0.75


def is_duplicate_question(text: str, existing: Sequence[str]) -> bool:
    """题目正文是否和已有题重复（含换皮：归一相同 / 互相包含 / 高相似）。

    Args:
        text: 待判题目正文。
        existing: 已有题目正文。

    Returns:
        重复回 `True`。
    """
    key = _norm_title(text)
    if not key:
        return True
    for other in existing:
        o = _norm_title(other)
        if not o:
            continue
        if key == o:
            return True
        # 短的一边嵌在长的一边里 → 同一题的换皮/缩写
        if len(key) >= 6 and len(o) >= 6 and (key in o or o in key):
            return True
        if _char_dice(key, o) >= _DUP_DICE:
            return True
    return False


def filter_new_questions(
    questions: Sequence[InterviewQuestion],
    *,
    existing: Sequence[str],
) -> list[InterviewQuestion]:
    """丢掉和已有题（或本批内部）重复的题。

    Args:
        questions: 本批生成的题。
        existing: 已有题目正文。

    Returns:
        去重后的清单。
    """
    out: list[InterviewQuestion] = []
    seen: list[str] = list(existing)
    for q in questions:
        if is_duplicate_question(q.question_text, seen):
            continue
        seen.append(q.question_text)
        out.append(q)
    return out


def generate_questions(
    *,
    job_data: dict[str, Any],
    resume_content: str,
    match_result: dict[str, Any],
    question_count: int | None = None,
    question_type: str = "",
    existing_questions: Sequence[str] = (),
    llm: LLMClient | None = None,
) -> list[InterviewQuestion]:
    """调 LLM 生成一岗的面试题。

    LLM 未配置抛 `ValidationWebError`，输出非法抛 `ValueError`。
    给了 `question_type` 就走补题：只出这一型、避开已有题、数量不越 `SUPPLEMENT_MAX`。

    Args:
        job_data: 职位信息（job_name / brand_name / job_desc / …）。
        resume_content: 简历全文。
        match_result: 匹配结果（match_score / pros / cons）。
        question_count: 题量；缺省按匹配度定，范围 `INTERVIEW_QUESTION_MIN`~`MAX`。
            补题时就是用户填的数量（1~`SUPPLEMENT_MAX`）。
        question_type: 补题题型；空 = 整套出题。
        existing_questions: 已有题目正文，补题时避开重复。
        llm: 注入的客户端（测试用）；缺省按当前配置新建。

    Returns:
        面试题清单（补题时已去掉与已有题重复的）。
    """
    if llm is None:
        cfg: LLMConfig = load_config()
        if not cfg.configured:
            raise ValidationWebError(
                "AI 还没配置，请先到「AI 设置」填好 API Key / 接口地址 / 模型"
            )
        client = LLMClient(cfg)
    else:
        client = llm

    if not str(job_data.get("job_desc") or "").strip():
        raise ValueError("岗位缺少职位描述，无法生成面试题")

    if question_type:
        if question_type not in QUESTION_TYPES:
            raise ValidationWebError(
                f"题型只能是：{' / '.join(QUESTION_TYPES)}"
            )
        try:
            asked = int(question_count if question_count is not None else 3)
        except (TypeError, ValueError):
            asked = 3
        count = max(1, min(SUPPLEMENT_MAX, asked))
    else:
        low, high = int(C.INTERVIEW_QUESTION_MIN), int(C.INTERVIEW_QUESTION_MAX)
        if question_count is None:
            count = question_count_for(match_result.get("match_score"))
        else:
            count = max(low, min(high, int(question_count)))

    messages = build_messages(
        job_data=job_data,
        resume_content=resume_content,
        match_result=match_result,
        question_count=count,
        question_type=question_type,
        existing_questions=existing_questions,
    )
    raw = client.chat(messages, max_tokens=C.LLM_INTERVIEW_MAX_TOKENS)
    data = extract_json(raw)
    if data is None:
        raw = client.chat(
            [*messages, {"role": "user", "content": "请严格只输出 JSON 对象。"}],
            max_tokens=C.LLM_INTERVIEW_MAX_TOKENS,
        )
        data = extract_json(raw)
    if data is None:
        raise ValueError("AI 返回格式错误，请重试")

    questions = parse_questions(data)
    if question_type:
        # 补题：只要指定题型，再和已有题（含本批内部）去重。
        questions = [q for q in questions if q.question_type == question_type]
        questions = filter_new_questions(questions, existing=existing_questions)
    if not questions:
        raise ValueError("AI 没有生成有效的面试题")
    return questions[: count if question_type else int(C.INTERVIEW_QUESTION_MAX)]
