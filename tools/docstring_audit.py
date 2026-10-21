"""AST 检查：每个函数/类都要有 Google 风格文档字符串，且不残留 RST 标记。

用法：

    python tools/docstring_audit.py                # 全仓
    python tools/docstring_audit.py boss_jobs      # 指定路径

退出码 1 = 有 MISSING 或 RST 残留；过长只告警。

Attributes:
    CLI_EXEMPT: 免检模块文档字符串长度的路径；这些模块的文档字符串就是 CLI 帮助正文。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = {".venv", "build", "dist", "__pycache__", ".saved_web", ".scratch", ".git", "node_modules", ".idea"}

CLI_EXEMPT = {
    "boss_filter/cli.py",
    "boss_jobs/cli.py",
    "boss_login/cli.py",
    "tools/cli_commands.py",
}

RST_FIELD = re.compile(r":(param|returns?|rtype|raises|type|ivar|cvar|vartype)\b")
RST_ROLE = re.compile(r":(class|func|meth|mod|attr|data|exc|obj|term|ref|doc):")

MODULE_LONG = 20
DEF_LONG = 15


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


def check_doc(node: ast.AST, name: str, kind: str, path: str, exempt: bool, out: list[str]) -> None:
    """检查单个节点的文档字符串，违规写入 `out`。

    Args:
        node: 模块、类或函数节点。
        name: 展示名（模块用路径，其它用标识符）。
        kind: 节点类别，取 `module` / `class` / `def`。
        path: 所属文件的仓库相对路径。
        exempt: 是否豁免模块文档字符串长度检查。
        out: 违规行收集列表。
    """
    doc = ast.get_docstring(node)
    if doc is None:
        out.append(f"MISSING  {path}  {kind} {name}")
        return
    if RST_FIELD.search(doc):
        out.append(f"RST_FIELD  {path}  {kind} {name}")
    if RST_ROLE.search(doc):
        out.append(f"RST_ROLE  {path}  {kind} {name}")
    n = len(doc.splitlines())
    if kind == "module":
        if not exempt and n > MODULE_LONG:
            out.append(f"LONG_MODULE  {path}  {n} lines")
    elif n > DEF_LONG:
        out.append(f"LONG_DEF  {path}  {kind} {name}  {n} lines")


def main(argv: list[str]) -> int:
    """扫给定路径（缺省全仓）并打印违规汇总。

    Args:
        argv: 要扫描的文件或目录；为空时扫全仓。

    Returns:
        存在 MISSING / RST 残留 / 语法错误时为 1，否则为 0。
    """
    out: list[str] = []
    files = iter_py(argv)
    for p in files:
        try:
            src = p.read_text(encoding="utf-8")
        except OSError as exc:
            out.append(f"READ_FAIL  {rel(p)}  {exc}")
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError as exc:
            out.append(f"SYNTAX  {rel(p)}  {exc}")
            continue
        path = rel(p)
        exempt = path in CLI_EXEMPT
        check_doc(tree, path, "module", path, exempt, out)
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                check_doc(n, n.name, "def", path, exempt, out)
            elif isinstance(n, ast.ClassDef):
                check_doc(n, n.name, "class", path, exempt, out)

    hard = [l for l in out if l.split()[0] in {"MISSING", "RST_FIELD", "RST_ROLE", "SYNTAX", "READ_FAIL"}]
    soft = [l for l in out if l not in hard]
    for line in hard:
        print(line)
    if soft:
        print(f"--- {len(soft)} warnings ---")
        for line in soft:
            print(line)
    print(f"\nchecked {len(files)} files | hard={len(hard)} warn={len(soft)}")
    return 1 if hard else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
