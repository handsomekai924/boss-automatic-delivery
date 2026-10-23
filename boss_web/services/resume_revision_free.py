"""简历自由优化：按用户选择的章节与提示词优化简历。

与定向优化（`resume_revision.py`）的区别：
- 定向优化：基于目标岗位 JD，多岗位合成画像
- 自由优化：用户选择章节 + 预设/自定义提示词

Attributes:
    PRESET_PROMPTS: 预设提示词库（5 维度 × 5 预设）。
    SYSTEM_PROMPT: 系统提示词（定立场 + 硬约束）。
    USER_TEMPLATE: 用户提示词模板。
    _RESUME_INPUT_LIMIT: 简历原文参与改写的字数上限。
"""

from __future__ import annotations

import re
from typing import Any, Sequence

from .. import config as C
from ..errors import ValidationWebError
from .llm_client import LLMClient, extract_json
from .llm_config_store import LLMConfig, load_config
from .resume_store import load_resume

_RESUME_INPUT_LIMIT = 20_000

SYSTEM_PROMPT = (
    "你是资深互联网猎头，擅长优化求职简历。"
    "这是求职者本人要投递用的简历，一律保持第一人称、中文输出（原文是中文则中文）。"
    "只输出一个 JSON 对象，不要任何解释文字或代码围栏。\n\n"
    "硬约束（必须遵守）：\n"
    "1. 绝不编造：不得新增没写过的工作经历、项目、技能、学历、证书、业绩数字。\n"
    "2. 事实字段逐字保留：公司名、职位名、起止时间、学校、专业、学历、联系方式一字不改。\n"
    "3. 只优化用户选定的章节：未选中的章节原样保留，不做任何修改。\n"
    "4. 中等幅度修改：可改措辞与结构——重排段落、精简或扩写、合并同质条目、"
    "调整表述；不做整份重组。\n"
    "5. 章节骨架保持可解析：保留原文的章节标题体系，输出仍能被按标题切段。\n"
    "6. 输出语言与原文一致；保留原文 Markdown 层级习惯（标题、列表）。\n"
    "7. 优化要自然，不堆砌，不把没做过的技术写成「熟悉」。"
)

USER_TEMPLATE = """# 简历原文
{truncate_note}{resume_md}

# 需要优化的章节
{selected_sections}

# 优化要求
{optimization_prompt}

请按系统提示词的硬约束，只优化上述选定章节，其他章节原样保留：

1. 输出一个 JSON 对象，只有两个字段；
2. "revised_md" 是修改后的**整份简历原文**（Markdown，章节结构与原稿对齐），不是 diff、不是补丁；
3. "changes" 是变更点清单，0–20 条，每条含 section（章节名）、type、
   summary、reason 四个字段；type 只能是 reordered / trimmed / expanded / keyword / rewrote 之一。
"""

PRESET_PROMPTS: dict[str, list[dict[str, str]]] = {
    "通用优化": [
        {
            "name": "全面润色",
            "prompt": "全面优化简历的表达，使语言更精炼、专业，突出个人亮点，增强可读性。保持内容真实性，不编造经历。"
        },
        {
            "name": "精简凝练",
            "prompt": "精简冗长的描述，删除冗余信息，保留核心要点。使简历更紧凑，突出关键信息，控制在合理篇幅内。"
        },
        {
            "name": "扩展完善",
            "prompt": "在不编造的前提下，扩展过于简略的描述，补充背景信息和成果细节，使每段经历更充实、更具说服力。"
        },
        {
            "name": "逻辑优化",
            "prompt": "优化各章节和条目的排列顺序，按重要性和相关性重组，使简历逻辑更清晰、重点更突出。"
        },
        {
            "name": "关键词强化",
            "prompt": "在保持自然表达的前提下，适当融入行业通用关键词和技术术语，增强简历在 ATS 系统中的匹配度。"
        }
    ],
    "技术深度": [
        {
            "name": "技术细节强化",
            "prompt": "深化技术描述，补充技术选型理由、架构设计思路、技术难点攻克过程，体现技术深度和问题解决能力。"
        },
        {
            "name": "技术栈突出",
            "prompt": "突出核心技术栈的使用深度和广度，明确各技术在项目中的具体应用场景和掌握程度。"
        },
        {
            "name": "架构设计凸显",
            "prompt": "强调系统架构设计能力，补充架构图、技术选型、扩展性考量等内容，体现架构思维。"
        },
        {
            "name": "性能优化突出",
            "prompt": "突出性能优化经验，补充优化前后的具体指标对比、优化思路和技术手段，用数据说话。"
        },
        {
            "name": "开源贡献展示",
            "prompt": "如有开源项目或技术分享经历，优化其描述，突出技术影响力和社区贡献，增强技术形象。"
        }
    ],
    "管理能力": [
        {
            "name": "团队管理突出",
            "prompt": "强化团队管理经验，补充团队规模、人员培养、绩效管理等内容，体现领导力和团队协作能力。"
        },
        {
            "name": "项目管理强化",
            "prompt": "突出项目管理能力，补充项目规划、进度把控、风险管理、跨部门协调等经验，展现项目推动力。"
        },
        {
            "name": "决策能力展示",
            "prompt": "强调关键决策经验，补充决策背景、权衡考量、执行结果，体现战略思维和决策能力。"
        },
        {
            "name": "资源整合能力",
            "prompt": "突出资源整合和跨团队协作经验，补充协调多方资源、推动跨部门合作的具体案例。"
        },
        {
            "name": "人才培养经验",
            "prompt": "强化人才培养和团队建设经验，补充招聘、培训、导师制度等内容，体现团队建设能力。"
        }
    ],
    "业绩成果": [
        {
            "name": "量化成果突出",
            "prompt": "强化业绩数据，将定性描述转化为定量指标（用户增长、性能提升、成本节约等），用数据说话。"
        },
        {
            "name": "商业价值凸显",
            "prompt": "突出工作成果的商业价值，补充对公司营收、用户增长、市场份额的具体贡献。"
        },
        {
            "name": "影响力展示",
            "prompt": "强调工作的影响范围和深度，补充受益用户数、影响团队规模、业务覆盖面等内容。"
        },
        {
            "name": "创新成果突出",
            "prompt": "突出创新性工作和突破性成果，补充创新点、难度、行业首创性等内容，体现创新能力。"
        },
        {
            "name": "奖项荣誉强化",
            "prompt": "如有奖项或荣誉，优化其描述，补充获奖背景、评选标准、含金量，增强说服力。"
        }
    ],
    "转行求职": [
        {
            "name": "可迁移技能突出",
            "prompt": "强化可迁移技能（沟通、分析、问题解决等），淡化行业特定经验，突出通用能力和学习能力。"
        },
        {
            "name": "相关经验挖掘",
            "prompt": "深挖现有经历中与目标方向相关的内容，重新组织表达，建立与目标岗位的关联。"
        },
        {
            "name": "学习能力展示",
            "prompt": "突出快速学习和适应新领域的能力，补充自学经历、技能转换案例，降低转行顾虑。"
        },
        {
            "name": "动机与热情传递",
            "prompt": "在自我评价等章节适当补充转行动机和对新领域的热情，增强求职诚意和决心。"
        },
        {
            "name": "跨界经验优势",
            "prompt": "将跨行业经验转化为优势，强调复合背景带来的独特视角和创新思维。"
        }
    ]
}


def get_preset_categories() -> list[str]:
    """返回预设提示词的所有分类。"""
    return list(PRESET_PROMPTS.keys())


def get_presets_by_category(category: str) -> list[dict[str, str]]:
    """按分类返回预设提示词列表。

    Args:
        category: 分类名称。

    Returns:
        该分类下的预设列表，每项含 `name` 和 `prompt`。
    """
    return PRESET_PROMPTS.get(category, [])


def _truncate_resume(content: str) -> tuple[str, bool]:
    """简历原文超长截尾；返回 `(文本, 是否被截断)`。"""
    if len(content) <= _RESUME_INPUT_LIMIT:
        return content, False
    return content[:_RESUME_INPUT_LIMIT], True


def build_messages(
    resume_md: str,
    selected_sections: list[str],
    optimization_prompt: str,
    *,
    retry: bool = False,
    truncate_note: str = "",
) -> list[dict[str, str]]:
    """拼简历自由优化的 chat 消息。

    Args:
        resume_md: 简历原文（可能已截断）。
        selected_sections: 用户选择的需要优化的章节列表。
        optimization_prompt: 优化提示词（预设或自定义）。
        retry: 是否重试提示。
        truncate_note: 原文截断提示。

    Returns:
        chat 消息列表。
    """
    sections_text = "、".join(f"「{s}」" for s in selected_sections) if selected_sections else "全部章节"
    user = USER_TEMPLATE.format(
        resume_md=resume_md,
        selected_sections=sections_text,
        optimization_prompt=optimization_prompt,
        truncate_note=truncate_note,
    )
    if retry:
        user += "\n\n请严格只输出 JSON 对象，不要任何其他文字。"
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


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


def generate_free_revision(
    *,
    resume_id: str,
    selected_sections: list[str],
    optimization_prompt: str,
    llm: LLMClient | None = None,
) -> dict[str, Any]:
    """跑一次简历自由优化：按用户选择的章节和提示词优化。

    Args:
        resume_id: 底稿简历 id。
        selected_sections: 用户选择的需要优化的章节列表（空 = 全部）。
        optimization_prompt: 优化提示词（预设或自定义）。
        llm: 注入的客户端（测试用）；缺省按当前配置新建。

    Returns:
        含 `revised_md` / `changes` / `original_md` 等字段的生成结果。

    Raises:
        ValidationWebError: LLM 未配置。
        FileNotFoundError: 简历不存在。
        ValueError: LLM 输出非法或长度截断。
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
    truncate_note = "（原文已截断，后段未参与改写）\n\n" if truncated else ""

    def _chat(messages: list[dict[str, str]], max_tokens: int) -> tuple[str, str | None]:
        """调一层客户端；优先带 `finish_reason` 的接口。"""
        md = getattr(client, "chat_with_metadata", None)
        if callable(md):
            result = md(messages, max_tokens=max_tokens)
            return result.content, result.finish_reason
        return client.chat(messages, max_tokens=max_tokens), None

    raw, finish_reason = _chat(
        build_messages(
            content,
            selected_sections,
            optimization_prompt,
            truncate_note=truncate_note
        ),
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
                content,
                selected_sections,
                optimization_prompt,
                retry=True,
                truncate_note=truncate_note
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
        "selected_sections": list(selected_sections),
        "optimization_prompt": optimization_prompt,
        "original_truncated": truncated,
        "original_md": resume.raw or "",
        "resume_title": resume.title,
    }
