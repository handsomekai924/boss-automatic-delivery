"""筛选条件相关接口地址、请求头等常量。

⚠️ 关于接口地址的可信度
--------------------------------------------------------------------------------
**已实测确认**（2026-09-30 直接打真实站点，Cookie 取自 ``boss_login`` 保存的
登录会话；全部 200 + ``{"code":0,"message":"Success",…}``）：

  GET /wapi/zpgeek/pc/all/filter/conditions.json   7 类筛选项 + 3 类辅助项
  GET /wapi/zpgeek/pc/recommend/conditions.json    同上，但少了 stageList
  GET /wapi/zpgeek/search/job/condition.json       搜索页那份；salary 无 low/high
  GET /wapi/zpCommon/data/city.json                省→市→区树 + 热点城市 + 定位城市
  GET /wapi/zpCommon/data/cityGroup.json           按拼音首字母分组的城市表
  GET /wapi/zpgeek/search/job/hot/city.json        只有热点城市（15 项）
  GET /wapi/zpgeek/common/data/defaultcity.json    默认城市（广州 101280100）
  GET /wapi/zpgeek/businessDistrict.json?city=…    商圈（广州为空）

路由来自按需 chunk ``static.zhipin.com/zhipin-geek-spa/web/v6748/`` 的
``app~2.1a6c0514.js``（``(0,r.ZV)(path)`` 是 GET 工厂，不是猜的）。

**公司行业没有接口**：职位页的行业下拉是 SSR 写进 HTML 的，chunk 里没有
下发这张表的调用。行业数据只能走 HTML 解析 / 写死，见 :mod:`boss_filter.fallback`。
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import boss_db

BASE_URL: Final[str] = "https://www.zhipin.com"

#: 职位推荐页（``/web/geek/jobs``，即 ``.saved_web`` 存的那页）的 Referer。
#: 接口不校验它，但带上更像浏览器，也方便服务端定位城市。
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

#: 业务码「未登录 / 登录态失效」。实测 ``/wapi/zpgeek/recommend/industry/query.json``
#: 在 Cookie 过期时回 ``{"code":7,"message":"当前登录状态已失效"}``。
CODE_SESSION_EXPIRED: Final[frozenset[int]] = frozenset({1, 7})

#: 默认超时（秒）
DEFAULT_TIMEOUT: Final[float] = 10.0

#: 默认重试次数（网络层/5xx）
DEFAULT_RETRIES: Final[int] = 2

# --------------------------------------------------------------------------- #
# 兜底 HTML
# --------------------------------------------------------------------------- #

#: 已保存的职位页（浏览器「另存为」），行业下拉等筛选项的离线来源。
#: 路径相对项目根目录；不存在时兜底表仍然可用（只是没法现场重解析）。
DEFAULT_SAVED_HTML: Final[str] = (
    r".saved_web/求职_找工作_招聘信息-BOSS直聘.html"
)

#: HTML 里筛选下拉的定位标记，来自实存页面，不是猜的：
#:   求职类型  <li ka="sel-job-rec-jobType-{code}">
#:   薪资待遇  <li ka="sel-job-rec-salary-{code}">
#:   工作经验  <li ka="sel-job-rec-exp-{code}">
#:   学历要求  <li ka="sel-job-rec-degree-{code}">
#:   公司规模  <li ka="sel-job-rec-scale-{code}">
#:   公司行业  <a ka="sel-industry-{code}">，外面套 <span class="label">分组名
HTML_OPTION_PATTERNS: Final[dict[str, str]] = {
    "job_types": r'ka="sel-job-rec-jobType-(\d+)"[^>]*>\s*([^<]+?)\s*<i',
    "salaries": r'ka="sel-job-rec-salary-(\d+)"[^>]*>\s*([^<]+?)\s*<i',
    "experiences": r'ka="sel-job-rec-exp-(\d+)"[^>]*>\s*([^<]+?)\s*<i',
    "degrees": r'ka="sel-job-rec-degree-(\d+)"[^>]*>\s*([^<]+?)\s*<i',
    "scales": r'ka="sel-job-rec-scale-(\d+)"[^>]*>\s*([^<]+?)\s*<i',
}

#: 行业下拉整块（含分组标签）的定位标记
HTML_INDUSTRY_MARKER: Final[str] = "condition-industry-select"

# --------------------------------------------------------------------------- #
# 搜索条件的落盘位置
# --------------------------------------------------------------------------- #

#: 项目根目录（跟 cwd 无关）
PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

#: 状态库路径。用户的搜索条件存在库里 ``doc('search_filter')`` 那一行
#: （见 :mod:`boss_db`）；环境变量 ``BOSS_DB`` 可覆盖。
#: 用户点名的 JSON 文件（``--filter my.json``）不走这里，见
#: :func:`boss_filter.search.search_filter_from_file`。
DEFAULT_DB_PATH: Final[Path] = boss_db.DEFAULT_DB_PATH
