"""网页控制台的路径与默认参数。"""

from __future__ import annotations

from pathlib import Path

#: 项目根目录（``F:\boss``），与 cwd 无关
PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 运行时数据（简历 / 分析结果 / LLM 配置）——gitignore 掉
DATA_DIR = PROJECT_ROOT / "data"
RESUMES_DIR = DATA_DIR / "resumes"
ANALYSES_DIR = DATA_DIR / "analyses"
LLM_CONFIG_PATH = DATA_DIR / "llm_config.json"

#: 静态资源
STATIC_DIR = Path(__file__).resolve().parent / "static"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787

#: 登录任务等验证码 / 滑块的人工上限（秒）
LOGIN_WAIT_TIMEOUT = 300.0

#: 简历分析时单次 LLM 调用之间的硬间隔（秒），压限流
ANALYZE_INTERVAL = 0.6

#: 分析批量默认看多少个职位
DEFAULT_ANALYZE_TOP_K = 20

# ---- LLM 系统固定参数（不开放给界面改）----
#: 采样温度
LLM_TEMPERATURE = 0.7
#: 单次回复 token 上限
LLM_MAX_TOKENS = 2048
#: 单次请求超时（秒）
LLM_TIMEOUT = 120.0


def ensure_data_dirs() -> None:
    """把运行时目录建出来（幂等）。"""
    for path in (DATA_DIR, RESUMES_DIR, ANALYSES_DIR):
        path.mkdir(parents=True, exist_ok=True)
