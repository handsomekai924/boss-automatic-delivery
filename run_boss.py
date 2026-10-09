"""PyInstaller 的入口脚本。

**必须在导入 ``boss_web.cli`` 之前跑 ``bootstrap()``**：它把状态库和 Chrome
profile 钉到 exe 同目录。晚一步，``boss_db`` 就会把库建到 PyInstaller 的临时
解包目录里——用户每关一次程序，简历和登录态就没了。

所以这里的导入顺序是刻意写成「先执行、后 import」的，别顺手把 import 提到
文件顶部去。（``boss_web`` 包本身也做了惰性导出配合这件事，见 ``__init__.py``。）
"""

from __future__ import annotations

import sys

from boss_web.runtime import bootstrap, force_utf8_streams

force_utf8_streams()
bootstrap()

from boss_web.cli import main  # noqa: E402 - 见模块 docstring：必须晚于 bootstrap()

if __name__ == "__main__":
    raise SystemExit(main())
