"""按 tokenize 分类注释：只留工具链注释，其余应已删除。

用法::

    python tools/comment_audit.py                  # 全仓
    python tools/comment_audit.py boss_jobs        # 指定路径

退出码 1 = 还有 SPHINX_ATTR / BANNER / DELETE_ME 该删没删。
"""

from __future__ import annotations

import io
import json
import sys
import tokenize
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = {".venv", "build", "dist", "__pycache__", ".saved_web", ".scratch", ".git", "node_modules", ".idea"}

TOOL_PREFIXES = (
    "# pragma:",
    "# pragma:no",
    "# noqa",
    "# type:",
    "# fmt:",
    "# isort:",
    "# mypy:",
    "# ruff:",
    "# pyright:",
    "# pylint:",
    "# coverage",
    "# pyright:",
)


def classify(text: str) -> str:
    """给一条注释定类别。

    Args:
        text: 单个 COMMENT token 的原文（含 `#`）。

    Returns:
        `SHEBANG` / `KEEP_TOOL` / `SPHINX_ATTR` / `BANNER` / `DELETE_ME` 之一。
    """
    s = text.strip()
    if s.startswith("#!"):
        return "SHEBANG"
    low = s.lower()
    if any(low.startswith(p) for p in TOOL_PREFIXES):
        return "KEEP_TOOL"
    if s == "#:" or s.startswith("#:"):
        return "SPHINX_ATTR"
    body = s.lstrip("#").strip()
    if body and (body.count("-") >= 5 or body.count("=") >= 5):
        return "BANNER"
    return "DELETE_ME"


def iter_py(paths: list[str]) -> list[Path]:
    """列出要扫描的 `.py` 文件。

    Args:
        paths: 显式给定的文件或目录；为空时扫全仓。

    Returns:
        待扫描文件列表。
    """
    if paths:
        out = []
        for p in paths:
            q = Path(p)
            if q.is_dir():
                out.extend(sorted(q.rglob("*.py")))
            else:
                out.append(q)
        return out
    out = []
    for p in sorted(ROOT.rglob("*.py")):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        out.append(p)
    return out


def rel(p: Path) -> str:
    """转成相对仓库根的 POSIX 路径。

    Args:
        p: 待转换的路径。

    Returns:
        仓库内相对路径；不在仓库内时返回原路径。
    """
    try:
        return p.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return p.as_posix()


def main(argv: list[str]) -> int:
    """分类所有注释并汇总违规项。

    Args:
        argv: 要扫描的文件或目录；为空时扫全仓。

    Returns:
        存在该删未删的注释或读取/词法失败时为 1，否则为 0。
    """
    total: Counter[str] = Counter()
    offenders: list[str] = []
    files = iter_py(argv)
    for p in files:
        try:
            src = p.read_text(encoding="utf-8")
        except OSError as exc:
            offenders.append(f"READ_FAIL  {rel(p)}  {exc}")
            continue
        per: Counter[str] = Counter()
        try:
            for tok in tokenize.generate_tokens(io.StringIO(src).readline):
                if tok.type != tokenize.COMMENT:
                    continue
                kind = classify(tok.string)
                per[kind] += 1
                if kind in {"SPHINX_ATTR", "BANNER", "DELETE_ME"}:
                    offenders.append(f"{kind}  {rel(p)}:{tok.start[0]}  {tok.string.strip()[:70]}")
        except (tokenize.TokenError, IndentationError) as exc:
            offenders.append(f"TOKFAIL  {rel(p)}  {exc}")
            continue
        total.update(per)

    for line in offenders:
        print(line)
    print(f"\nfiles={len(files)} totals={dict(total)}")
    hard = total.get("SPHINX_ATTR", 0) + total.get("BANNER", 0) + total.get("DELETE_ME", 0)
    print(f"hard={hard}")
    print(json.dumps(dict(total), ensure_ascii=False))
    return 1 if (hard or any(o.startswith(("READ_FAIL", "TOKFAIL")) for o in offenders)) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
