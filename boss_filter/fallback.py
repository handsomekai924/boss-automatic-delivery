"""兜底来源：写死的选项表 + 解析已保存 HTML。

todo.md 第二节的说明：

    以上筛选项应优先通过 BOSS 直聘接口获取。如果接口不可用，也可以基于我提供
    的已保存 HTML 静态文件进行分析提取，并将可选值直接写死在项目中作为兜底方案。

所以这里两件事：
  1. :data:`boss_filter.tables` 里那套写死表（从线上接口 / 已存 HTML 抄下来的）；
  2. :func:`parse_html_conditions` —— 现场从 ``.saved_web`` 那份页面再解析一遍，
     用来校对写死表有没有过期，或在接口挂掉时现场凑一份。

公司行业**只有** HTML 这一个来源：接口侧没有下发这张表（见 config 模块的说明）。
"""

from __future__ import annotations

import re
from pathlib import Path

from . import tables
from .config import (
    DEFAULT_SAVED_HTML,
    HTML_INDUSTRY_MARKER,
    HTML_OPTION_PATTERNS,
)
from .errors import FilterDataError
from .models import (
    CityNode,
    FilterConditions,
    FilterOption,
    IndustryGroup,
    options_from_rows,
)


# --------------------------------------------------------------------------- #
# 写死表 → FilterConditions
# --------------------------------------------------------------------------- #


def build_fallback_conditions() -> FilterConditions:
    """用项目内写死的常量表拼出一份可用的筛选条件。

    城市树太大（线上约 900KB）不适合写死，兜底只有热点城市，且放在
    ``hot_cities``；``cities`` 留空，调用方看到空的城市维度就知道该去打接口。
    """
    return FilterConditions(
        cities=(),
        hot_cities=options_from_rows(tables.HOT_CITIES),
        job_types=options_from_rows(tables.JOB_TYPES),
        salaries=options_from_rows(tables.SALARIES),
        experiences=options_from_rows(tables.EXPERIENCES),
        degrees=options_from_rows(tables.DEGREES),
        scales=options_from_rows(tables.SCALES),
        industries=_industry_groups_from_tables(),
        pay_types=options_from_rows(tables.PAY_TYPES),
        stages=options_from_rows(tables.STAGES),
        part_times=options_from_rows(tables.PART_TIMES),
        source="fallback",
    )


def _industry_groups_from_tables() -> tuple[IndustryGroup, ...]:
    return tuple(
        IndustryGroup(name=group_name, options=options_from_rows(rows))
        for group_name, rows in tables.INDUSTRY_GROUPS
    )


# --------------------------------------------------------------------------- #
# HTML 解析
# --------------------------------------------------------------------------- #


def parse_html_conditions(html: str) -> FilterConditions:
    """从职位页 HTML 里抠出筛选下拉的可选值。

    能解析出 6 个维度（求职类型 / 薪资 / 经验 / 学历 / 行业 / 规模）。
    **城市不在页面里**——城市弹层是点开才渲染的，存盘的 HTML 没有它，所以
    返回对象的 ``cities`` 为空，``hot_cities`` 用写死表补上（同兜底）。
    """
    if not html or not isinstance(html, str):
        raise FilterDataError("HTML 内容为空，无法解析筛选项")

    parsed: dict[str, tuple[FilterOption, ...]] = {}
    for field_name, pattern in HTML_OPTION_PATTERNS.items():
        rows = re.findall(pattern, html)
        if not rows:
            raise FilterDataError(f"HTML 里找不到「{field_name}」的筛选项（标记 {pattern} 无命中）")
        parsed[field_name] = tuple(
            FilterOption(code=str(code), name=_clean(name)) for code, name in rows
        )

    industries = parse_html_industries(html)
    if not industries:
        raise FilterDataError("HTML 里找不到公司行业的筛选项")

    return FilterConditions(
        cities=(),
        hot_cities=options_from_rows(tables.HOT_CITIES),
        job_types=parsed["job_types"],
        salaries=parsed["salaries"],
        experiences=parsed["experiences"],
        degrees=parsed["degrees"],
        scales=parsed["scales"],
        industries=industries,
        source="html",
    )


def parse_html_industries(html: str) -> tuple[IndustryGroup, ...]:
    """只解析行业下拉：分组名取 ``<span class="label">``，组内取 ``ka="sel-industry-N"``。

    分组边界按标签切分，不依赖 ``</li>`` 的具体嵌套——存盘页和裁剪片段的
    收尾标签不一定一致。
    """
    idx = html.find(HTML_INDUSTRY_MARKER)
    if idx < 0:
        return ()
    # 行业块的终点是筛选条收尾（清空按钮）——比配一堆嵌套 div 稳。
    end = html.find('ka="empty-filter"', idx)
    block = html[idx:] if end < 0 else html[idx:end]

    groups: list[IndustryGroup] = []
    chunks = re.split(r'<span class="label">', block)[1:]
    for chunk in chunks:
        group_name, _, body = chunk.partition("</span>")
        options = tuple(
            FilterOption(code=str(code), name=_clean(name))
            for code, name in re.findall(r'ka="sel-industry-(\d+)"[^>]*>([^<]*)<i', body)
        )
        if options:
            groups.append(IndustryGroup(name=_clean(group_name), options=options))
    return tuple(groups)


def load_saved_html(path: str | Path | None = None) -> str:
    """读入已保存的职位页 HTML。默认读项目根下的 ``.saved_web/…``。"""
    target = Path(path) if path is not None else _default_html_path()
    if not target.is_file():
        raise FilterDataError(f"找不到已保存的 HTML: {target}")
    return target.read_text(encoding="utf-8", errors="replace")


def conditions_from_html(path: str | Path | None = None) -> FilterConditions:
    """读入已保存页面并解析成筛选条件。"""
    return parse_html_conditions(load_saved_html(path))


def _default_html_path() -> Path:
    """默认 HTML 路径：相对项目根（``boss_filter`` 的上一级）。"""
    root = Path(__file__).resolve().parent.parent
    return root / DEFAULT_SAVED_HTML


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def city_node(code: str, name: str, **kwargs) -> CityNode:  # pragma: no cover - 便捷构造
    return CityNode(code=str(code), name=str(name), **kwargs)
