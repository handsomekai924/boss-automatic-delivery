"""``python -m boss_web`` 入口。"""

from __future__ import annotations

import sys

from .cli import main
from .runtime import force_utf8_streams

if __name__ == "__main__":
    force_utf8_streams()
    sys.exit(main())
