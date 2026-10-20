"""BOSS直聘职位筛选条件获取。

接口不可用时退回项目内写死表。搜索条件的用户配置落在状态库 `data/boss.db`。
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
from .expect import EXPECT_KINDS, PART_TIME_JOB_TYPE, JobExpectFilter

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
    "EXPECT_KINDS",
    "PART_TIME_JOB_TYPE",
    "JobExpectFilter",
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
