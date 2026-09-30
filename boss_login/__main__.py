"""``python -m boss_login`` 入口。"""

import sys

from .cli import main


def _force_utf8_streams() -> None:
    """Windows 控制台默认 GBK，打 ✓/✗ 这类符号会直接 UnicodeEncodeError。

    管道/重定向时更明显——Python 退回 locale 编码，一遇到非 GBK 字符就崩。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    _force_utf8_streams()
    sys.exit(main())
