"""检查 JS / Vue 的 JSDoc 覆盖与格式：每个命名函数都要有 JSDoc，正文注释该删没删。

用法：

    python tools/jsdoc_audit.py                   # 全仓
    python tools/jsdoc_audit.py boss_web/static   # 指定路径

退出码 1 = 有 MISSING / DELETE_ME / RST 残留；过长只告警。

检测的函数形态：`function` 声明（含嵌套、async、export）、`const f = () =>` /
`= function`、对象字面量的 `name: () =>` 与 `name() {}` 简写、类方法。
不要求给匿名内联回调（`.map(x => …)`、`addEventListener("t", () => …)`）写 JSDoc。

Attributes:
    KEEP_TOOL: 保留的工具链注释前缀。
    SKIP_DIRS: 不扫的目录。
    DEF_LONG: JSDoc 超过这么多行只告警。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = {".venv", "build", "dist", "__pycache__", ".saved_web", ".scratch", ".git", "node_modules", ".idea", "images", ".chrome_profile", "data", "docs", "notes", "tools/_scratch"}

EXEMPT_PREFIX = (
    "boss_jobs/assets/",
)

KEEP_TOOL = (
    "// eslint-",
    "/* eslint",
    "// prettier-",
    "/* prettier",
    "// @ts-",
    "/* @ts-",
    "/* istanbul",
    "// istanbul",
    "/* c8",
    "/* v8",
    "/* global",
    "/* exported",
    "/* jshint",
    "// jshint",
    "// #",
)

RST_FIELD = re.compile(r":(param|returns?|rtype|raises|type|ivar|cvar|vartype)\b")
RST_ROLE = re.compile(r":(class|func|meth|mod|attr|data|exc|obj|term|ref|doc):")
RST_BOLD = re.compile(r"\*\*[^*]+\*\*")
DBL_TICK = re.compile(r"``[^`]+``")

BANNER = re.compile(r"^//\s*[-=*_]{3,}")
FN_DECL = re.compile(r"^(?:export\s+)?(?:default\s+)?async\s+function\s*\*?\s*([A-Za-z_$][\w$]*)|^function\s*\*?\s*([A-Za-z_$][\w$]*)")
FN_BIND = re.compile(r"^(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(async\s*)?(\(|function\b)")
OBJ_FN = re.compile(r"^([A-Za-z_$][\w$]*)\s*:\s*(async\s*)?(\(|function\b)")
METHOD = re.compile(r"^(?:async\s+)?(?:get\s+|set\s+)?([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{")
CLASS_HEAD = re.compile(r"^(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)")

DEF_LONG = 20


def _arrow_after_paren(code: str, open_pos: int) -> bool:
    """判断 `(` 之后是不是箭头函数（配对括号后跟 `=>`）。

    用来区分 `const f = (a) => a` 和 `const x = (a || b)` 这两种写法。

    Args:
        code: 去掉字符串/注释后的单行代码。
        open_pos: `(` 的下标。

    Returns:
        是箭头函数为 `True`。
    """
    depth = 0
    i = open_pos
    n = len(code)
    while i < n:
        c = code[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                j = i + 1
                while j < n and code[j] in " \t":
                    j += 1
                return code[j : j + 2] == "=>"
        i += 1
    return False


def _is_block_body(code: str) -> bool:
    """判断这条函数是不是块体（`{ … }`），块体才强制要求 JSDoc。

    Args:
        code: 去掉字符串/注释后的单行代码。

    Returns:
        是块体为 `True`；单行表达式体（`=> expr,`）为 `False`。
    """
    return bool(re.search(r"\)\s*\{", code) or re.search(r"function\s*\w*\s*\([^)]*\)\s*\{", code))


REGEX_PRECEDERS = frozenset("([{,;=:!&|?+-*%~^<>")
REGEX_KEYWORDS = frozenset(
    {"return", "typeof", "instanceof", "in", "of", "new", "delete", "void", "do", "else", "case", "yield", "await", "throw"}
)


def _regex_allowed(prev: str) -> bool:
    """判断当前 `/` 是不是正则字面量的开头。

    用来分辨 `/` 是除号还是正则；正则里可以有引号与 `//`，不分辨就会把后面的
    JSDoc 误当字符串吃掉。

    Args:
        prev: 前一个有效 token 的类型标记。

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


def strip_code(src: str) -> str:
    """把字符串、注释与正则换成等长空白，保留换行，只留可匹配的代码骨架。

    这样正则不会命中字符串里的 `function` 或注释里的 `//`；也吃掉正则字面量，
    免得 `/"` 这类写法把状态机带偏。

    Args:
        src: JS 源码。

    Returns:
        与原文件行数相同、只含代码 token 的字符串。
    """
    out = list(src)
    prev = ""
    i = 0
    n = len(src)
    while i < n:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            while i < n and src[i] != "\n":
                out[i] = " "
                i += 1
        elif ch == "/" and nxt == "*":
            out[i] = out[i + 1] = " "
            i += 2
            while i < n and not (src[i] == "*" and i + 1 < n and src[i + 1] == "/"):
                if src[i] != "\n":
                    out[i] = " "
                i += 1
            if i < n:
                out[i] = " "
            if i + 1 < n:
                out[i + 1] = " "
            i += 2
        elif ch == "/" and _regex_allowed(prev):
            end = _read_regex(src, i)
            while i < end and i < n:
                if src[i] != "\n":
                    out[i] = " "
                i += 1
            prev = "/"
        elif ch in "'\"":
            quote = ch
            out[i] = " "
            i += 1
            while i < n and src[i] != quote:
                if src[i] == "\\":
                    out[i] = " "
                    i += 1
                    if i < n and src[i] != "\n":
                        out[i] = " "
                    i += 1
                    continue
                if src[i] != "\n":
                    out[i] = " "
                i += 1
            if i < n:
                out[i] = " "
                i += 1
            prev = quote
        elif ch == "`":
            out[i] = " "
            i += 1
            depth = 0
            while i < n:
                c = src[i]
                if c == "\\":
                    out[i] = " "
                    i += 1
                    if i < n and src[i] != "\n":
                        out[i] = " "
                    i += 1
                    continue
                if depth == 0 and c == "`":
                    out[i] = " "
                    i += 1
                    break
                if depth == 0 and c == "$" and i + 1 < n and src[i + 1] == "{":
                    out[i] = out[i + 1] = " "
                    i += 2
                    depth = 1
                    continue
                if depth > 0:
                    if c == "{":
                        depth += 1
                    elif c == "}":
                        depth -= 1
                if c != "\n":
                    out[i] = " "
                i += 1
            prev = "`"
        elif ch.isalpha() or ch in "_$":
            j = i
            while j < n and (src[j].isalnum() or src[j] in "_$"):
                j += 1
            word = src[i:j]
            prev = word if word in REGEX_KEYWORDS else "ident"
            i = j
        elif ch.isdigit():
            j = i
            while j < n and (src[j].isalnum() or src[j] in "._"):
                j += 1
            prev = "num"
            i = j
        else:
            prev = ch
            i += 1
    return "".join(out)


def iter_js(paths: list[str]) -> list[Path]:
    """列出要扫描的 `.js` / `.mjs` / `.vue` 文件。

    没给路径时优先用 `git ls-files`（只看入库文件），拿不到 git 再退回 glob。

    Args:
        paths: 显式给定的文件或目录；为空时扫全仓。

    Returns:
        排好序的文件列表。
    """
    exts = {".js", ".mjs", ".vue"}
    if not paths:
        try:
            import subprocess

            out = subprocess.run(
                ["git", "ls-files", "*.js", "*.mjs", "*.vue"],
                capture_output=True, text=True, encoding="utf-8", cwd=ROOT, check=True,
            ).stdout.split()
            return sorted({ROOT / p for p in out if Path(p).suffix in exts})
        except Exception:
            paths = []
    if not paths:
        base = [ROOT]
    else:
        base = [Path(p) for p in paths]
    found: set[Path] = set()
    for item in base:
        if item.is_file():
            if item.suffix in exts:
                found.add(item)
            continue
        for pat in ("**/*.js", "**/*.mjs", "**/*.vue"):
            for f in item.glob(pat):
                if any(part in SKIP_DIRS for part in f.parts):
                    continue
                found.add(f)
    return sorted(found)


def vue_scripts(src: str) -> list[tuple[int, str]]:
    """抽出 `.vue` 文件里的 `<script>` / `<script setup>` 段。

    Args:
        src: SFC 原文。

    Returns:
        `(起始行号, 段内容)` 列表；行号从 1 起，便于报错定位。
    """
    out: list[tuple[int, str]] = []
    for m in re.finditer(r"<script[^>]*>(.*?)</script>", src, re.S):
        body = m.group(1)
        start = src[: m.start(1)].count("\n") + 1
        out.append((start, body))
    return out


def jsdoc_before(lines: list[str], idx: int) -> tuple[str | None, int]:
    """取紧挨着 `idx` 行上方的 JSDoc 块。

    Args:
        lines: 文件行列表。
        idx: 函数所在行的下标（0 起）。

    Returns:
        `(JSDoc 正文, 起始行下标)`；上方没有 JSDoc 时为 `(None, -1)`。
    """
    j = idx - 1
    while j >= 0 and lines[j].strip() == "":
        j -= 1
    if j < 0 or not lines[j].strip().endswith("*/"):
        return None, -1
    end = j
    while j >= 0 and "/**" not in lines[j]:
        j -= 1
    if j < 0:
        return None, -1
    text = "\n".join(lines[j : end + 1])
    if not text.strip().startswith("/**"):
        return None, -1
    return text, j


def classify_comment(text: str) -> str:
    """给一条注释定类别。

    Args:
        text: 单条注释原文（含 `//` 或 `/*`）。

    Returns:
        `KEEP_TOOL` / `BANNER` / `DELETE_ME`。
    """
    stripped = text.strip()
    for pref in KEEP_TOOL:
        if stripped.startswith(pref):
            return "KEEP_TOOL"
    if BANNER.match(stripped) or (stripped.startswith("/*") and "*" * 3 in stripped.replace("/**", "").replace("*/", "")):
        return "BANNER"
    return "DELETE_ME"


def check_file(path: Path, rel: str, src: str, start_line: int) -> list[tuple[str, int, str]]:
    """检查一份 JS 源码的 JSDoc 覆盖与注释残留。

    Args:
        path: 文件路径（只用于报错显示）。
        rel: 相对仓库根的路径（只用于报错显示）。
        src: 要检查的 JS 源码。
        start_line: 源码在原文件里的起始行号（`.vue` 抽段时用）。

    Returns:
        `(类别, 行号, 说明)` 列表。
    """
    issues: list[tuple[str, int, str]] = []
    lines = src.splitlines()
    skeleton = strip_code(src).splitlines()

    for i, raw in enumerate(lines):
        s = raw.strip()
        if s.startswith("//") or (s.startswith("/*") and not s.startswith("/**")):
            kind = classify_comment(s)
            if kind != "KEEP_TOOL":
                issues.append((kind, start_line + i, s[:90]))
        if s.startswith("/**"):
            block = [raw]
            j = i
            while j + 1 < len(lines) and "*/" not in "\n".join(block):
                j += 1
                block.append(lines[j])
            body = "\n".join(block)
            if RST_FIELD.search(body) or RST_ROLE.search(body):
                issues.append(("RST_ROLE", start_line + i, body[:90]))
            if RST_BOLD.search(body.replace("```", "")):
                issues.append(("RST_BOLD", start_line + i, body[:90]))
            if DBL_TICK.search(body.replace("```", "")):
                issues.append(("DBL_TICK", start_line + i, body[:90]))

    def report_def(idx: int, name: str) -> None:
        """检查某个函数定义上方有没有 JSDoc，没有或过长就记一笔。

        Args:
            idx: 函数所在行的下标（0 起）。
            name: 函数名，用于报告。
        """
        doc, at = jsdoc_before(lines, idx)
        if doc is None:
            issues.append(("MISSING", start_line + idx, name))
            return
        n = doc.count("\n") + 1
        if n > DEF_LONG:
            issues.append(("LONG_DEF", start_line + at, f"{name}  {n} lines"))

    for i, sk in enumerate(skeleton):
        code = sk.strip()
        if not code:
            continue
        m = FN_DECL.match(code)
        if m:
            name = next(g for g in m.groups() if g)
            report_def(i, name)
            continue
        m = FN_BIND.match(code) or OBJ_FN.match(code)
        if m:
            name = m.group(1)
            if m.group(3) == "(":
                open_pos = m.start(3)
                if not _arrow_after_paren(code, open_pos):
                    continue
            if not _is_block_body(code):
                continue
            report_def(i, name)
            continue
        m = METHOD.match(code)
        if m:
            name = m.group(1)
            if name in {"if", "for", "while", "switch", "catch", "return", "do", "else"}:
                continue
            report_def(i, name)
            continue
        m = CLASS_HEAD.match(code)
        if m:
            report_def(i, m.group(1))

    return issues


def check_file_any(path: Path) -> list[tuple[str, int, str]]:
    """按扩展名检查一个文件（`.vue` 抽 script 段）。

    Args:
        path: 文件路径。

    Returns:
        `(类别, 行号, 说明)` 列表。
    """
    rel = path.relative_to(ROOT).as_posix() if path.is_absolute() and ROOT in path.parents else path.as_posix()
    src = path.read_text(encoding="utf-8")
    if path.suffix == ".vue":
        out: list[tuple[str, int, str]] = []
        segs = vue_scripts(src)
        if not segs:
            out.append(("NO_SCRIPT", 1, "SFC 里没有 <script> 段"))
        for start, body in segs:
            out.extend(check_file(path, rel, body, start))
        for i, raw in enumerate(src.splitlines(), 1):
            s = raw.strip()
            if s.startswith("<!--"):
                out.append(("DELETE_ME", i, s[:90]))
        return out
    return check_file(path, rel, src, 1)


def main(argv: list[str]) -> int:
    """跑检查并打印报告。

    Args:
        argv: 命令行参数（要扫描的路径）。

    Returns:
        退出码；有 hard 级问题时为 1。
    """
    files = iter_js(argv)
    counts: dict[str, int] = {}
    hard = 0
    scanned = 0
    HARD_KINDS = {"MISSING", "DELETE_ME", "BANNER", "RST_ROLE", "RST_BOLD", "DBL_TICK", "NO_SCRIPT"}
    for f in files:
        rel = f.relative_to(ROOT).as_posix() if ROOT in f.parents else f.as_posix()
        if any(rel.startswith(p) for p in EXEMPT_PREFIX):
            continue
        scanned += 1
        try:
            issues = check_file_any(f)
        except SyntaxError as exc:
            print(f"SYNTAX  {rel}: {exc}")
            hard += 1
            continue
        for kind, line, detail in issues:
            counts[kind] = counts.get(kind, 0) + 1
            tag = "HARD" if kind in HARD_KINDS else "warn"
            print(f"{tag}  {kind}  {rel}:{line}  {detail}")
            if kind in HARD_KINDS:
                hard += 1
    print()
    print(f"checked {scanned} files | hard={hard} | {counts}")
    return 1 if hard else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
