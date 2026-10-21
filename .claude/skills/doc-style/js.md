# JS 文档注释规范（JSDoc）

全仓 `*.js` / `*.mjs` 的说明性文字只保留 **JSDoc**（`/** … */`）。行注释 `//` 一律删除（工具链注释除外）。

**例外**：`boss_jobs/assets/` 下的逆向/第三方脚本（如 `run_abc.js`）**不动**——那是抓来的资产，不是我们写的代码；`jsdoc_audit` 也豁免它们。

写 JSDoc 只描述三件事：**作用、入参、返回值**。协议逆向、历史背景、实测记录进 `notes/<相对路径>/<文件名>.md`。

## 保留的注释（工具链）

以下注释**不删**：

- `// eslint-disable` / `// eslint-enable` / `/* eslint … */`
- `// prettier-ignore`
- `// @ts-check` / `// @ts-expect-error` / `// @ts-ignore` / `// @ts-nocheck`
- `/* istanbul ignore … */` / `/* c8 ignore … */` / `/* v8 ignore … */`
- `/* global … */` / `/* exported … */` / `/* jshint … */`

## 必须删除的注释

- 函数体内的实现注释、行尾注释
- `// ---------- 章节 ----------` / `// ===== 章节 =====` 分隔线及其标题
- 紧贴字段上方的 `// 字段说明` → 并进所属对象/类的 JSDoc，然后删掉

## 文件头（模块级 JSDoc）

每个 `.js` 文件**第一行**必须是文件头 JSDoc。一句话能说清就写一行：

```js
/** fetch 封装：统一错误 → 可读 message */
```

需要补边界时再展开，最多 5 行：

```js
/**
 * 星舰控制台入口：hash 路由 + 舰桥状态。
 *
 * 路由表见 `routes`；能力层全在 `views/`，这里只做挂载与切换。
 */
```

没有 `Attributes:` 段——模块常量的说明写进它自己的 JSDoc，或写进文件头。

## 函数 JSDoc

一句话说得清就**一行**：

```js
/** 秒数 → 「3 分 12 秒」/ 「48 秒」；负数与 NaN → "…" */
export function fmtDuration(sec) {
```

有参数或返回值再展开：

```js
/**
 * 把弹层挂到 body 并贴着 trigger 定位，自动翻边、超出右边界往左收。
 *
 * @param {HTMLElement} trigger 触发按钮
 * @param {HTMLElement} pop     弹层本体（初始 hidden）
 * @param {{minWidth?: number, onOpen?: () => void, onClose?: () => void}} [opts]
 */
export function attachPop(trigger, pop, { minWidth = 200, onOpen, onClose } = {}) {
```

## 写法细则

1. **块体函数必须有 JSDoc**：`function` 声明、`export function`、`const f = () => {`、对象方法简写 `name() {`、类方法、getter/setter、**嵌套函数**。
   **单行表达式体箭头可以不写**（`get: (url) => request("GET", url),` / `const $ = (s, r) => (r || document).querySelector(s);`）——名字 + 一行体已经自明，说明由所在对象/文件头的 JSDoc 承担。语义不自明的单行箭头（做了转换、有副作用）仍然写一行。
2. 一句话能说清就**只写一行**（不带 `@param` / `@returns`）。
3. `@param {Type} name 描述`：
   - **必须写类型**——JS 没有类型标注，类型就是签名。
   - 可选参数用 `[]`：`@param {number} [timeout=30] 超时秒数`。
   - 解构参数写成一个对象类型：`@param {{minWidth?: number, onOpen?: () => void}} [opts]`，并在描述里点名关键字段。
   - 多个 `@param` 的 `name` 列对齐（用空格补到等宽），描述左对齐。
4. `@returns {Type} 描述`：只在返回值含义不自明时写；`void`/`undefined` 不写；一行说明已讲清返回值的不写。
5. **不写** `@throws` / `@example` / `@see` / `@deprecated`（除非真标废弃）/ `@todo`。
   会抛就在第一句说：`/** 解析 payload；非对象时抛 `TypeError`。 */`
6. 段落顺序：作用 →（空行）→ `@param` 由上到下 → `@returns`。
7. 类：类头 JSDoc 写作用 + `@property` 列字段（只列名字/类型说不清的）；构造函数写 `/** 初始化。 */`，参数写在类的 `@param` 上。
8. 对象字面量里的方法同函数；纯配置对象用一段 JSDoc 说明整体用途，键名自明的不逐键写。
9. 私有 / 嵌套 / 回调同样补一行。
10. `test_*`（若有 JS 测试）写一行「验的是什么」。
11. 语言：**中文**。面向用户的 UI 文案字符串不动。
12. 多行 JSDoc：`/**` 单独成行，每行行首 ` * `，收尾 ` */` 单独成行；正文与 `@param` 之间空一行。

## 类型怎么写

JS 的类型是 JSDoc 类型表达式，常用的：

| 场景 | 写法 |
|---|---|
| 字符串 / 数字 / 布尔 | `{string}` / `{number}` / `{boolean}` |
| 可空 | `{?string}` 或 `{string\|null}` |
| 任意 | `{*}` 或 `{any}` |
| 数组 | `{Array<string>}` 或 `{string[]}` |
| 对象 / 字典 | `{Object<string, any>}` |
| 具名形状 | `{{id: string, name: string}}` |
| 函数 | `{(a: number) => string}` |
| Promise | `{Promise<Object>}` |
| DOM 节点 | `{HTMLElement}` / `{HTMLInputElement}` |
| 事件 | `{Event}` / `{MouseEvent}` |
| 可选参数 | `[name]` 或 `[name=默认值]` |
| 多类型 | `{string\|number}` |

## 过长内容的去处

被精简的内容若 ≥ 8 行协议 / 逆向 / 实测细节，先写进 `notes/<相对路径>/<文件名>.md`
再删（`boss_web/static/js/ui.js` → `notes/boss_web/static/js/ui.md`）。笔记模板同 Python
（见 [python.md](python.md) 末节），写完在 `notes/INDEX.md` 加一行。

## 与 Python 的差异（对着 `python.md` 看）

| 项 | Python | JS |
|---|---|---|
| 文档载体 | docstring（`"""…"""`） | JSDoc（`/** … */`） |
| 入参 | `Args:` 段，省略类型 | `@param {Type} name`，**必须写类型** |
| 返回值 | `Returns:` 段 | `@returns {Type}` |
| 类字段 | `Attributes:` 段 | `@property {Type} name` |
| 模块说明 | 模块 docstring | 文件头 JSDoc |
| 属性注释 | `#:` → `Attributes:` | 字段上 `// …` → `@property` |
| 删除目标 | `#` 正文注释、`# ---` 横幅 | `//` 正文注释、`// ---` 横幅 |
| 构造函数 | `"""初始化。"""` | `/** 初始化。 */` |

## 验收

```bash
python tools/jsdoc_audit.py                  # hard=0：无 MISSING / DELETE_ME / RST 残留
node --check <file>                          # 语法没坏
python tools/code_diff_check.py              # 去注释后与 HEAD 的代码 token 一致 = 只改了文档
python -m pytest tests/ -q                   # 后端测试不红
```

`git diff` 抽查同上：改动行只有 JSDoc / 注释 / 空行，**无代码 token 变化**。
