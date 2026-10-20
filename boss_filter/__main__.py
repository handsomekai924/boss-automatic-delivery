"""`python -m boss_filter` 入口。"""

import sys

from .cli import main


def _force_utf8_streams() -> None:
    """把标准流重配成 UTF-8，避免 Windows GBK 控制台编码报错。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    _force_utf8_streams()
    sys.exit(main())
