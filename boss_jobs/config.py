"""职位获取相关接口地址、请求头、节流参数等常量。

列表路由在用的只有搜索流与求职期望流；采集来源二选一。
实测结论与路由演进见 `notes/boss_jobs/config.md`。

Attributes:
    GEEK_JOBS_REFERER: 所有请求的默认 Referer（求职页）；接口不校验，带上更像浏览器。
    ENDPOINTS: 接口路径表。
    ENDPOINTS["job_search"]: 职位搜索列表（GET）；要登录且要 `__zp_stoken__`。
    ENDPOINTS["job_expect_flow"]: 求职期望流列表（GET）；推荐/兼职/指定期望共用。
    ENDPOINTS["job_expect_list"]: 当前账号的求职期望元数据（GET）；只要登录 Cookie。
    ENDPOINTS["job_detail"]: 职位详情 / JD 正文（GET）；query 带 `securityId` + `lid`。
    ENDPOINTS["friend_add"]: 打招呼 / 加好友（POST，form）；只建会话，不投递正文。
    ENDPOINTS["get_wt"]: MQTT over WSS 接入凭据（GET）；`zpData.wt2` 当 MQTT 密码。
    ENDPOINTS["get_user_info"]: 当前登录用户（GET）；取 `zpData.token` 与 `userId`。
    ENDPOINTS["get_boss_data"]: 会话对象信息（GET）；取 boss 数字 uid 与 `bossSource`。
    ENDPOINTS["chat_history"]: 历史消息（GET）；当不了送达判据，只当探针。
    ENDPOINTS["chatremind_log"]: 「开聊提醒」弹窗埋点（POST，form）；要带 `zp_token` 头。
    DEFAULT_HEADERS: 默认请求头。
    CODE_SESSION_EXPIRED: 未登录 / 登录态失效业务码；code 1 是通用失败码，不算。
    CODE_BROWSER_CHECK: 浏览器环境异常业务码——缺 `__zp_stoken__` 安全网关令牌。
    CODE_RISK_CONTROL: 账号风控业务码；要人机验证，客户端不去绕。
    STOKEN_COOKIE: 安全网关令牌的 Cookie 名。
    ZP_TOKEN_COOKIE: `zp_token` 头对应的 Cookie 名（`bst`）。
    ZP_TOKEN_HEADER: 请求头名；部分确认类接口缺了回 code 121。
    CHAT_REMIND_CONFIRM_CID: 「开聊提醒」弹窗点「好」时前端回传的确认位。
    STOKEN_ENV: 环境变量兜底，可直接塞浏览器里拷出来的令牌。
    PAGE_SIZE: 服务端固定每页条数；传 pageSize 也不认，留作客户端切分/校验。
    DEFAULT_PAGE_INTERVAL: 列表页之间的抓取间隔（秒）。
    DETAIL_INTERVAL: 补抓职位详情的条间隔（秒）。
    DELIVER_INTERVAL: 打招呼之间的条间隔（秒）。
    CHAT_WS_HOST: 聊天 MQTT 网关主机（WSS）。
    CHAT_WS_PORT: 聊天 MQTT 网关端口。
    CHAT_WS_PATH: Paho 客户端构造里的 path。
    CHAT_TOPIC: 消息统一发到的主题，收件人在帧的 `to` 里。
    CHAT_KEEPALIVE: MQTT 心跳（秒）。
    CHAT_TIMEOUT: 等 CONNACK 的超时（秒）。
    CHAT_PUSH_WAIT: 连上后等服务端会话同步帧的超时（秒）；只在没 mid 基数时等。
    CHAT_MID_FLOOR: `mid` 基数兜底值；正常路径会被会话同步里的真实 id 顶上去。
    CHAT_MID_RANGE: 从推送里认「消息 id」的合理区间。
    CHAT_PUBACK_WAIT: 等 PUBACK 的超时（秒）；只记日志，默认 0 = 发完不等。
    CHAT_FLUSH_WAIT: 发完再停多久才主动断（秒）。
    CHAT_FLUSH_DEADLINE: 等 PUBLISH 真正写到 socket 的硬上限（秒）。
    CHAT_RETAIN: 正文帧的 MQTT `retain`；`False` 只走实时投递，避免重复投递。
    CHAT_RECONNECT_MIN: 断线重连间隔下限（秒）；故意开大，由显式重建连接。
    CHAT_RECONNECT_MAX: 断线重连间隔上限（秒）。
    CHAT_DEFAULT_SOURCE: 发消息时塞进 `from`/`to` 的用户 source。
    CHAT_BODY_TEXT: 文本消息的 `body.type` / `templateId`。
    CHAT_PROTO_MESSAGE: 协议帧类型：普通消息。
    CHAT_PROTO_PRESENCE: 协议帧类型：presence。
    CHAT_CLIENT_VERSION: 客户端版本号。
    CHAT_APP_ID: 客户端 appid。
    CHAT_PRESENCE_ONLINE: presence 帧的上线 `type`。
    BROWSER_CHECK_BACKOFF: 撞 code 37 后先退避再重试的秒数。
    BROWSER_CHECK_COOLOFF: 撞 code 37 后、下一条之前再多歇的秒数。
    BROWSER_CHECK_GIVEUP: 连续撞 code 37 几次就停整批。
    DEFAULT_MAX_PAGES: 默认最多翻几页；0 = 一直翻到接口回空页。
    PROJECT_ROOT: 项目根，跟 cwd 无关。
    DEFAULT_DB_PATH: 状态库路径；环境变量 `BOSS_DB` 可覆盖。
    FONT_DIGIT_ENTITIES: HTML 私有区数字的实体序列；下标即数字 0-9。
    FONT_DIGIT_CHARS: 同一张表的字符形式（U+E031…U+E03A），下标即数字 0-9。
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import boss_db

BASE_URL: Final[str] = "https://www.zhipin.com"

GEEK_JOBS_REFERER: Final[str] = f"{BASE_URL}/web/geek/jobs"

ENDPOINTS: Final[dict[str, str]] = {
    "job_search": "/wapi/zpgeek/search/joblist.json",
    "business_district": "/wapi/zpgeek/businessDistrict.json",
    "job_expect_flow": "/wapi/zpgeek/pc/recommend/job/list.json",
    "job_expect_list": "/wapi/zpgeek/pc/recommend/expect/list.json",
    "job_detail": "/wapi/zpgeek/job/detail.json",
    "friend_add": "/wapi/zpgeek/friend/add.json",
    "get_wt": "/wapi/zppassport/get/wt",
    "get_user_info": "/wapi/zpuser/wap/getUserInfo.json",
    "get_boss_data": "/wapi/zpchat/geek/getBossData",
    "chat_history": "/wapi/zpchat/geek/historyMsg",
    "chatremind_log": "/wapi/zpCommon/actionLog/geek/chatremind.json",
}

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

CODE_BROWSER_CHECK: Final[int] = 37

CODE_RISK_CONTROL: Final[int] = 36

STOKEN_COOKIE: Final[str] = "__zp_stoken__"

ZP_TOKEN_COOKIE: Final[str] = "bst"
ZP_TOKEN_HEADER: Final[str] = "zp_token"

CHAT_REMIND_CONFIRM_CID: Final[int] = 1

STOKEN_ENV: Final[str] = "BOSS_ZP_STOKEN"

DEFAULT_TIMEOUT: Final[float] = 10.0

DEFAULT_RETRIES: Final[int] = 2

DEFAULT_BACKOFF: Final[float] = 0.8


PAGE_SIZE: Final[int] = 15

DEFAULT_PAGE_INTERVAL: Final[float] = 1.0

DETAIL_INTERVAL: Final[float] = 1.0

DELIVER_INTERVAL: Final[float] = 1.0

CHAT_WS_HOST: Final[str] = "ws6.zhipin.com"
CHAT_WS_PORT: Final[int] = 443
CHAT_WS_PATH: Final[str] = "/chatws"
CHAT_TOPIC: Final[str] = "chat"

CHAT_KEEPALIVE: Final[int] = 25
CHAT_TIMEOUT: Final[float] = 15.0

CHAT_PUSH_WAIT: Final[float] = 4.0

CHAT_MID_FLOOR: Final[int] = 394_000_000_000_000

CHAT_MID_RANGE: Final[tuple[int, int]] = (10**13, 10**17)

CHAT_PUBACK_WAIT: Final[float] = 0.0

CHAT_FLUSH_WAIT: Final[float] = 0.15

CHAT_FLUSH_DEADLINE: Final[float] = 1.0

CHAT_RETAIN: Final[bool] = False

CHAT_RECONNECT_MIN: Final[int] = 30
CHAT_RECONNECT_MAX: Final[int] = 60

CHAT_DEFAULT_SOURCE: Final[int] = 0
CHAT_BODY_TEXT: Final[int] = 1
CHAT_PROTO_MESSAGE: Final[int] = 1
CHAT_PROTO_PRESENCE: Final[int] = 2
CHAT_CLIENT_VERSION: Final[str] = "4.92"
CHAT_APP_ID: Final[int] = 9019
CHAT_PRESENCE_ONLINE: Final[int] = 1

BROWSER_CHECK_BACKOFF: Final[float] = 2.0

BROWSER_CHECK_COOLOFF: Final[float] = 5.0

BROWSER_CHECK_GIVEUP: Final[int] = 3

DEFAULT_MAX_PAGES: Final[int] = 0


PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

DEFAULT_DB_PATH: Final[Path] = boss_db.DEFAULT_DB_PATH


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
FONT_DIGIT_CHARS: Final[tuple[str, ...]] = tuple(chr(0xE031 + i) for i in range(10))
