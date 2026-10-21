"""证明改动「只动了文档注释」：去掉 docstring / 注释后与 HEAD 比对，只看代码 token。

Python 走 AST（剥掉 docstring 节点后 `ast.dump`）；JS / Vue 走字符串扫描（剥掉
`//` 与 `/* */`、压掉多余空白后逐字符比对）。两种都是「文档变了、代码没变」才算过。

用法：

    python tools/code_diff_check.py                 # 比对所有改动过的 .py / .js / .vue
    python tools/code_diff_check.py boss_web/static # 只比对指定路径

退出码 1 = 有文件的代码 token 变了（不只是文档）。
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PY_SUFFIX = ".py"
JS_SUFFIXES = {".js", ".mjs", ".vue"}


class StripDoc(ast.NodeTransformer):
    """把所有 docstring 节点剥掉，让对比只看代码。"""

    def _strip(self, node):
        """剥掉 `node` 体首的 docstring。

        Args:
            node: `Module` / `FunctionDef` / `AsyncFunctionDef` / `ClassDef`。

        Returns:
            处理完的节点。
        """
        self.generic_visit(node)
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
        return node

    visit_Module = visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _strip


def dump_py(src: str) -> str:
    """解析 Python 源码、剥 docstring，返回可比对的 AST 摘要。

    Args:
        src: Python 源码。

    Returns:
        不含位置信息的 `ast.dump` 字符串。
    """
    tree = StripDoc().visit(ast.parse(src))
    ast.fix_missing_locations(tree)
    return ast.dump(tree, include_attributes=False)


REGEX_PRECEDERS = frozenset("([{,;=:!&|?+-*%~^<>")
REGEX_KEYWORDS = frozenset(
    {"return", "typeof", "instanceof", "in", "of", "new", "delete", "void", "do", "else", "case", "yield", "await", "throw"}
)


def _regex_allowed(prev: str) -> bool:
    """判断当前 `/` 是不是正则字面量的开头。

    JS 里 `/` 既可能是除号也可能是正则，靠前一个有效 token 分辨：算子、关键字之后
    是正则，标识符 / 数字 / `)` / `]` 之后是除号。

    Args:
        prev: 前一个有效 token 的类型标记（`"ident"` / `"num"` / `")"` / 单字符算子…）。

    Returns:
        是正则开头为 `True`。
    """
    if not prev:
        return True
    if prev in REGEX_KEYWORDS:
        return True
    return len(prev) == 1 and prev in REGEX_PRECEDERS


def _read_regex(src: str, i: int) -> int:
    """从 `/` 开始吃掉一个正则字面量，返回结束后的下标。

    会跳过 `\\/`，并吃掉结尾的 flag 字母。

    Args:
        src: 源码。
        i: `/` 的下标。

    Returns:
        正则字面量之后的下标。
    """
    n = len(src)
    i += 1
    while i < n:
        c = src[i]
        if c == "\\":
            i += 2
            continue
        if c == "\n":
            return i
        if c == "[":
            i += 1
            while i < n and src[i] != "]":
                if src[i] == "\\":
                    i += 1
                i += 1
            i += 1
            continue
        if c == "/":
            i += 1
            break
        i += 1
    while i < n and src[i].isalpha():
        i += 1
    return i


def strip_js(src: str) -> str:
    """剥掉 JS 的注释与多余空白，只留代码 token。

    会跳过字符串、模板字面量与**正则字面量**——正则里可以有引号、`//`、`/*`，
    不跳过就会把后面的 JSDoc 误当字符串留下（`/"` 这种写法尤其常见）。

    Args:
        src: JS / Vue 源码。

    Returns:
        压成单行、只含代码的字符串。
    """
    out: list[str] = []
    prev = ""
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if c in " \t\r\n":
            i += 1
            continue
        if c == "/" and nxt == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if c == "/" and nxt == "*":
            i += 2
            while i < n and not (src[i] == "*" and i + 1 < n and src[i + 1] == "/"):
                i += 1
            i += 2
            continue
        if c == "/" and _regex_allowed(prev):
            start = i
            i = _read_regex(src, i)
            out.append(src[start:i])
            prev = "/"
            continue
        if c in "'\"":
            quote = c
            out.append(c)
            i += 1
            while i < n and src[i] != quote:
                if src[i] == "\\":
                    out.append(src[i])
                    i += 1
                    if i < n:
                        out.append(src[i])
                        i += 1
                    continue
                out.append(src[i])
                i += 1
            if i < n:
                out.append(quote)
                i += 1
            prev = quote
            continue
        if c == "`":
            out.append(c)
            i += 1
            depth = 0
            while i < n:
                ch = src[i]
                if ch == "\\":
                    out.append(ch)
                    i += 1
                    if i < n:
                        out.append(src[i])
                        i += 1
                    continue
                if depth == 0 and ch == "`":
                    out.append(ch)
                    i += 1
                    break
                if depth == 0 and ch == "$" and i + 1 < n and src[i + 1] == "{":
                    out.append(ch)
                    out.append(src[i + 1])
                    i += 2
                    depth = 1
                    continue
                if depth > 0:
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                out.append(ch)
                i += 1
            prev = "`"
            continue
        if c.isalpha() or c in "_$":
            j = i
            while j < n and (src[j].isalnum() or src[j] in "_$"):
                j += 1
            word = src[i:j]
            out.append(word)
            i = j
            prev = word if word in REGEX_KEYWORDS else "ident"
            continue
        if c.isdigit():
            j = i
            while j < n and (src[j].isalnum() or src[j] in "._"):
                j += 1
            out.append(src[i:j])
            i = j
            prev = "num"
            continue
        out.append(c)
        i += 1
        prev = c
    return re.sub(r"\s+", " ", "".join(out)).strip()


def changed_files(paths: list[str]) -> list[str]:
    """列出相对 HEAD 有改动的 `.py` / `.js` / `.mjs` / `.vue` 文件。

    Args:
        paths: 限定在这些路径下；为空则看全仓。

    Returns:
        仓库相对路径列表。
    """
    spec = ["*.py", "*.js", "*.mjs", "*.vue"]
    if paths:
        cmd = ["git", "diff", "--name-only", "HEAD", "--"] + [f"{p}/**/{s}" for p in paths for s in spec] + [
            f"{p}/{s}" for p in paths for s in spec
        ]
    else:
        cmd = ["git", "diff", "--name-only", "HEAD", "--"] + spec
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", cwd=ROOT)
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def main(argv: list[str]) -> int:
    """跑逐文件比对并打印报告。

    Args:
        argv: 限定路径。

    Returns:
        退出码；有代码 token 变化时为 1。
    """
    files = changed_files(argv)
    ok = bad = 0
    fails: list[str] = []
    for rel in files:
        path = ROOT / rel
        head = subprocess.run(
            ["git", "show", f"HEAD:{rel}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=ROOT,
        )
        if head.returncode != 0:
            print(f"NEW   {rel}  （HEAD 里没有，跳过）")
            continue
        try:
            src = path.read_text(encoding="utf-8")
            if path.suffix == PY_SUFFIX:
                same = dump_py(head.stdout) == dump_py(src)
            elif path.suffix in JS_SUFFIXES:
                same = strip_js(head.stdout) == strip_js(src)
            else:
                continue
        except SyntaxError as exc:
            print(f"SYNTAX  {rel}: {exc}")
            bad += 1
            fails.append(rel)
            continue
        if same:
            ok += 1
        else:
            bad += 1
            fails.append(rel)
            print(f"CODE_CHANGED  {rel}")
    print()
    print(f"files changed vs HEAD: {len(files)} | code_identical={ok} | code_changed={bad}")
    if fails:
        print("以下文件的代码 token 变了，不只是文档：")
        for rel in fails:
            print(f"  {rel}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
