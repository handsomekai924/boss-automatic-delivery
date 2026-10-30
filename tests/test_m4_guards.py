"""多站点收口的架构守卫（todo3 第八节成功标准 #3 / #4 / #7）。

这三条是**主张**，不是文档里的一句话——不钉死的话，接第二个站点时一定会被一点点
啃掉：多调一个站点方法（洞从 3 变 4）、新任务自造一个事件环、`core` 反过来 import
业务层。这里把它们变成会红的测试：

- #3 依赖无环：`core` 只 import 自己和标准库，内部还得无环；
- #4 模板方法洞数固定：`run_crawl` / `run_deliver` 的站点调用点**各恰好 3 个**，
  且只许在模板里出现；
- #7 任务运行时只有一份：`events` 事件环不许再增生，新增长任务复用 `task_runtime`。

#2（共享层不认站点协议）见 `test_core_boundaries.py`；#5（投递门槛是共享规则）
见 `test_core_flow.py` 的两条门槛用例；#1 / #8 是流程与工期主张，静态测不了，
得等第二站点立项后拿 `git diff` 验。
"""

from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
CORE = ROOT / "boss_web" / "core"
SERVICES = ROOT / "boss_web" / "services"

# ---------------------------------------------------------------------------
# #4 模板方法洞数固定
# ---------------------------------------------------------------------------

#: 每个模板方法的站点调用点，路径是「`adapter.` 之后的完整点串」。
#: 多一个少一个都是契约破坏——多一个 = 共享层开始夹带站点编排，少一个 = 洞没填。
TEMPLATE_HOLES: dict[str, frozenset[str]] = {
    "run_crawl": frozenset({
        "catalog.to_search_params",  # 通用条件 → 站点私有参数串
        "jobs.iter_pages",  # 翻页迭代（翻页策略是站点语义）
        "jobs.fetch_detail",  # 抓详情（形状站点私有）
    }),
    "run_deliver": frozenset({
        "messenger.open_session",  # 建会话（不发正文）
        "messenger.send_text",  # 发正文
        "applier.apply",  # 投简历
    }),
}

#: 共享规则的**输入**，不是洞：站点只是如实自述能力 / 回报会话阶段 / 归类故障，
#: 不许借它们夹带编排步骤。`run_deliver` 允许调这三个，且只许调这三个。
SHARED_INPUTS: frozenset[str] = frozenset({
    "applier.caps",
    "messenger.conversation_state",
    "classify_fault",
})


def _dotted(func: ast.Attribute) -> str | None:
    """把 `adapter.a.b(...)` 这样的调用拆成 `a.b`；根不是 `adapter` 回 `None`。"""
    parts: list[str] = []
    node: ast.AST = func
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name) or node.id != "adapter":
        return None
    parts.reverse()
    return ".".join(parts)


def _site_calls(func: ast.AST) -> set[str]:
    """某个函数体里对 `adapter.*` 的方法调用点（属性读取不算）。"""
    out: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            path = _dotted(node.func)
            if path:
                out.add(path)
    return out


def _template(name: str) -> ast.FunctionDef:
    """从 `core/flow.py` 里取模板方法；取不到就当场炸。"""
    tree = ast.parse((CORE / "flow.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"core/flow.py 里找不到模板方法 {name}（契约被删了？）")


def test_洞数固定_每个模板方法的站点调用点恰好三个():
    """成功标准 #4：`run_crawl` / `run_deliver` 各恰好 3 个站点调用点。

    数的是**洞**（`SHARED_INPUTS` 那三个共享判据不算，见下一条）。
    多出第 4 个洞 = 共享层开始夹带站点编排，第二站点就得跟着硬塞；
    少一个 = 契约被改了没人知道。
    """
    for name, want in TEMPLATE_HOLES.items():
        got = _site_calls(_template(name)) - SHARED_INPUTS
        assert got == set(want), (
            f"{name} 的站点调用点不是约定的那 3 个\n"
            f"  多出来：{sorted(got - set(want))}\n"
            f"  少了：{sorted(set(want) - got)}"
        )


def test_洞只在模板里_别处不许再有站点调用点():
    """成功标准 #4 的另一半：站点调用点只许住在模板方法体内。

    `_ref_of` 这类辅助只许**读** `adapter.site_id`，不许调站点方法——
    读属性是取站点身份，调方法就是新开一个洞。
    """
    tree = ast.parse((CORE / "flow.py").read_text(encoding="utf-8"))
    strays: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        path = _dotted(node.func)
        if not path:
            continue
        owner = _enclosing_function(tree, node)
        if owner in TEMPLATE_HOLES:
            continue
        strays.append(f"{owner or '<模块级>'} 调了 adapter.{path}")
    assert not strays, (
        "站点调用点跑到模板外面了（这些只许住在 run_crawl / run_deliver 里）：\n"
        + "\n".join(strays)
    )


def _enclosing_function(tree: ast.AST, target: ast.AST) -> str:
    """找 `target` 所在的最内层函数名；不在函数里回空串。"""
    best: str = ""
    stack: list[tuple[ast.AST, str]] = [(tree, "")]
    while stack:
        node, fname = stack.pop()
        here = node.name if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else fname
        if node is target:
            return here
        for child in ast.iter_child_nodes(node):
            stack.append((child, here))
    return ""


def test_共享规则的输入不是洞():
    """`caps` / `conversation_state` / `classify_fault` 是门槛与熔断的判据输入。

    它们在 `run_deliver` 里被调，但不算洞——站点只是如实自述，不能借它们夹带
    编排步骤。这条钉住区分：新增的站点调用要么进 `TEMPLATE_HOLES` 当第 4 个洞
    （先改契约再说），要么被 #4 的上一条逮住。
    """
    got = _site_calls(_template("run_deliver"))
    assert SHARED_INPUTS <= got, f"run_deliver 少调了共享判据：{sorted(SHARED_INPUTS - got)}"
    assert not (SHARED_INPUTS & TEMPLATE_HOLES["run_deliver"]), "洞与判据输入不许重叠"


# ---------------------------------------------------------------------------
# #3 依赖无环
# ---------------------------------------------------------------------------


def _core_modules() -> dict[str, pathlib.Path]:
    """`boss_web.core.*` 的模块名 → 源文件（`adapters/` 是站点实现，不在此列）。"""
    out: dict[str, pathlib.Path] = {}
    for path in sorted(CORE.rglob("*.py")):
        if "adapters" in path.parts or path.name == "__init__.py":
            continue
        rel = path.relative_to(ROOT).with_suffix("")
        out[".".join(rel.parts)] = path
    return out


def _imports_of(path: pathlib.Path, module: str) -> set[str]:
    """这个文件相对 import 到的兄弟模块名（只有 `boss_web.core.*` 算边）。"""
    package = module.rsplit(".", 1)[0]
    tree = ast.parse(path.read_text(encoding="utf-8"))
    edges: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level < 1:
            continue
        base = package
        for _ in range(node.level - 1):
            base = base.rsplit(".", 1)[0] if "." in base else ""
        target = f"{base}.{node.module}" if node.module else base
        if not target.startswith("boss_web.core"):
            continue
        edges.add(target)
        if node.module is None:  # `from . import a, b`
            for alias in node.names:
                edges.add(f"{base}.{alias.name}")
    return edges


def test_依赖无环_core只认自己():
    """成功标准 #3：`core` 内部 import 无环，对外只认标准库。

    一旦成环，todo3 Stage 1 的 `packages/core` 就拆不出去（拆包时循环依赖是硬墙）。
    """
    modules = _core_modules()
    assert modules, "一个 core 模块都没扫到，守卫等于没跑"
    graph = {
        name: {t for t in _imports_of(path, name) if t in modules and t != name}
        for name, path in modules.items()
    }
    cycles: list[str] = []
    state: dict[str, int] = {}  # 0=没走 1=在栈上 2=走完

    def walk(node: str, trail: list[str]) -> None:
        """DFS 找环，撞到栈上的就记一条。"""
        state[node] = 1
        for nxt in sorted(graph.get(node, ())):
            if state.get(nxt) == 1:
                cycles.append(" → ".join(trail + [node, nxt]))
            elif state.get(nxt, 0) == 0:
                walk(nxt, trail + [node])
        state[node] = 2

    for name in sorted(graph):
        if state.get(name, 0) == 0:
            walk(name, [])
    assert not cycles, "core 内部 import 成环了：\n" + "\n".join(cycles)


# ---------------------------------------------------------------------------
# #7 任务运行时只有一份
# ---------------------------------------------------------------------------

#: 现存的 `events` 事件环持有者。**这个集合是历史债，不是范本**——
#: 新增长任务不许再往里加名字，事件环该从 `task_runtime` 拿。
EVENT_DEQUE_OWNERS: frozenset[str] = frozenset({
    "blacklist_recheck",
    "crawl_task",
    "deliver_task",
    "desc_task",
    "interview_generation_task",
    "login_task",
    "match_task",
    "parse_task",
    "resume_revision_task",
})


def _events_owner(path: pathlib.Path) -> bool:
    """这个文件有没有自己持有一个 `events` 事件环（认 `deque` 注解/构造）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        names: list[ast.AST] = []
        anno: ast.AST | None = None
        if isinstance(node, ast.AnnAssign):
            names, anno = [node.target], node.annotation
        elif isinstance(node, ast.Assign):
            names, anno = list(node.targets), None
        else:
            continue
        hit = any(getattr(t, "id", getattr(t, "attr", "")) == "events" for t in names)
        if not hit:
            continue
        text = ast.unparse(anno) if anno is not None else ""
        if "deque" in text:
            return True
    return False


def test_事件环不再增生():
    """成功标准 #7：长任务的 `events` 事件环只有现存这些，不许再加。

    新增长任务该复用 `task_runtime` 的登记/广播，不是再抄一份
    `self.events: deque(maxlen=…)`。这里钉死集合：加名字就红，
    到时候要么说明白为什么非得自造，要么改成复用。
    """
    found = {p.stem for p in SERVICES.glob("*.py") if _events_owner(p)}
    added = found - EVENT_DEQUE_OWNERS
    removed = EVENT_DEQUE_OWNERS - found
    assert not added, (
        "又冒出新的事件环了——新增长任务复用 `task_runtime`，别自己造：\n"
        + "\n".join(sorted(added))
    )
    assert not removed, f"这些模块已经不持事件环了，从钉住的名单里摘掉：{sorted(removed)}"


def test_钉住的名单不是空的():
    """名单空了守卫就成摆设，先钉住它。"""
    assert len(EVENT_DEQUE_OWNERS) >= 5
    assert len(TEMPLATE_HOLES["run_crawl"]) == 3
    assert len(TEMPLATE_HOLES["run_deliver"]) == 3
    assert not (SHARED_INPUTS & TEMPLATE_HOLES["run_deliver"])


# ---------------------------------------------------------------------------
# 别名收口：后端不许再往回发 legacy 裸路径
# ---------------------------------------------------------------------------

#: 平台作用域资源的前缀。裸的 `/api/<前缀>/…` 是 legacy 专用兼容别名，
#: 由 `api/__init__.py` 双挂载；**后端自报的 URL**（滑块帮助页的 `post_url`
#: / `page_url`、二维码图）只许是 `/api/sites/{site}/…`，别名一摘就断在半路。
ALIAS_PREFIXES: tuple[str, ...] = (
    "/api/auth",
    "/api/filters",
    "/api/jobs",
    "/api/crawl",
    "/api/collection",
    "/api/deliver",
    "/api/settings",
)


def _backend_files() -> list[pathlib.Path]:
    """`boss_web/` 下的源文件（`static/` 是前端产物，不在此列）。"""
    return sorted(
        p for p in (ROOT / "boss_web").rglob("*.py") if "static" not in p.parts
    )


def _string_literals(path: pathlib.Path) -> list[str]:
    """源码里代码用到的字符串字面量（含 f-string 的常数段），docstring 不算。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = getattr(node, "body", [])
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            if isinstance(body[0].value.value, str):
                skip.add(body[0].value.lineno)
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.lineno not in skip
    ]


def _is_alias(text: str) -> bool:
    """这个字符串里有没有 legacy 裸路径；站点作用域的不算。"""
    return "/api/sites/" not in text and any(p in text for p in ALIAS_PREFIXES)


def test_后端不往回发legacy裸路径():
    """别名是给 legacy SPA 的，后端自己吐的 URL 一律站点作用域。

    这条是**踩过才加的**：`login_task` 把 `/api/auth/slider/…` 写进滑块帮助页的
    `post_url`，前端开窗那条却是 `/api/sites/boss/auth/slider/…`——两个口径，
    别名一摘帮助页就再也提交不了票据。加了这条之后再有人图省事写裸路径会当场红。
    """
    leaks: list[str] = []
    for path in _backend_files():
        for text in _string_literals(path):
            if _is_alias(text):
                leaks.append(f"{path.relative_to(ROOT)}：{text!r}")
    assert not leaks, (
        "后端在往回发 legacy 裸路径（只许 `/api/sites/{site}/…`）：\n" + "\n".join(leaks)
    )


def test_扫描器认得出别名路径(tmp_path):
    """自检：站点作用域不算漏，裸别名才算；f-string 也逃不掉。"""
    path = tmp_path / "_selfcheck.py"
    path.write_text(
        'A = "/api/auth/slider/t1/solution"\n'
        'B = "/api/sites/boss/auth/slider/t1/solution"\n'
        'C = f"/api/auth/qr/{tid}/image"\n',
        encoding="utf-8",
    )
    literals = _string_literals(path)
    assert _is_alias("/api/auth/slider/t1/solution") is True
    assert _is_alias("/api/sites/boss/auth/slider/t1/solution") is False
    assert any("/api/auth/qr" in t and _is_alias(t) for t in literals), f"f-string 没被逮住：{literals}"
    assert not any("/api/sites/boss" in t and _is_alias(t) for t in literals)


def test_扫描器认得出站点调用点(tmp_path):
    """自检：只认 `adapter.` 开头的调用，别家同名方法不误报。"""
    path = tmp_path / "_selfcheck.py"
    path.write_text(
        "def run(adapter, other):\n"
        "    adapter.catalog.to_search_params(c)\n"
        "    adapter.jobs.iter_pages(p)\n"
        "    other.catalog.to_search_params(c)\n"  # 不是 adapter，不数
        "    adapter.site_id\n"  # 属性读取不是调用点
        "    return adapter.applier.caps()\n",
        encoding="utf-8",
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    got = _site_calls(tree.body[0])
    assert got == {
        "catalog.to_search_params",
        "jobs.iter_pages",
        "applier.caps",
    }


def test_扫描器认得出事件环(tmp_path):
    """自检：`events` 要真是 `deque` 才算；换个同名字段不误报。"""
    yes = tmp_path / "_yes.py"
    yes.write_text("class T:\n    events: deque[dict] = deque(maxlen=5)\n", encoding="utf-8")
    assert _events_owner(yes) is True
    no = tmp_path / "_no.py"
    no.write_text("class T:\n    events: int = 0\n", encoding="utf-8")
    assert _events_owner(no) is False
