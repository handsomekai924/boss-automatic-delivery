# Python 文档字符串规范（Google 风格）

全仓 `*.py` 的说明性文字只保留 **docstring**。普通 `#` 注释一律删除（工具链注释除外）。

写 docstring 只描述三件事：**作用、入参、返回值**。不写协议逆向、历史背景、实测记录——那些进 `notes/<相对路径>/<文件名>.md`。

## 保留的注释（工具链）

以下注释**不删**：

- `#!` shebang
- `# pragma:`（含 `# pragma: no cover`）
- `# noqa`
- `# type: ignore` / `# type:` 其它
- `# fmt:`
- `# isort:`
- `# mypy:` / `# ruff:` / `# pyright:` / `# pylint:`
- coverage 相关注释

## 必须删除的注释

- 函数体内的实现注释、行尾注释
- `# --- 章节 ---` 分隔线及其标题
- `#:` Sphinx 属性注释 → 内容汇入 `Attributes:`，然后删掉
- 模块顶部的 `#` 横幅块 → 内容汇入模块 docstring 或 `notes/`，然后删掉

## 模块 docstring

```python
"""一句话说明本模块做什么。

（可选第二段：两行以内补充边界/调用关系，不写协议细节。）

Attributes:
    CONSTANT_NAME: 含义。
    ENDPOINTS: 键 → 含义表；只解释键名不自明的。
    ENDPOINTS["city"]: 字典项专属说明，仅在键名不自明时写。
"""
```

名字已自明的常量（`EXIT_OK = 0`、`DEFAULT_TIMEOUT = 10.0`）**不要**为它编 `Attributes:` 行。
只有原本带 `#:` 注释、或名字/类型说不清的常量才进 `Attributes:`。

## CLI 入口模块（例外）

`boss_filter/cli.py`、`boss_jobs/cli.py`、`boss_login/cli.py`、`tools/cli_commands.py`
的模块 docstring 会被 `epilog=__doc__` 直接渲染进 `--help`，且 `tools/cli_commands.py`
取首行当命令摘要。这四个**保持「一句话 + 缩进用法块」**，不写 `Args:`/`Returns:`：

```python
"""职位获取命令行入口。

    python -m boss_jobs fetch                      # 分页抓取，页间睡 1s
    python -m boss_jobs fetch --max-pages 3        # 只翻 3 页

登录态读状态库 doc('session')。
"""
```

首行必须是可独立阅读的一句话。用法块用**缩进**，不要写 RST 的 `::`。

## 类 docstring

参数写在**类**上，`__init__` 只写一行：

```python
class FilterClient:
    """筛选条件客户端。

    Args:
        base_url: 站点地址。
        timeout: 单次请求超时（秒）。
    """

    def __init__(self, base_url: str, timeout: float) -> None:
        """初始化。"""
```

dataclass / NamedTuple 用 `Attributes:` 不用 `Args:`：

```python
@dataclass(frozen=True)
class FilterOption:
    """一个可选筛选值。

    Attributes:
        code: 码。
        low_salary: 薪资档下限（K），非薪资档为 `None`。
    """
```

`Attributes:` 只列**原本有注释**、或**名字/类型说不清**的字段。字段多的 dataclass 不要给每个字段编一行。

## 函数 docstring

```python
def from_api(cls, item: dict[str, Any]) -> "FilterOption":
    """从接口的 `{code, name, lowSalary?, highSalary?}` 构造。

    Args:
        item: 接口返回的单条筛选项。

    Returns:
        构造好的筛选项。
    """
```

## 写法细则

1. 一句话能说清就**只写一行**（不带 `Args:`/`Returns:`）。
2. `Args:` 里省略类型——签名已有类型标注。`tests/` 里签名没标注的写 `name (int): 含义`。
3. `Returns:` 只在返回值含义不自明时写；`-> None` 不写；一句话已说明返回值的不写。
4. **不写** `Raises:` / `Examples:` / `Notes:` / `Yields:` / `Todo:` 段。
   确实会抛的异常并进第一句：`"""解析 payload；非 JSON 对象时抛 `ValueError`。"""`
5. 段落顺序：作用 → `Args:` → `Returns:`；类再加 `Attributes:`。
6. 去掉 Sphinx 标记，改成反引号：
   - `:class:\`Foo\`` → `` `Foo` ``
   - `:mod:\`a.b\`` → `` `a.b` ``
   - `:func:\`a.b\`` / `:meth:\`a.b\`` → `` `a.b` ``
   - `:data:\`X\`` / `:attr:\`X\`` → `` `X` ``
   - RST 双反引号 ` ``code`` ` → 单反引号 `` `code` ``
   - RST 字段列表 `:param x:` / `:returns:` / `:rtype:` / `:raises:` → `Args:` / `Returns:`（异常并进第一句）
   - RST 字面块标记 `用法::` / `程序里用::` → `用法：` / `程序里用：`
   - RST 加粗 `**强调**` → 纯文本
   - `:memory:` 这类**不是** RST role（是 SQLite URI），别改
7. `__init__` 只写 `"""初始化。"""`；`__str__`/`__repr__`/`__enter__`/`__iter__` 等 dunder 一行。
8. `@property` 一行说明计算出的值，无 `Args:`；非显然的补一行边界。
9. 私有函数、嵌套函数同样补一行。
10. `test_*` 写一行「验的是什么」；测试类 / fixture / `_make_*` 辅助一行；
    `@parametrize` 说共性不变式，不逐 case 写。
11. 语言：**中文**。argparse 的 `help=` / `description=` 字符串不动。
12. 多行 docstring：正文与 `"""` 之间空一行，收尾 `"""` 单独成行（PEP 257）。

## 过长内容的去处

被精简的内容若 ≥ 8 行协议 / 逆向 / 实测细节，先写进 `notes/<相对路径>/<文件名>.md`
再删（`boss_jobs/cdp_stoken.py` → `notes/boss_jobs/cdp_stoken.md`）。笔记模板：

```markdown
# `<package>.<module>` 协议笔记

> 摘自 `path/to/module.py` 的模块 docstring / docstring / `#` 注释。

## 一句话

…

## 细节

（原文照留，保留实测日期与结论）

### `ClassName.method_name`

（若细节只属于某个函数）
```

写完在 `notes/INDEX.md` 加一行：`| [文件](文件) | `pkg.module` | 一句话 |`。

## 边界情况裁决

| 情况 | 做法 |
|---|---|
| 字典字面量里的 `#:` | 汇入模块 `Attributes:` 写成 `ENDPOINTS["k"]: 说明。`；说明 > 2 行就抽进 `notes/` |
| dataclass 字段行尾注释 | 汇入类 `Attributes:`，删行尾注释；字段名自明的不编行 |
| 章节横幅的标题（`# --- 输出 ---`） | 一并删掉，语义由模块/类 docstring 承担 |
| 函数体内讲「为什么」的注释 | 删代码里的；若属协议知识 → 先进 `notes/` 再删 |
| `**kwargs` / `*args` | 函数签名里的，不是 RST 加粗，别动 |
| docstring 里出现的 `**` 是参数展开语法举例 | 保留原样 |

## 验收

```bash
python tools/docstring_audit.py     # hard=0；LONG_MODULE / LONG_DEF 只告警
python tools/comment_audit.py       # SPHINX_ATTR=0 BANNER=0 DELETE_ME=0
python tools/code_diff_check.py     # 去 docstring 后 AST 与 HEAD 一致
python -m pytest tests/ -q
```

`docstring_audit` 的 `CLI_EXEMPT` 豁免那四个 CLI 入口模块的模块 docstring 长度检查。
