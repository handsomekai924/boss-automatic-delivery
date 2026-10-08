"""简历 LLM 固定模板解析。

规则切章节退居幕后（只用来「阅读原文」），**解析结果**一律出自这里的固定模板：
一次 LLM 调用抠出结构化 JSON（基本信息 / 求职意向 / 工作 / 项目 / 教育 / 技能 /
自我评价 / 摘要），字段对齐 ``docs/todo.md`` 第四节「简历解析字段」。

模板是**固定的**，不开放给界面改——改字段名会连累匹配页吃同一套结构。
失败重试一次（追加「严格只输出 JSON」），仍失败则抛错，**不覆盖**原有解析结果。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .llm_client import LLMClient, extract_json
from .llm_config_store import LLMConfig, load_config
from ..errors import ValidationWebError

logger = logging.getLogger(__name__)

#: 固定模板的输出骨架（文档与校验共用；LLM 照这个形状回）
TEMPLATE_FIELDS: dict[str, str] = {
    "basic": "基本信息：name/phone/email/city，缺的留空串",
    "intent": "求职意向：position/city/salary",
    "work": "工作经历数组：company/title/period/highlights[]",
    "project": "项目经历数组：name/role/period/highlights[]",
    "education": "教育经历数组：school/major/degree/period",
    "skills": "技能标签字符串数组，如 [\"Python\",\"FastAPI\"]",
    "self_evaluation": "自我评价，一段话",
    "summary": "300 字内的简历整体摘要，给后续匹配用",
}

SYSTEM_PROMPT = (
    "你是资深简历结构化助手，只输出 JSON 对象，不要解释、不要代码围栏。"
)

USER_TEMPLATE = """请把下面这份简历整理成**固定模板**的 JSON 对象。

# 输出模板（严格按这个形状，字段名一个都不能改）
{template}

# 简历原文
---
{content}
---

要求：
1. 只输出一个 JSON 对象，不要解释、不要 markdown 围栏；
2. 信息没写就留空串 / 空数组，**不要编造**；
3. highlights 用简短动宾短语（「主导订单系统重构，QPS 提升 3 倍」）；
4. summary 300 字以内，概括技术栈、年限、亮点，给后续人岗匹配用。
"""

_RETRY_SUFFIX = "\n\n请严格只输出 JSON 对象，不要任何其他文字。"


class ResumeParseError(ValueError):
    """LLM 没回出可用的结构化结果。"""


def _template_block() -> str:
    return "\n".join(f"- `{key}`：{desc}" for key, desc in TEMPLATE_FIELDS.items())


def build_messages(content: str, *, retry: bool = False) -> list[dict[str, str]]:
    """拼固定模板的 chat 消息。``retry=True`` 追加「严格只输出 JSON」。"""
    user = USER_TEMPLATE.format(template=_template_block(), content=content or "")
    if retry:
        user += _RETRY_SUFFIX
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def normalize(data: dict[str, Any]) -> dict[str, Any]:
    """把 LLM 回的 JSON 拉成模板形状：补键、修类型、剪长度。

    不求全对（LLM 偶尔漏键），但求**形状稳定**——匹配页直接吃这几个字段。
    """

    def _str(value: Any) -> str:
        return "" if value is None else str(value).strip()

    def _str_list(value: Any, *, limit: int = 12) -> list[str]:
        if not isinstance(value, list):
            return []
        out: list[str] = []
        for item in value:
            text = _str(item)
            if text and text not in out:
                out.append(text)
            if len(out) >= limit:
                break
        return out

    def _entries(value: Any, keys: tuple[str, ...], *, limit: int = 8) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        out: list[dict[str, Any]] = []
        for raw in value[:limit]:
            if not isinstance(raw, dict):
                continue
            entry: dict[str, Any] = {}
            for key in keys:
                entry[key] = _str_list(raw.get(key), limit=6) if key == "highlights" else _str(raw.get(key))
            out.append(entry)
        return out

    basic = data.get("basic") if isinstance(data.get("basic"), dict) else {}
    intent = data.get("intent") if isinstance(data.get("intent"), dict) else {}

    return {
        "basic": {
            "name": _str(basic.get("name")),
            "phone": _str(basic.get("phone")),
            "email": _str(basic.get("email")),
            "city": _str(basic.get("city")),
        },
        "intent": {
            "position": _str(intent.get("position")),
            "city": _str(intent.get("city")),
            "salary": _str(intent.get("salary")),
        },
        "work": _entries(data.get("work"), ("company", "title", "period", "highlights")),
        "project": _entries(data.get("project"), ("name", "role", "period", "highlights")),
        "education": _entries(data.get("education"), ("school", "major", "degree", "period")),
        "skills": _str_list(data.get("skills"), limit=40),
        "self_evaluation": _str(data.get("self_evaluation"))[:500],
        "summary": _str(data.get("summary"))[:600],
    }


def parse_resume_llm(
    content: str,
    *,
    config: LLMConfig | None = None,
    llm: LLMClient | None = None,
) -> dict[str, Any]:
    """LLM 固定模板解析简历原文，回模板形状的 dict。

    :param content: 简历 Markdown 原文
    :param config: LLM 配置；``llm`` 没传时用来建客户端
    :param llm: 注入的客户端（测试用）
    :raises ResumeParseError: 两次都没抠出合法 JSON
    :raises ValidationWebError: LLM 还没配置
    """
    if llm is None:
        cfg = config or load_config()
        if not cfg.configured:
            raise ValidationWebError(
                "LLM 还没配置，请先到「模型」页填好 API Key / Base URL / 模型名"
            )
        llm = LLMClient(cfg)

    if not (content or "").strip():
        raise ResumeParseError("简历原文是空的，没法解析")

    raw = llm.chat(build_messages(content))
    data = extract_json(raw)
    if data is None:
        logger.info("简历解析首选拔回复不是 JSON，重试一次")
        raw = llm.chat(build_messages(content, retry=True))
        data = extract_json(raw)
    if data is None:
        raise ResumeParseError("LLM 没回出合法 JSON，解析失败（原有解析结果未被覆盖）")
    return normalize(data)


def parse_and_stamp(
    content: str,
    *,
    model: str = "",
    config: LLMConfig | None = None,
    llm: LLMClient | None = None,
) -> dict[str, Any]:
    """解析并打上时间戳/模型名，回 ``meta.llm`` 的形状。

    ::

        {"parsed_at": 1728000000.0, "model": "…", "data": { …模板结构体… }}
    """
    data = parse_resume_llm(content, config=config, llm=llm)
    return {
        "parsed_at": time.time(),
        "model": model or (llm.config.model if llm is not None else (config or load_config()).model),
        "data": data,
    }
