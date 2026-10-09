"""BOSS直聘职位筛选条件获取（城市 / 求职类型 / 薪资 / 经验 / 学历 / 行业 / 规模）。

优先打真实 wapi 接口，不可用时退回 :mod:`boss_filter.tables` 写死表。
公司行业没有接口，永远用写死表（或解析已保存 HTML）。

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

搜索条件的**用户配置**落在状态库 ``data/boss.db``（没有就留空 = 全部「不限」）::

    from boss_filter import load_search_filter
    f = load_search_filter()            # 读库里的 doc('search_filter')，没有就是全空
    f.to_params()                       # → {'page': '1', 'pageSize': '15', 'scene': '1'}

    # 用户点名的 JSON 文件只读一次，不写库（CLI --filter my.json）
    from boss_filter import search_filter_from_file
    f = search_filter_from_file("my.json")

CLI::

    python -m boss_filter show
    python -m boss_filter export --out filters.json
    python -m boss_filter show --fallback
"""

from __future__ import annotations

from .client import FilterClient, get_filter_conditions, load_industries, parse_conditions_payload
from .config import BASE_URL, DEFAULT_DB_PATH, DEFAULT_SAVED_HTML, ENDPOINTS, PROJECT_ROOT
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
from .search import (
    JobSearchFilter,
    ResolvedSearchFilter,
    filter_path,
    load_search_filter,
    save_search_filter,
    search_filter_from_dict,
    search_filter_from_file,
)

__version__ = "0.1.0"

__all__ = [
    "FilterClient",
    "get_filter_conditions",
    "CityNode",
    "FilterConditions",
    "FilterOption",
    "IndustryGroup",
    "JobSearchFilter",
    "ResolvedSearchFilter",
    "filter_path",
    "load_search_filter",
    "save_search_filter",
    "search_filter_from_dict",
    "search_filter_from_file",
    "FilterApiError",
    "FilterDataError",
    "FilterError",
    "FilterTransportError",
    "build_fallback_conditions",
    "conditions_from_html",
    "load_industries",
    "load_saved_html",
    "parse_conditions_payload",
    "parse_html_conditions",
    "parse_html_industries",
    "normalize_code",
    "options_from_api",
    "options_from_rows",
    "BASE_URL",
    "DEFAULT_DB_PATH",
    "DEFAULT_SAVED_HTML",
    "ENDPOINTS",
    "PROJECT_ROOT",
    "__version__",
]
