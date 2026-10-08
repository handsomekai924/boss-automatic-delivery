"""职位获取相关接口地址、请求头、节流参数等常量。

⚠️ 关于接口地址的可信度
--------------------------------------------------------------------------------
**已实测确认**（2026-09-30，Cookie 取自 ``boss_login`` 落盘的登录会话
（``data/boss.db`` 的 ``doc('session')``），直接打真实站点）：

  GET /wapi/zpgeek/pc/special/zone/joblist.json?page=…&type=1
      → 200 + {"code":0,"message":"Success","zpData":{"jobList":[…],"hasMore":…}}
        每页 15 条（pageSize 传了也不认），字段覆盖 todo.md 第四节全部 8 项。
        page=1 起步；page=0 回空列表；翻到头那页不足 15 条且 hasMore=false，
        再下一页回 0 条。

  GET /wapi/zpgeek/pc/recommend/job/list.json   → code 37「浏览器环境异常」
  POST /wapi/zpgeek/search/joblist.json         → code 37「浏览器环境异常」
        （带 ``__zp_stoken__`` 即可过；令牌按 :mod:`boss_jobs.stoken` 自动算）

special/zone 那条（推荐页 PageJobRecommend 在用的也是它）。

路由来自按需 chunk ``static.zhipin.com/zhipin-geek-spa/web/v6748/`` 的
``job-recommend-type.c303eca2.js``（``tb=(0,tc.iH)(v.zI)``，``zI`` 就是
``/wapi/zpgeek/pc/special/zone/joblist.json``）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import boss_db

BASE_URL: Final[str] = "https://www.zhipin.com"

#: 职位推荐页（``/web/geek/jobs``）的 Referer。接口不校验它，带上更像浏览器。
GEEK_JOBS_REFERER: Final[str] = f"{BASE_URL}/web/geek/jobs"

ENDPOINTS: Final[dict[str, str]] = {
    #: 唯一在用的分页职位列表（GET）。见文件头的实测记录。
    "job_list": "/wapi/zpgeek/pc/special/zone/joblist.json",
    #: 职位搜索列表（GET）。要登录 **且** 要 ``__zp_stoken__``（缺了回 code 37）。
    #: 令牌由 :class:`boss_jobs.cdp_stoken.CdpStokenProvider` 全自动补（拉 Chrome
    #: 让站点自己算），见 :func:`boss_jobs.client.JobClient.fetch_search_page`。
    "job_search": "/wapi/zpgeek/search/joblist.json",
    #: 职位详情（JD 正文）。**已实测**（2026-10-08）：要登录 **且** 要
    #: ``__zp_stoken__``（缺了回 code 37，跟搜索一样），query 带
    #: ``securityId`` + ``lid`` 两参就够。正文在 ``zpData.jobInfo.postDescription``。
    "job_detail": "/wapi/zpgeek/job/detail.json",
    #: 打招呼 / 加好友（POST，form）。**路径与参数形态已从站点前端 chunk 确认，
    #: 尚未实测**：query 带 ``securityId`` + ``jobId`` + ``lid``；body 是
    #: ``application/x-www-form-urlencoded``，透传 ``encryptBossId`` / ``sessionId``
    #: 等字段。见 :meth:`boss_jobs.client.JobClient.greet`。
    "friend_add": "/wapi/zpgeek/friend/add.json",
}

#: 推荐页 ``pageType`` 10/45 → type=1（全职流），36 → type=2（兼职流）
REQUEST_PARAMS: Final[dict[str, str]] = {"type": "1"}

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

#: 业务码「未登录 / 登录态失效」。与 :mod:`boss_filter.config` 同判据。
CODE_SESSION_EXPIRED: Final[frozenset[int]] = frozenset({1, 7})

#: 业务码「浏览器环境异常」——缺 ``__zp_stoken__`` 安全网关令牌。
#: 服务端会顺手在 ``zpData`` 里下发一次性挑战 ``{seed,name,ts}``，
#: 拿挑战去跑 ``/web/common/security-js/{name}.js`` 的 ``ABC.z`` 即可算出令牌，
#: 来源与算法见 :mod:`boss_jobs.stoken`。
CODE_BROWSER_CHECK: Final[int] = 37

#: 业务码「您的账户存在异常行为」——账号维度风控，要人机验证（GeeTest）。
#: 不是缺令牌，客户端不去绕。
CODE_RISK_CONTROL: Final[int] = 36

#: 安全网关令牌的 Cookie 名。**不是服务端发的**，是浏览器按
#: ``/web/common/security-js/{name}.js`` 的 ``ABC.z(seed, ts)`` 算出来再回传的，
#: 里面还编了浏览器环境指纹。取它最稳的路子是拉真 Chrome 让站点自己写
#: （:mod:`boss_jobs.cdp_stoken`，``fetch`` 默认走这条）；
#: :mod:`boss_jobs.stoken` 那条 Node 硬算只用来验证算法，服务端不认它的指纹。
STOKEN_COOKIE: Final[str] = "__zp_stoken__"

#: 环境变量兜底：想跳过自动计算、直接用浏览器里拷出来的令牌时设它。
STOKEN_ENV: Final[str] = "BOSS_ZP_STOKEN"

#: 单次请求超时（秒）
DEFAULT_TIMEOUT: Final[float] = 10.0

#: 网络层/5xx 重试次数
DEFAULT_RETRIES: Final[int] = 2

#: 重试退避基数（秒），按 2 的幂递增
DEFAULT_BACKOFF: Final[float] = 0.8

# --------------------------------------------------------------------------- #
# 分页与风控节流
# --------------------------------------------------------------------------- #

#: 服务端固定每页 15 条，传 pageSize 也不认。留作客户端切分/校验用。
PAGE_SIZE: Final[int] = 15

#: 翻页间隔（秒）。**每拿完一页就清洗入库，再睡这么久才要下一页**，
#: 把请求频率压到人手滚动的量级，避免触发风控。
DEFAULT_PAGE_INTERVAL: Final[float] = 1.0

#: 补抓职位详情（JD）的条间隔（秒）。
#: **1 秒**：实测 0.3s 会被安全网关当过频（连环 code 37），1s 是人手点击的量级。
DETAIL_INTERVAL: Final[float] = 1.0

#: 打招呼（发送）之间的条间隔（秒）。同 DETAIL_INTERVAL 一个道理：
#: 1s 是人手点「立即沟通」的节奏，既不像脚本刷屏，也不至于慢到没法用。
DELIVER_INTERVAL: Final[float] = 1.0

#: 打招呼请求 body 里放**招呼语正文**的字段名。**默认 ``None`` = 不发正文**。
#:
#: 本期只发标准打招呼（todo.md C6 已拍板）：正文字段名（``content`` /
#: ``greeting`` / ``sayHi``）没有从站点前端 chunk 里挖到，猜错了会被服务端忽略
#: 或者报错。招呼语照常生成 / 展示 / 手改 / 落库，只差把它塞进请求那一行。
#: 实测出正确字段名后，把这里改成字段名即升级为「带招呼语发送」。
GREETING_FIELD: Final[str | None] = None

#: 撞上安全网关 code 37 时先歇多久再拿同一枚令牌重试（秒）。
#: 37 有时只是「请求太快」，先退避；歇完还 37 才轮到强制换新（换新自己
#: 还有 ``RENEW_COOLDOWN`` 冷却，不会连环拉 Chrome）。
BROWSER_CHECK_BACKOFF: Final[float] = 2.0

#: 撞上安全网关 code 37 之后，下一条之前再多歇多久（秒）。
#: 37 有时是「令牌不对」，有时是**整段 IP / 会话被限速**——后者多打一发只会
#: 撞得更狠。失败后先躺平一会儿，再碰下一条。
BROWSER_CHECK_COOLOFF: Final[float] = 5.0

#: **连续**撞上 code 37 几次就停整批。
#: 实测（2026-10-08）：安全网关的限速墙是「一小段窗口里放行几发，然后整段拦」，
#: 撞墙后继续一条条砸只会把窗口越压越久。连环 3 次 = 这一轮大概率已经全线拦了，
#: 停批让人歇几分钟，比拿几十发请求去探墙厚道得多（也不容易把账号风控惹出来）。
BROWSER_CHECK_GIVEUP: Final[int] = 3

#: 默认最多翻几页。0 = 一直翻到接口回空页。
DEFAULT_MAX_PAGES: Final[int] = 0

# --------------------------------------------------------------------------- #
# 入库
# --------------------------------------------------------------------------- #

#: 项目根（``F:\boss``），跟 cwd 无关。
PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

#: 状态库路径。职位、登录态、筛选条件、stoken、LLM 配置、简历、分析全在这一份
#: ``data/boss.db`` 里（见 :mod:`boss_db`）。环境变量 ``BOSS_DB`` 可覆盖。
DEFAULT_DB_PATH: Final[Path] = boss_db.DEFAULT_DB_PATH

# --------------------------------------------------------------------------- #
# 薪资字体反混淆
# --------------------------------------------------------------------------- #

#: 列表页 HTML 里的薪资是私有区字符（防爬字体），映射见 chunk 的
#: ``S=["&#xe031;",…,"&#xe03a;"]`` + ``mixFont`` → 下标即数字 0-9。
#: **JSON 接口回的 salaryDesc 已经是明文**（"8-15K"），这表留给 HTML
#: 兜底解析和历史脏数据用。
FONT_DIGIT_ENTITIES: Final[tuple[str, ...]] = (
    "&#xe031;",
    "&#xe032;",
    "&#xe033;",
    "&#xe034;",
    "&#xe035;",
    "&#xe036;",
    "&#xe037;",
    "&#xe038;",
    "&#xe039;",
    "&#xe03a;",
)
#: 同一张表的字符形式（U+E031…U+E03A），下标即数字 0-9
FONT_DIGIT_CHARS: Final[tuple[str, ...]] = tuple(chr(0xE031 + i) for i in range(10))
