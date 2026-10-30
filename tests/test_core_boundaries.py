"""共享层的边界守卫（todo3 第三节：依赖方向、import 禁令）。

`boss_web/core/` 是多平台抽象的内核，`boss_web/core/adapters/` 才是站点实现。
两者之间这条线必须守住：站点私有语义（BOSS 的 code 36 / 开聊提醒 /
`__zp_stoken__` 这些）只能留在 adapter 与站点任务里，**不许渗进共享层的代码
字符串**——一旦渗进来，接第二个站点就会把 BOSS 的风控文案原样甩给别家用户。

扫的是 `ast.Constant` 且跳过 docstring：docstring 里提这些词是文档（写明边界），
代码字符串里冒出来才是漏。同理 import 禁令只盯 `boss_*` 与 server 的业务层，
内核不许反向依赖它们。
"""

from __future__ import annotations

import ast
import pathlib

CORE = pathlib.Path(__file__).resolve().parent.parent / "boss_web" / "core"

#: 站点私有语义的指纹；共享层的代码字符串里一个都不该出现。
SITE_PRIVATE_WORDS: tuple[str, ...] = (
    "开聊提醒",  # BOSS 每日沟通配额的专属提醒
    "每日沟通",  # 同上，BOSS 的配额语义
    "code 36",  # BOSS 账号风控
    "code 37",  # BOSS 缺 __zp_stoken__ 的安全网关
    "安全网关",
    "__zp_stoken__",
    "stoken",
    "zhipin",
    "BOSS直聘",
    "求职期望",  # BOSS 私有检索路径
    "encrypt_job_id",
    "securityId",
    "security_id",
)

#: 内核不许反向依赖的模块。
FORBIDDEN_IMPORTS: tuple[str, ...] = (
    "boss_jobs",
    "boss_login",
    "boss_filter",
    "boss_db",
    "boss_web.services",
    "boss_web.api",
    "boss_web.app",
    "boss_web.cli",
)


def _shared_files() -> list[pathlib.Path]:
    """共享层的源文件：`core/` 下面除了 `adapters/` 的全部 `.py`。"""
    return sorted(p for p in CORE.rglob("*.py") if "adapters" not in p.parts)


def _docstring_lines(tree: ast.AST) -> set[int]:
    """模块/类/函数 docstring 所在行号（这些行的字符串是文档，不算代码文案）。"""
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        body = getattr(node, "body", [])
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            lines.add(first.value.lineno)
    return lines


def _code_strings(path: pathlib.Path) -> list[str]:
    """源码里**代码用到**的字符串字面量，docstring 与注释不算。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = _docstring_lines(tree)
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.lineno not in skip
    ]


def test_共享层不掺站点私有语义():
    """BOSS 的风控文案/私有字段名只能留在 adapter 与站点任务里。

    这条守卫是**活的**：`core/limits.py` 曾把「安全网关」写进熔断的停批原因，
    就是它逮住的。
    """
    files = _shared_files()
    assert files, "共享层一个文件都没扫到，守卫等于没跑"
    leaks: list[str] = []
    for path in files:
        for text in _code_strings(path):
            for word in SITE_PRIVATE_WORDS:
                if word in text:
                    leaks.append(f"{path.name}：{word!r} 出现在代码字符串 {text!r}")
    assert not leaks, "站点私有语义渗进共享层了：\n" + "\n".join(leaks)


def test_共享层零依赖():
    """内核只依赖标准库和自己，不许 import `boss_*` 或 server 业务层。

    依赖方向一旦倒过来，`packages/core`（todo3 Stage 1）就拆不出去。
    """
    for path in _shared_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                for banned in FORBIDDEN_IMPORTS:
                    assert not name.startswith(banned), (
                        f"{path.name} 反向依赖了 {name}（禁令：{banned}）"
                    )


def test_守卫盯的词表不是空的():
    """词表空了守卫就成摆设，先钉住它。"""
    assert len(SITE_PRIVATE_WORDS) >= 10
    assert all(SITE_PRIVATE_WORDS), "词表里不许有空串（空串什么都命中，等于把测试搞黄）"


def test_扫描器认得出docstring和代码字符串(tmp_path):
    """自检：docstring 里的词不算漏，代码里的才算。"""
    path = tmp_path / "_selfcheck.py"
    path.write_text('"""开聊提醒是 BOSS 私有语义。"""\nMSG = "开聊提醒"\n', encoding="utf-8")
    assert _code_strings(path) == ["开聊提醒"]
