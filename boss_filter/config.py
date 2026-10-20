"""筛选条件相关接口地址、请求头等常量。

主路径（`conditions` / `city` / `hot_city` / `industry` 等）是真实 wapi GET 接口。
公司行业走 `/wapi/zpCommon/data/industry.json`，不再靠 HTML 解析。

Attributes:
    GEEK_JOBS_REFERER: 职位推荐页 Referer；接口不校验，带上更像浏览器。
    ENDPOINTS: 接口路径表。
    ENDPOINTS["conditions"]: 一次拿全筛选项的主力接口（比 recommend 那份多 stageList）。
    ENDPOINTS["recommend_conditions"]: 推荐页那份；conditions 挂了可以试它。
    ENDPOINTS["search_condition"]: 搜索页那份；salaryList 没有 lowSalary/highSalary。
    ENDPOINTS["city_group"]: 按 firstChar 分组的城市表，体量比 city.json 小。
    ENDPOINTS["industry"]: 公司行业树（15 分组 + 134 子项，code 为 1000xx）。
    CONDITION_LIST_FIELDS: conditions 响应列表字段 → 模型字段名。
    DEFAULT_HEADERS: 请求头；Accept/UA 与 `boss_login.config.DEFAULT_HEADERS` 同源。
    CODE_SESSION_EXPIRED: 未登录/登录态失效业务码；只有 7 是实测确认的，code 1 不算。
    DEFAULT_SAVED_HTML: 已保存的职位页 HTML（离线来源）。
    HTML_OPTION_PATTERNS: HTML 筛选下拉标记 → (维度字段, 提取正则)。
    HTML_INDUSTRY_MARKER: 行业下拉整块（含分组标签）的定位标记。
    DEFAULT_DB_PATH: 状态库路径（搜索条件在 `doc('search_filter')`）；`BOSS_DB` 可覆盖。
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import boss_db

BASE_URL: Final[str] = "https://www.zhipin.com"

GEEK_JOBS_REFERER: Final[str] = f"{BASE_URL}/web/geek/jobs"

ENDPOINTS: Final[dict[str, str]] = {
    "conditions": "/wapi/zpgeek/pc/all/filter/conditions.json",
    "recommend_conditions": "/wapi/zpgeek/pc/recommend/conditions.json",
    "search_condition": "/wapi/zpgeek/search/job/condition.json",
    "city": "/wapi/zpCommon/data/city.json",
    "city_group": "/wapi/zpCommon/data/cityGroup.json",
    "hot_city": "/wapi/zpgeek/search/job/hot/city.json",
    "default_city": "/wapi/zpgeek/common/data/defaultcity.json",
    "industry": "/wapi/zpCommon/data/industry.json",
}

CONDITION_LIST_FIELDS: Final[tuple[tuple[str, str], ...]] = (
    ("jobTypeList", "job_types"),
    ("salaryList", "salaries"),
    ("experienceList", "experiences"),
    ("degreeList", "degrees"),
    ("scaleList", "scales"),
    ("payTypeList", "pay_types"),
    ("stageList", "stages"),
    ("partTimeList", "part_times"),
)

DEFAULT_HEADERS: Final[dict[str, str]] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": GEEK_JOBS_REFERER,
    "X-Requested-With": "XMLHttpRequest",
}

CODE_OK: Final[int] = 0

CODE_SESSION_EXPIRED: Final[frozenset[int]] = frozenset({7})

DEFAULT_TIMEOUT: Final[float] = 10.0

DEFAULT_RETRIES: Final[int] = 2


DEFAULT_SAVED_HTML: Final[str] = (
    r".saved_web/求职_找工作_招聘信息-BOSS直聘.html"
)

HTML_OPTION_PATTERNS: Final[dict[str, str]] = {
    "job_types": r'ka="sel-job-rec-jobType-(\d+)"[^>]*>\s*([^<]+?)\s*<i',
    "salaries": r'ka="sel-job-rec-salary-(\d+)"[^>]*>\s*([^<]+?)\s*<i',
    "experiences": r'ka="sel-job-rec-exp-(\d+)"[^>]*>\s*([^<]+?)\s*<i',
    "degrees": r'ka="sel-job-rec-degree-(\d+)"[^>]*>\s*([^<]+?)\s*<i',
    "scales": r'ka="sel-job-rec-scale-(\d+)"[^>]*>\s*([^<]+?)\s*<i',
}

HTML_INDUSTRY_MARKER: Final[str] = "condition-industry-select"


PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

DEFAULT_DB_PATH: Final[Path] = boss_db.DEFAULT_DB_PATH
