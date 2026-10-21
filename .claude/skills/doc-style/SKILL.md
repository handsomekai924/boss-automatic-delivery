---
name: doc-style
description: 把代码里的说明性文字改写成文档注释（Python 用 Google 风格 docstring，JS 用 JSDoc，Vue3 用 SFC 规范），删掉正文注释/章节横幅/Sphinx 属性注释，长协议细节抽进 notes/。当用户要「改注释」「补文档字符串」「写 JSDoc」「统一文档风格」「审计注释」，或新增 .py / .js / .vue 文件时使用。
---

# 文档注释改写

代码里的说明性文字只保留**文档注释**。Python 用 Google 风格 docstring，JS 用 JSDoc，Vue3 用 SFC 规范。正文注释一律删掉（工具链注释除外）；长协议/逆向/实测细节抽进 `notes/`。

## 先读规范

动手前先读对应语言的规范，**不要凭印象写**：

| 语言 | 规范 | 审计脚本 |
|---|---|---|
| Python `.py` | [python.md](python.md) | `python tools/docstring_audit.py` + `python tools/comment_audit.py` |
| JS `.js` / `.mjs` | [js.md](js.md) | `python tools/jsdoc_audit.py` |
| Vue3 `.vue` | [vue3.md](vue3.md) | `python tools/jsdoc_audit.py`（认 `<script>` 段） |

三份规范的共同底线：**只写作用、入参、返回值，不写一坨冗余信息**。协议逆向、历史背景、实测记录进 `notes/`，不进文档注释。

## 通用流程

对分到的**每个文件**走完这六步，不要跨文件跳步：

1. **抽长细节**：文档注释里 ≥ 8 行的协议 / 逆向 / 实测细节，先写进 `notes/<相对路径>/<文件名>.md`，再精简原文档注释到 1–3 行作用 + 入参/返回值。
2. **汇入属性**：`#:` 注释、字段行尾注释、JSDoc 里的 `@property` 散注释 → 并进类/接口的 `Attributes:` 或 `@property`，然后删原注释。
3. **补文档注释**：每个函数 / 类 / 方法都补上（含私有、嵌套、测试、fixture）。一句话说得清就只写一行。
4. **去 RST / 去 Sphinx**：`:param x:` / `:returns:` / `:rtype:` / `:raises:` → `Args:` / `Returns:`（JS 是 `@param` / `@returns`）；`:class:`Foo`` / ` ``code`` ` → 单反引号 `Foo` / `code`；`用法::` 的 `::` 去掉；`**强调**` → 纯文本。
5. **删正文注释**：函数体内的实现注释、行尾注释、`# --- 章节 ---` / `// ---------- 章节 ----------` 分隔线，全部删掉。工具链注释保留（清单见各语言规范）。
6. **逐文件跑审计**：对应语言的审计脚本对**这个文件**跑一遍，hard=0 才算完。

## 验收口径（改完必须全绿）

```bash
python tools/docstring_audit.py        # Python：hard=0，无 MISSING / RST 残留
python tools/comment_audit.py          # Python：hard=0，SPHINX_ATTR=0 BANNER=0 DELETE_ME=0
python tools/jsdoc_audit.py            # JS/Vue：hard=0，无 MISSING / DELETE_ME
python -m pytest tests/ -q             # 测试不红
```

**只动文档字符串和注释，不动代码 token。** 改完用 AST 对比证明（Python；JS 用 `node --check` + 肉眼抽查 `git diff`）：

```bash
python tools/code_diff_check.py        # 去掉文档注释后与 HEAD 的 AST 一致 = 只改了文档
```

## 并行分工纪律

多个 agent 同时改一个仓库时：

- **文件所有权严格不重叠**，每个 agent 只碰自己名单上的文件。
- `notes/INDEX.md` 只由汇总阶段写，中途不要抢。
- **不 commit、不 `git checkout`、不 `git reset`**。共享工作树里可能有别人未提交的改动。
- 读写一律 `encoding="utf-8"`，跑工具带 `PYTHONUTF8=1`（Windows 控制台默认 GBK，会把中文写坏）。

## 常见陷阱

| 陷阱 | 对策 |
|---|---|
| 字符串字面量里有 `#`、`"""`、`//`、`/*` | 删注释必须用 `tokenize`（Python）或字符串感知扫描（JS），不能按行正则 |
| `epilog=__doc__` 的 CLI 帮助被改坏 | `boss_filter/cli.py`、`boss_jobs/cli.py`、`boss_login/cli.py`、`tools/cli_commands.py` 的模块 docstring 是 `--help` 尾部，保持「一句话 + 缩进用法块」 |
| `tools/cli_commands.py` 取 docstring 首行当命令摘要 | 那四个模块首行必须是可独立阅读的一句话 |
| 字典字面量里的 `#:`（`ENDPOINTS = { #: ... }`） | 汇进模块 `Attributes:` 写成 `ENDPOINTS["k"]: 说明。` |
| 把函数体中部的字符串表达式当成 docstring | 只有 `def`/`class`/`async def` 紧跟的第一个字符串才是 docstring |
| 只删注释却顺手「优化」了 import | AST 对比会红；未使用的 import 是 HEAD 遗留，别动 |
