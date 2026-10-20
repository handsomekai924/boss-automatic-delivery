"""BOSS直聘职位分页获取：即时清洗 + 入库 + 翻页节流。

每条职位保留（岗位名称 / 公司名称 / 工作地点 / 薪资待遇 /
工作经验要求 / 学历要求 / 公司行业 / 公司规模）。清洗入库同步按页做，中途断了
已到手的页不会丢；页间默认硬睡 1 秒（`--interval` 可调）把频率压到人手滚动的量级。
登录态复用 `boss_login` 落盘的 `doc('session')`。

    python -m boss_jobs fetch                 # 翻页抓，每页洗完就入库
    python -m boss_jobs fetch --max-pages 3
    python -m boss_jobs stats
    python -m boss_jobs list --city 广州

    from boss_jobs import create_client, open_store
    with open_store() as store:               # 默认 data/boss.db
        report = client.crawl(store=store)    # 抓一页存一页，页间睡 1 秒
"""

from __future__ import annotations

from .client import (
    BossData,
    CrawlBlacklistContext,
    CrawlReport,
    CrawlStats,
    GreetResult,
    GreetingDelivery,
    JobClient,
    JobDetail,
    create_client,
    http_from_session,
)
from .chat import ChatCredentials, ChatSocket, encode_presence, encode_text_message
from .config import (
    BASE_URL,
    CHAT_TOPIC,
    CHAT_WS_HOST,
    CHAT_WS_PATH,
    CHAT_WS_PORT,
    CODE_BROWSER_CHECK,
    CODE_RISK_CONTROL,
    DEFAULT_DB_PATH,
    DEFAULT_MAX_PAGES,
    DEFAULT_PAGE_INTERVAL,
    DELIVER_INTERVAL,
    DETAIL_INTERVAL,
    ENDPOINTS,
    PAGE_SIZE,
    STOKEN_COOKIE,
    STOKEN_ENV,
)
from .errors import (
    ChatSendError,
    JobApiError,
    JobDataError,
    JobError,
    JobTransportError,
)
from .cdp_stoken import (
    CdpClient,
    CdpStokenProvider,
    StokenRecord,
    StokenStore,
    connect_or_launch,
    find_chrome,
)
from .models import (
    ExpectItem,
    ExpectList,
    Job,
    PageResult,
    clean_expects,
    clean_page,
    clean_pages,
    clean_salary,
    clean_text,
)
from .stoken import (
    StokenChallenge,
    StokenError,
    StokenProvider,
    compute_stoken,
    load_security_js,
    mint_offline,
    parse_challenge,
)
from .store import JobStore, SaveOutcome, open_store

__version__ = "0.1.0"

__all__ = [
    "JobClient",
    "create_client",
    "http_from_session",
    "CrawlBlacklistContext",
    "CrawlReport",
    "CrawlStats",
    "GreetResult",
    "BossData",
    "GreetingDelivery",
    "JobDetail",
    "ChatCredentials",
    "ChatSocket",
    "encode_presence",
    "encode_text_message",
    "CdpClient",
    "CdpStokenProvider",
    "StokenRecord",
    "StokenStore",
    "connect_or_launch",
    "find_chrome",
    "StokenChallenge",
    "StokenError",
    "StokenProvider",
    "compute_stoken",
    "load_security_js",
    "mint_offline",
    "parse_challenge",
    "Job",
    "PageResult",
    "ExpectItem",
    "ExpectList",
    "clean_expects",
    "clean_page",
    "clean_pages",
    "clean_salary",
    "clean_text",
    "JobStore",
    "SaveOutcome",
    "open_store",
    "ChatSendError",
    "JobApiError",
    "JobDataError",
    "JobError",
    "JobTransportError",
    "BASE_URL",
    "CHAT_TOPIC",
    "CHAT_WS_HOST",
    "CHAT_WS_PATH",
    "CHAT_WS_PORT",
    "CODE_BROWSER_CHECK",
    "CODE_RISK_CONTROL",
    "DEFAULT_DB_PATH",
    "DEFAULT_MAX_PAGES",
    "DEFAULT_PAGE_INTERVAL",
    "DELIVER_INTERVAL",
    "DETAIL_INTERVAL",
    "ENDPOINTS",
    "PAGE_SIZE",
    "STOKEN_COOKIE",
    "STOKEN_ENV",
    "__version__",
]
