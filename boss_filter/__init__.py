"""BOSS直聘职位筛选条件获取。

按 todo.md 第二节，拿齐 7 类筛选维度的**可选值**（供用户配置筛选条件用）：

    城市 / 求职类型 / 薪资待遇 / 工作经验 / 学历要求 / 公司行业 / 公司规模

优先打真实 wapi 接口，接口不可用时退回项目内写死表（数据来自线上接口与
已保存的职位页 HTML）。公司行业没有接口，永远用写死表。

>>> from boss_filter import get_filter_conditions
>>> cond = get_filter_conditions()          # 打真实接口，挂了自动兜底
>>> cond.source
'api'
>>> [o.name for o in cond.job_types]
['不限', '全职', '兼职']
>>> cond.find_option("salaries", "405").name
'10-20K'

离线（不打接口）::

    cond = get_filter_conditions(html_path=".saved_web/求职_找工作_招聘信息-BOSS直聘.html")

CLI::

    python -m boss_filter show
    python -m boss_filter export --out filters.json
    python -m boss_filter show --fallback
"""

from __future__ import annotations

from .client import FilterClient, get_filter_conditions, load_industries, parse_conditions_payload
from .config import BASE_URL, DEFAULT_SAVED_HTML, ENDPOINTS
from .errors import (
    FilterApiError,
    FilterDataError,
    FilterError,
    FilterTransportError,
)
from .fallback import (
    build_fallback_conditions,
    conditions_from_html,
    load_saved_html,
    parse_html_conditions,
    parse_html_industries,
)
from .models import (
    CityNode,
    FilterConditions,
    FilterOption,
    IndustryGroup,
    normalize_code,
    options_from_api,
    options_from_rows,
)

__version__ = "0.1.0"

__all__ = [
    # 核心
    "FilterClient",
    "get_filter_conditions",
    # 模型
    "CityNode",
    "FilterConditions",
    "FilterOption",
    "IndustryGroup",
    # 异常
    "FilterApiError",
    "FilterDataError",
    "FilterError",
    "FilterTransportError",
    # 兜底 / HTML
    "build_fallback_conditions",
    "conditions_from_html",
    "load_industries",
    "load_saved_html",
    "parse_conditions_payload",
    "parse_html_conditions",
    "parse_html_industries",
    # 工具
    "normalize_code",
    "options_from_api",
    "options_from_rows",
    # 常量
    "BASE_URL",
    "DEFAULT_SAVED_HTML",
    "ENDPOINTS",
    "__version__",
]
