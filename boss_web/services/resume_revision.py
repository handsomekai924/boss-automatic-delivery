"""简历优化：按目标岗位改一版的装配、提示词与生成。

以简历原文为底稿、绝不编造、中等幅度修改，多岗位合出一版；业务规则见
`docs/resume-targeted-revision.md`。

Attributes:
    SYSTEM_PROMPT: 系统提示词（定立场 + 硬约束）。
    USER_TEMPLATE: 用户提示词模板（装简历原文 + 岗位画像）。
    _REPRESENTATIVE_SMALL_LIMIT: 1–5 岗时每岗完整 JD 的字数上限。
    _REPRESENTATIVE_BIG_LIMIT: 岗位超过 5 个时代表性 JD 的字数上限。
    _RESUME_INPUT_LIMIT: 简历原文参与改写的字数上限，超出截尾并在提示词里明示。
"""

from __future__ import annotations

import re
from collections import Counter
from difflib import SequenceMatcher
from typing import Any, Sequence

from .. import config as C
from ..errors import ValidationWebError
from .llm_client import LLMClient, extract_json
from .llm_config_store import LLMConfig, load_config
from .resume_store import load_resume, update_revision_result

MAX_TARGET_JOBS = 10

_REPRESENTATIVE_SMALL_LIMIT = 1200
_REPRESENTATIVE_BIG_LIMIT = 800
_REPRESENTATIVE_COUNT = 5
_AGG_SKILLS_LIMIT = 40
_AGG_LABELS_LIMIT = 20

_RESUME_INPUT_LIMIT = 20_000

SYSTEM_PROMPT = (
    "你是资深互联网猎头，擅长把一份已有的求职简历按目标岗位改一版。"
    "这是求职者本人要投递用的简历，一律保持第一人称、中文输出（原文是中文则中文）。"
    "只输出一个 JSON 对象，不要任何解释文字或代码围栏。\n\n"
    "硬约束（必须遵守）：\n"
    "1. 绝不编造：不得新增没写过的工作经历、项目、技能、学历、证书、业绩数字。\n"
    "2. 事实字段逐字保留：公司名、职位名、起止时间、学校、专业、学历、联系方式一字不改。\n"
    "3. 中等幅度修改：可改自我评价 / 技能标签 / 项目与工作描述的措辞与结构——"
    "重排段落、精简或扩写、合并同质条目、按目标岗位关键词调整表述；不做整份重组。\n"
    "4. 章节骨架保持可解析：保留原文的章节标题体系，输出仍能被按标题切段。\n"
    "5. 输出语言与原文一致；保留原文 Markdown 层级习惯（标题、列表）。\n"
    "6. 对齐目标岗位关键词要自然，不堆砌，不把没做过的技术写成「熟悉」。"
)


def _job_profile_items(jobs: Sequence[Any]) -> list[dict[str, Any]]:
    """把 `Job` 序列收成装配用的画像条目（岗位名 / 公司 / JD 摘要）。"""
    items: list[dict[str, Any]] = []
    for job in jobs:
        items.append(
            {
                "job_name": getattr(job, "job_name", "") or "",
                "brand_name": getattr(job, "brand_name", "") or "",
                "salary_desc": getattr(job, "salary_desc", "") or "",
                "job_desc": getattr(job, "job_desc", "") or "",
                "skills": list(getattr(job, "skills", ()) or ()),
                "job_labels": list(getattr(job, "job_labels", ()) or ()),
            }
        )
    return items


def _aggregate(titles: Counter[str], skills: Counter[str], labels: Counter[str]) -> str:
    """把岗位集合聚合成一段「需求画像文字」。"""
    lines: list[str] = []
    if titles:
        top = ", ".join(f"{k}×{v}" for k, v in titles.most_common(10))
        lines.append(f"职位名频次：{top}")
    if skills:
        top = ", ".join(f"{k}×{v}" for k, v in skills.most_common(_AGG_SKILLS_LIMIT))
        lines.append(f"技能要求频次：{top}")
    if labels:
        top = ", ".join(f"{k}×{v}" for k, v in labels.most_common(_AGG_LABELS_LIMIT))
        lines.append(f"标签频次：{top}")
    return "\n".join(lines) or "（目标岗位无可用画像信息）"


def _jd_excerpt(item: dict[str, Any], limit: int) -> str:
    """单条岗位的 JD 摘要：有 JD 截断，没有就用技能 / 标签兜底，都没有给固定文案。"""
    desc = (item.get("job_desc") or "").strip()
    if desc:
        return desc[:limit]
    bits = [str(b) for b in (item.get("skills") or [])] + [
        str(b) for b in (item.get("job_labels") or [])
    ]
    if bits:
        return "（无 JD）标签/技能：" + "、".join(bits[:12])
    return "（无 JD，只按职位名 / 公司参与）"


def assemble_target_block(jobs: Sequence[Any]) -> tuple[str, dict[str, Any]]:
    """装配「岗位需求画像」文本块与自包含快照。

    Args:
        jobs: 目标岗位（`Job` 或行为兼容的对象）。

    Returns:
        `(提示词岗位块文本, target_json 快照)`。
    """
    items = _job_profile_items(jobs)
    count = len(items)

    titles: Counter[str] = Counter()
    skills: Counter[str] = Counter()
    labels: Counter[str] = Counter()
    for it in items:
        name = str(it["job_name"] or "").strip()
        if name:
            titles[name] += 1
        for s in it["skills"]:
            s = str(s).strip()
            if s:
                skills[s] += 1
        for l in it["job_labels"]:
            l = str(l).strip()
            if l:
                labels[l] += 1

    if count <= 5:
        parts: list[str] = []
        for i, it in enumerate(items, start=1):
            parts.append(
                f"### 目标岗位 {i}\n"
                f"- 岗位：{it['job_name']}  公司：{it['brand_name']}  薪资：{it['salary_desc']}\n"
                f"- JD：\n{_jd_excerpt(it, _REPRESENTATIVE_SMALL_LIMIT)}"
            )
        block = "\n\n".join(parts)
    else:
        jd_ranked = sorted(
            items, key=lambda x: len(str(x["job_desc"] or "")), reverse=True
        )
        reps = jd_ranked[:_REPRESENTATIVE_COUNT]
        lines = [f"目标岗位共 {count} 个。「{_aggregate(titles, skills, labels)}」"]
        lines.append("本批中 JD 最完整的代表岗位（环节）：")
        for i, it in enumerate(reps, start=1):
            lines.append(
                f"- 代表{i} 岗位：{it['job_name']}  公司：{it['brand_name']}  薪资：{it['salary_desc']}"
            )
            lines.append(_jd_excerpt(it, _REPRESENTATIVE_BIG_LIMIT))
        lines.append("全部已选岗位一览（职位名 + 公司）：")
        for it in items:
            lines.append(f"- {it['job_name']}  公司：{it['brand_name']}")
        block = "\n".join(lines)

    snapshot = {
        "count": count,
        "items": [dict(it) for it in items],
    }
    return block, snapshot


def _truncate_resume(content: str) -> tuple[str, bool]:
    """简历原文超长截尾；返回 `(文本, 是否被截断)`。"""
    if len(content) <= _RESUME_INPUT_LIMIT:
        return content, False
    return content[:_RESUME_INPUT_LIMIT], True


def build_messages(
    resume_md: str,
    target_block: str,
    *,
    retry: bool = False,
    truncate_note: str = "",
) -> list[dict[str, str]]:
    """拼简历优化的 chat 消息。`retry=True` 追加「严格只输出 JSON」。

    Args:
        resume_md: 简历原文（可能已截断）。
        target_block: 岗位画像文本块。
        retry: 是否重试提示。
        truncate_note: 原文截断提示（有截断时在输入里明示）。

    Returns:
        chat 消息列表。
    """
    user = USER_TEMPLATE.format(
        resume_md=resume_md,
        target_block=target_block,
        truncate_note=truncate_note,
    )
    if retry:
        user += "\n\n请严格只输出 JSON 对象，不要任何其他文字。"
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


USER_TEMPLATE = """# 简历原文（修改底稿，不是摘要）
{truncate_note}{resume_md}

# 目标岗位需求画像
{target_block}

请按系统提示词的硬约束，把上面这份简历按目标岗位改出一版：

1. 输出一个 JSON 对象，只有两个字段；
2. "revised_md" 是修改后的**整份简历原文**（Markdown，章节结构与原稿对齐），不是 diff、不是补丁；
3. "changes" 是变更点清单，0–20 条，每条含 section（取原稿已有章节名）、type、
   summary、reason 四个字段；type 只能是 reordered / trimmed / expanded / keyword / rewrote 之一。
"""


def generate_revision(
    *,
    resume_id: str,
    jobs: Sequence[Any],
    llm: LLMClient | None = None,
    target_json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """跑一次简历优化：装配画像 → 调 LLM → 回 `(revised_md, changes)` 形状。

    LLM 未配置抛 `ValidationWebError`，简历不存在抛 `FileNotFoundError`，
    输出非法 JSON 或长度截断抛 `ValueError`。

    Args:
        resume_id: 底稿简历 id。
        jobs: 目标岗位序列。
        llm: 注入的客户端（测试用）；缺省按当前配置新建。
        target_json: 自包含目标快照；缺省用 `jobs` 现算。

    Returns:
        含 `revised_md` / `changes` / `original_md` 等字段的生成结果。
    """
    if llm is None:
        cfg: LLMConfig = load_config()
        if not cfg.configured:
            raise ValidationWebError(
                "LLM 还没配置，请先到「模型」页填好 API Key / Base URL / 模型名"
            )
        client = LLMClient(cfg)
    else:
        client = llm

    resume = load_resume(resume_id)
    content, truncated = _truncate_resume(resume.raw or "")
    target_block, computed_snapshot = assemble_target_block(jobs)
    snapshot = dict(target_json if target_json is not None else computed_snapshot)
    snapshot["count"] = len(jobs)
    truncate_note = "（原文已截断，后段未参与改写）\n\n" if truncated else ""

    def _chat(messages: list[dict[str, str]], max_tokens: int) -> tuple[str, str | None]:
        """调一层客户端；优先带 `finish_reason` 的接口。"""
        md = getattr(client, "chat_with_metadata", None)
        if callable(md):
            result = md(messages, max_tokens=max_tokens)
            return result.content, result.finish_reason
        return client.chat(messages, max_tokens=max_tokens), None

    raw, finish_reason = _chat(
        build_messages(content, target_block, truncate_note=truncate_note),
        max_tokens=C.LLM_REVISE_MAX_TOKENS,
    )
    data = extract_json(raw)
    truncated = finish_reason == "length"
    if data is None or truncated:
        retry_limit = (
            C.LLM_REVISE_RETRY_MAX_TOKENS
            if truncated
            else C.LLM_REVISE_MAX_TOKENS
        )
        raw, finish_reason = _chat(
            build_messages(
                content, target_block, retry=True, truncate_note=truncate_note
            ),
            max_tokens=retry_limit,
        )
        data = extract_json(raw)
        if finish_reason == "length":
            truncated = True
    if truncated:
        raise ValueError(
            "模型输出达到长度上限，修改稿被截断。请精简简历，或更换支持更大输出长度的模型后重试。"
        )
    if data is None or not isinstance(data, dict):
        raise ValueError("LLM 没回合法 JSON，简历优化失败（草稿未被覆盖）")

    revised_md = str(data.get("revised_md") or "").strip()
    if not revised_md:
        raise ValueError("修改稿为空，LLM 没回出可用内容")

    changes = _normalize_changes(data.get("changes"))

    return {
        "revised_md": revised_md,
        "changes": changes,
        "target": snapshot,
        "original_truncated": truncated,
        "original_md": resume.raw or "",
        "resume_title": resume.title,
    }


def _normalize_changes(raw: Any) -> list[dict[str, Any]]:
    """把 LLM 回的 changes 收成定长、定字段的清单。"""
    if not isinstance(raw, list):
        return []
    allowed = {"rewrote", "reordered", "trimmed", "expanded", "keyword"}
    out: list[dict[str, Any]] = []
    for item in raw[:20]:
        if not isinstance(item, dict):
            continue
        itype = str(item.get("type") or "rewrote").strip()
        if itype not in allowed:
            itype = "rewrote"
        out.append(
            {
                "section": str(item.get("section") or ""),
                "type": itype,
                "summary": str(item.get("summary") or "")[:200],
                "reason": str(item.get("reason") or "")[:200],
            }
        )
    return out


def build_diff(original_md: str, revised_md: str) -> list[dict[str, Any]]:
    """用 `difflib` 算出原文 → 修改稿的结构化行块。

    Returns:
        对齐块列表：`{left, right, tag}`（`same` / `replace` / `delete` / `insert`）。
    """
    sm = SequenceMatcher(None, (original_md or "").splitlines(), (revised_md or "").splitlines())
    blocks: list[dict[str, Any]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        left = "\n".join((original_md or "").splitlines()[i1:i2])
        right = "\n".join((revised_md or "").splitlines()[j1:j2])
        blocks.append({"tag": tag, "left": left, "right": right})
    return blocks


def markdown_to_text(md: str) -> str:
    """把 Markdown 改成近似纯文本：去掉标题 / 列表符号，保留段落与换行。

    Args:
        md: Markdown 文本。

    Returns:
        纯文本（无 `#` / `-` / `*` 行首符号）。
    """
    lines: list[str] = []
    for line in (md or "").replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if not stripped:
            lines.append("")
            continue
        text = stripped
        if text.startswith("#"):
            text = re.sub(r"^#{1,6}\s+", "", text)
        else:
            text = re.sub(r"^[-*+]\s+", "", text)
            text = re.sub(r"^\d+[.、)]\s+", "", text)
        lines.append(text)
    return "\n".join(lines)