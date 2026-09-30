"""``python -m boss_filter`` 入口。"""

import sys

from .cli import main


def _force_utf8_streams() -> None:
    """Windows 控制台默认 GBK，中文/符号会直接 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    _force_utf8_streams()
    sys.exit(main())
