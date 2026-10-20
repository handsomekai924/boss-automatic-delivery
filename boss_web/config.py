"""网页控制台的路径与默认参数。

状态全在状态库 `data/boss.db`（见 `boss_db`），这里只留静态资源与运行参数。

Attributes:
    PROJECT_ROOT: 项目根目录，与 cwd 无关；打包成 exe 后指向 PyInstaller 解包目录。
    DATA_DIR: 运行时数据目录（状态库 `boss.db` 所在处）；开发态仓库 `data/`，打包态 exe 同目录。
    STATIC_DIR: 静态资源目录；dev = 仓库 `boss_web/static`，打包 = 解包目录同名路径。
    LOGIN_WAIT_TIMEOUT: 登录任务等验证码 / 滑块的人工上限（秒）。
    ANALYZE_INTERVAL: 简历分析时单次 LLM 调用之间的硬间隔（秒），压限流。
    MATCH_CONCURRENCY: 匹配页并行度（同时在途的 LLM 调用数）；调小缓解限流，调大易撞 429。
    DEFAULT_ANALYZE_TOP_K: 分析批量默认看多少个职位。
    DEFAULT_DELIVER_MIN_SCORE: 一键投递默认评分阈值；可在界面改，落 `doc('deliver_config')`。
    LLM_TEMPERATURE: 采样温度。
    LLM_MAX_TOKENS: 通用单次回复 token 上限（招呼语等）。
    LLM_RESUME_PARSE_MAX_TOKENS: 简历结构化解析单次回复 token 上限。
    LLM_RESUME_PARSE_RETRY_MAX_TOKENS: 简历输出被截断时唯一一次重试的 token 上限。
    LLM_MATCH_MAX_TOKENS: 人岗匹配单次回复 token 上限。
    LLM_TIMEOUT: 单次请求超时（秒）。
"""

from __future__ import annotations

from .runtime import app_data_dir, resource_root

PROJECT_ROOT = resource_root()

DATA_DIR = app_data_dir()

STATIC_DIR = PROJECT_ROOT / "boss_web" / "static"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787

LOGIN_WAIT_TIMEOUT = 300.0

ANALYZE_INTERVAL = 0.6

MATCH_CONCURRENCY = 4

DEFAULT_ANALYZE_TOP_K = 20

DEFAULT_DELIVER_MIN_SCORE = 70

LLM_TEMPERATURE = 0.7
LLM_MAX_TOKENS = 2048
LLM_RESUME_PARSE_MAX_TOKENS = 8192
LLM_RESUME_PARSE_RETRY_MAX_TOKENS = 16384
LLM_MATCH_MAX_TOKENS = 4096
LLM_TIMEOUT = 120.0
