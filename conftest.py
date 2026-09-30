"""pytest 根配置。

放在项目根目录，使 `boss_login` 包在测试时可直接导入（无需安装为包）。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
