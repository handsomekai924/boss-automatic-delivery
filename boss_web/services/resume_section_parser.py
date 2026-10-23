"""简历章节解析：从 Markdown 简历中提取章节结构。

用于自由优化模式的章节选择功能。

Attributes:
    HEADING_PATTERN: 匹配 Markdown 标题的正则模式。
"""

from __future__ import annotations

import re
from typing import Any

HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


def extract_sections(markdown_content: str) -> list[dict[str, Any]]:
    """从 Markdown 内容中提取章节列表。

    Args:
        markdown_content: Markdown 格式的简历内容。

    Returns:
        章节列表，每项包含 `title`（章节名）、`level`（标题级别）。
        按出现顺序排列。
    """
    if not markdown_content:
        return []

    sections: list[dict[str, Any]] = []
    seen_titles: set[str] = set()

    for match in HEADING_PATTERN.finditer(markdown_content):
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
