"""网页控制台的路径与默认参数。

状态（登录态 / 搜索条件 / stoken / LLM 配置 / 简历 / 分析 / 职位）全在
状态库 ``data/boss.db`` 里，见 :mod:`boss_db`；这里只留静态资源与运行参数。
"""

from __future__ import annotations

from pathlib import Path

#: 项目根目录（``F:\boss``），与 cwd 无关
PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 运行时数据目录（状态库 ``boss.db`` 所在处）——gitignore 掉
DATA_DIR = PROJECT_ROOT / "data"

#: 静态资源
STATIC_DIR = Path(__file__).resolve().parent / "static"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787

#: 登录任务等验证码 / 滑块的人工上限（秒）
LOGIN_WAIT_TIMEOUT = 300.0

#: 简历分析时单次 LLM 调用之间的硬间隔（秒），压限流
ANALYZE_INTERVAL = 0.6

#: 匹配页并行度：一键匹配 / 匹配选中时同时在途的 LLM 调用数。
#: 每条要等 LLM 好几秒，串行太慢；调小可缓解接口限流，调大更快但更容易撞 429。
MATCH_CONCURRENCY = 4

#: 分析批量默认看多少个职位
DEFAULT_ANALYZE_TOP_K = 20

#: 一键投递的默认评分阈值：匹配分 ≥ 它的岗位才进「一键发送全部」
#: （用户可在界面上改，改完落状态库 ``doc('deliver_config')``）
DEFAULT_DELIVER_MIN_SCORE = 70

# ---- LLM 系统固定参数（不开放给界面改）----
#: 采样温度
LLM_TEMPERATURE = 0.7
#: 单次回复 token 上限
LLM_MAX_TOKENS = 2048
#: 单次请求超时（秒）
LLM_TIMEOUT = 120.0
