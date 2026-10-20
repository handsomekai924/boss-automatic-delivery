"""简历 LLM 固定模板解析。

一次 LLM 调用抠出结构化 JSON；字段名固定，不开放给界面改。
失败重试一次仍失败则抛错，不覆盖原有解析结果。

Attributes:
    TEMPLATE_FIELDS: 固定模板的输出骨架（文档与校验共用；LLM 照这个形状回）。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .. import config as C
from .llm_client import LLMClient, extract_json
from .llm_config_store import LLMConfig, load_config
from ..errors import ValidationWebError

logger = logging.getLogger(__name__)

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
    """把 `TEMPLATE_FIELDS` 拼成提示词里的条目列表。"""
    return "\n".join(f"- `{key}`：{desc}" for key, desc in TEMPLATE_FIELDS.items())


def build_messages(content: str, *, retry: bool = False) -> list[dict[str, str]]:
    """拼固定模板的 chat 消息。`retry=True` 追加「严格只输出 JSON」。"""
    user = USER_TEMPLATE.format(template=_template_block(), content=content or "")
    if retry:
        user += _RETRY_SUFFIX
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def normalize(data: dict[str, Any]) -> dict[str, Any]:
    """把 LLM 回的 JSON 拉成模板形状：补键、修类型、剪长度。"""

    def _str(value: Any) -> str:
        """值转干净字符串；空 / `None` 回空串。"""
        return "" if value is None else str(value).strip()

    def _str_list(value: Any, *, limit: int = 12) -> list[str]:
        """列表值收成去重后的字符串列表。"""
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
        """经历条目列表收成定长字段字典。"""
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

    Args:
        content: 简历 Markdown 原文。
        config: LLM 配置；`llm` 没传时用来建客户端。
        llm: 注入的客户端（测试用）。

    Returns:
        模板形状的解析结果。

    非法 JSON 两次都抠不出时抛 `ResumeParseError`；LLM 未配置时抛
    `ValidationWebError`。
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

    def _chat(messages: list[dict[str, str]], max_tokens: int) -> tuple[str, str | None]:
        """调一层客户端；优先带 `finish_reason` 的接口。"""
        chat_with_metadata = getattr(llm, "chat_with_metadata", None)
        if callable(chat_with_metadata):
            result = chat_with_metadata(messages, max_tokens=max_tokens)
            return result.content, result.finish_reason
        return llm.chat(messages, max_tokens=max_tokens), None

    raw, finish_reason = _chat(
        build_messages(content), max_tokens=C.LLM_RESUME_PARSE_MAX_TOKENS
    )
    data = extract_json(raw)
    truncated = finish_reason == "length"
    if data is None or truncated:
        logger.info("简历解析首选拔回复%s，重试一次", "被截断" if truncated else "不是 JSON")
        retry_limit = (
            C.LLM_RESUME_PARSE_RETRY_MAX_TOKENS
            if truncated
            else C.LLM_RESUME_PARSE_MAX_TOKENS
        )
        raw, finish_reason = _chat(build_messages(content, retry=True), max_tokens=retry_limit)
        data = extract_json(raw)
        if finish_reason == "length":
            truncated = True
    if truncated and (finish_reason == "length" or data is None):
        raise ResumeParseError(
            "模型输出达到长度上限，简历解析结果被截断。请缩短简历，或更换支持更大输出长度的模型后重试。"
        )
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
    """解析并打上时间戳 / 模型名，回 `meta.llm` 的形状。"""
    data = parse_resume_llm(content, config=config, llm=llm)
    return {
        "parsed_at": time.time(),
        "model": model or (llm.config.model if llm is not None else (config or load_config()).model),
        "data": data,
    }
