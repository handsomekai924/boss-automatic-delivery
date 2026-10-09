"""筛选条件相关接口地址、请求头等常量。

主路径（``conditions`` / ``city`` / ``hot_city`` 等）是真实 wapi GET 接口。
**公司行业没有接口**：行业下拉是 SSR 写进 HTML 的，只能走 HTML 解析 / 写死，
见 :mod:`boss_filter.fallback`。
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import boss_db

BASE_URL: Final[str] = "https://www.zhipin.com"

#: 职位推荐页（``/web/geek/jobs``）的 Referer。接口不校验，带上更像浏览器。
GEEK_JOBS_REFERER: Final[str] = f"{BASE_URL}/web/geek/jobs"

ENDPOINTS: Final[dict[str, str]] = {
    #: 一次拿全筛选项的主力接口（比 recommend 那份多 stageList）
    "conditions": "/wapi/zpgeek/pc/all/filter/conditions.json",
    #: 推荐页在用的那份；conditions 挂了可以试它
    "recommend_conditions": "/wapi/zpgeek/pc/recommend/conditions.json",
    #: 搜索页那份；salaryList 没有 lowSalary/highSalary
    "search_condition": "/wapi/zpgeek/search/job/condition.json",
    #: 城市树（省→市→区）+ hotCityList + locationCity
    "city": "/wapi/zpCommon/data/city.json",
    #: 按 firstChar 分组的城市表，体量比 city.json 小
    "city_group": "/wapi/zpCommon/data/cityGroup.json",
    #: 只有热点城市，体量最小
    "hot_city": "/wapi/zpgeek/search/job/hot/city.json",
    #: 默认城市（当前站点城市）
    "default_city": "/wapi/zpgeek/common/data/defaultcity.json",
}

#: conditions 响应 ``zpData`` 里的列表字段 → :class:`~boss_filter.models.FilterConditions` 字段名。
#: 键是接口字段，值是模型字段；两边不一致才要这张表。
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

#: 请求头。Accept/UA 与 ``boss_login.config.DEFAULT_HEADERS`` 同源；筛选接口是
#: GET JSON，不需要表单 Content-Type。
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

#: 通用成功码（与 wapi 网关一致）
CODE_OK: Final[int] = 0

#: 业务码「未登录 / 登录态失效」。**只有 7 是实测确认的**（Cookie 过期时回
#: ``{"code":7,"message":"当前登录状态已失效"}``）。
#: **code 1 是业务失败的通用码**，别一律当登录失效——判据见
#: :attr:`boss_filter.errors.FilterApiError.is_session_expired`。
CODE_SESSION_EXPIRED: Final[frozenset[int]] = frozenset({7})

#: 默认超时（秒）
DEFAULT_TIMEOUT: Final[float] = 10.0

#: 默认重试次数（网络层/5xx）
DEFAULT_RETRIES: Final[int] = 2


#: 已保存的职位页 HTML（行业下拉等筛选项的离线来源）。路径相对项目根；
#: 不存在时兜底表仍然可用（只是没法现场重解析）。
DEFAULT_SAVED_HTML: Final[str] = (
    r".saved_web/求职_找工作_招聘信息-BOSS直聘.html"
)

#: HTML 筛选下拉的 ``ka="sel-job-rec-*-<code>"`` 标记 → (维度字段, 提取正则)。
#: 行业是 ``ka="sel-industry-<code>"``，整块定位见 :data:`HTML_INDUSTRY_MARKER`。
HTML_OPTION_PATTERNS: Final[dict[str, str]] = {
    "job_types": r'ka="sel-job-rec-jobType-(\d+)"[^>]*>\s*([^<]+?)\s*<i',
    "salaries": r'ka="sel-job-rec-salary-(\d+)"[^>]*>\s*([^<]+?)\s*<i',
    "experiences": r'ka="sel-job-rec-exp-(\d+)"[^>]*>\s*([^<]+?)\s*<i',
    "degrees": r'ka="sel-job-rec-degree-(\d+)"[^>]*>\s*([^<]+?)\s*<i',
    "scales": r'ka="sel-job-rec-scale-(\d+)"[^>]*>\s*([^<]+?)\s*<i',
}

#: 行业下拉整块（含分组标签）的定位标记
HTML_INDUSTRY_MARKER: Final[str] = "condition-industry-select"


#: 项目根目录（跟 cwd 无关）
PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

#: 状态库路径（搜索条件在 ``doc('search_filter')``，见 :mod:`boss_db`）；``BOSS_DB`` 可覆盖。
#: 用户点名的 JSON 文件不走这里，见 :func:`boss_filter.search.search_filter_from_file`。
DEFAULT_DB_PATH: Final[Path] = boss_db.DEFAULT_DB_PATH
