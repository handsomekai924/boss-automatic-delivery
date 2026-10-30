"""简历章节解析：从 Markdown 简历中提取章节结构。

用于自由优化模式的章节选择功能。

任意格式的简历原文（docx / pdf / txt 抽出来的纯文本也一样）先经 `to_markdown`
规范化成 Markdown 再做分析（缺陷清单「章节优化未检测到章节标题」）；
展示原文（查看原文 / 优化记录「原文」）仍用原文本，不走这里。

Attributes:
    HEADING_PATTERN: 匹配 Markdown 标题的正则模式。
"""

from __future__ import annotations

import re
from typing import Any

HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

_MD_HEADING_ANY = re.compile(r"^#{1,6}\s+\S", re.MULTILINE)

# 行首编号 / 项目符号（「一、」「1.」「(2)」「◆」都算）——提升标题前剥掉
_LINE_PREFIX = re.compile(r"^\s*(?:[一二三四五六七八九十]+、|\(?[0-9A-Za-z]{1,2}[.、)]\s*|[\-–—*·•●◆■◇■]+\s*)")

# 常见简历章节名（比较前小写 + 去标点，见 `_section_core`）
_KNOWN_SECTIONS = frozenset({
    # 中文
    "基本信息", "个人信息", "联系方式", "求职意向", "求职状态", "职业目标",
    "教育背景", "教育经历", "教育情况",
    "工作经历", "工作经验", "工作简介", "实习经历", "实习经验", "实践经历",
    "项目经历", "项目经验", "主要项目", "代表作", "作品集",
    "校园经历", "社会实践", "培训经历", "主要经历",
    "专业技能", "职业技能", "技能特长", "技能", "语言能力", "it技能",
    "自我评价", "个人评价", "自我介绍", "个人简介", "个人优势", "个人总结", "自我总结",
    "获奖经历", "所获荣誉", "荣誉奖项", "荣誉证书", "证书", "资格证书",
    "兴趣爱好", "其他", "其他说明", "附件",
    # 英文
    "summary", "professional summary", "objective", "profile", "personal statement",
    "experience", "work experience", "professional experience", "employment",
    "education", "academic background",
    "skills", "technical skills", "core skills",
    "projects", "selected projects",
    "awards", "honors", "certifications", "certificates",
    "activities", "interests", "references", "contact", "personal information",
})


def _section_core(line: str) -> str | None:
    """独占一行且命中常见章节名 → 返回干净标题；否则 None。"""
    s = line.strip()
    if not s or len(s) > 30 or "\t" in s:
        return None
    s = _LINE_PREFIX.sub("", s).strip()
    s = s.strip("：:、。.．;；·•●◆■ ").strip()
    if not s or len(s) > 30:
        return None
    return s if s.lower() in _KNOWN_SECTIONS else None


def to_markdown(text: str) -> str:
    """任意格式简历原文 → 供分析用的 Markdown 规范化文本。

    已是 Markdown 的原样返回；否则把独占一行的常见章节名提升为 `##` 标题，
    其余行一字不动。**只用于分析**（章节提取 / 改稿提示词），
    展示原文仍用原文本。
    """
    if not text or _MD_HEADING_ANY.search(text):
        return text

    out: list[str] = []
    for line in text.splitlines():
        core = _section_core(line)
        out.append(f"## {core}" if core else line)
    return "\n".join(out)


def extract_sections(markdown_content: str) -> list[dict[str, Any]]:
    """从 Markdown 内容中提取章节列表。

    非 Markdown 简历先经 `to_markdown` 规范化——docx / txt 里独占一行的
    常见章节名（工作经历 / Education …）同样能被检测出来。

    Args:
        markdown_content: Markdown 格式的简历内容（或任意格式原文）。

    Returns:
        章节列表，每项包含 `title`（章节名）、`level`（标题级别）。
        按出现顺序排列。
    """
    if not markdown_content:
        return []

    sections: list[dict[str, Any]] = []
    seen_titles: set[str] = set()

    for match in HEADING_PATTERN.finditer(to_markdown(markdown_content)):
        level = len(match.group(1))
        title = match.group(2).strip()

        if not title or title in seen_titles:
            continue

        sections.append({
            "title": title,
            "level": level,
        })
        seen_titles.add(title)

    return sections


def get_section_titles(markdown_content: str) -> list[str]:
    """提取简历的所有章节标题（去重）。

    Args:
        markdown_content: Markdown 格式的简历内容。

    Returns:
        章节标题列表。
    """
    sections = extract_sections(markdown_content)
    return [s["title"] for s in sections]
