# Vue3 文档注释规范（占位）

> **状态：占位。** 项目还没有 `.vue` 文件；真把 Vue3 接进来时按这份做，做完跑 `python tools/jsdoc_audit.py`。
> 基线是 [js.md](js.md)（JSDoc），这里只写 Vue 特有的部分。

## SFC 三段各自的文档载体

| 段 | 文档载体 | 说明 |
|---|---|---|
| `<script setup>` | JSDoc | 每个函数 / 变量同 [js.md](js.md) |
| `<template>` | **不写注释** | 模板自带语义；真要说明就写进 `<script setup>` 的 JSDoc |
| `<style>` | **不写注释** | 需要说明就在对应组件的 `<script setup>` 里讲 |

组件是文件级单元，所以**没有「文件头 JSDoc」**——改成 `<script setup>` 顶部一段**组件说明**：

```vue
<script setup>
/**
 * 职位卡片：列表流里的一条，点开进详情。
 *
 * 拉黑态只改样式不隐藏，让用户看得见「为什么跳过」。
 */

import { computed } from "vue";
</script>
```

## `defineProps`

属性必须写成 **JSDoc `@typedef` + `defineProps` 引用**，不要用 TypeScript 接口（本项目不引入 TS）：

```vue
<script setup>
/**
 * 职位卡片的入参。
 *
 * @typedef {Object} JobCardProps
 * @property {string} jobId      职位加密 id，全站唯一。
 * @property {string} title      岗位名称。
 * @property {string} [salary]   薪资文案；空串 = 接口没回。
 * @property {boolean} [blocked] 是否已被规则拉黑。
 */

/** @type {JobCardProps} */
const props = defineProps({
  jobId: { type: String, required: true },
  title: { type: String, required: true },
  salary: { type: String, default: "" },
  blocked: { type: Boolean, default: false },
});
</script>
```

- `@property` 只列**名字/类型说不清**的字段；`title: 岗位名称` 这种自明的不写。
- 可选属性用 `[]`；默认值写在 `@property` 描述里。
- 用运行时声明（`defineProps({…})`）而不是纯类型声明，打包后不依赖 TS 编译。

## `defineEmits`

事件是组件的公开接口，必须逐个写清**触发时机**：

```vue
<script setup>
/**
 * 卡片上的用户动作。
 *
 * @typedef {Object} JobCardEmits
 * @property {(e: "open", jobId: string) => void} open     点卡片主体时触发。
 * @property {(e: "greet", jobId: string) => void} greet   点「打招呼」时触发；已在冷却中不触发。
 * @property {(e: "block", jobId: string) => void} block   点「拉黑」时触发。
 */

/** @type {JobCardEmits} */
const emit = defineEmits(["open", "greet", "block"]);
</script>
```

- 事件名用 kebab-case（`job-open`）或单动词（`open`），全文件统一一种。
- 回调签名写成 `(e: "name", ...args) => void`，参数名要说人话。

## `defineExpose`

默认 `<script setup>` 全不外露。真要给父组件拿方法，写清**给谁用、什么时候用**：

```js
/**
 * 给父组件的表单校验入口；`父组件 ref.validate()` 调。
 *
 * @returns {boolean} 全部字段合法为 `true`，否则 `false` 并把第一个非法字段滚进视口。
 */
function validate() {
  /* … */
}

defineExpose({ validate });
```

## 组合式函数（composables）

`src/composables/use*.js`（或 `.ts`）按 [js.md](js.md) 写 JSDoc，外加两处：

1. **返回值**必须写 `@returns {Object}` 并逐字段列清——composable 的返回值就是它的 API。
2. **副作用**写进第一句（会改路由、会起轮询、会写 localStorage）。

```js
/**
 * 抓取任务的实时状态：起轮询 `/api/crawl/status`，组件卸载时停。
 *
 * @param {number} [intervalMs=1500] 轮询间隔（毫秒）。
 * @returns {{status: import("vue").Ref<string>, progress: import("vue").Ref<number>, retry: () => Promise<void>}}
 *   `status` 任务状态串；`progress` 0-100；`retry` 失败后手动重跑一次。
 */
export function useCrawlTask(intervalMs = 1500) {
  /* … */
}
```

## 其它 Vue 特有规则

| 情况 | 做法 |
|---|---|
| 模板里的 `<!-- 注释 -->` | **删**。语义上移到 `<script setup>` 的 JSDoc |
| `v-if` / `v-for` 的行内说明 | 删；分支语义由外层函数 JSDoc 承担 |
| 插槽 | 组件说明里点名「有哪些具名插槽、各自装什么」 |
| `defineModel` | 同 `defineProps`，在 `@typedef` 里写 `@property {import("vue").ModelRef} v-model` |
| 自定义指令 `v-*` | 指令对象的方法按 [js.md](js.md) 写 JSDoc；文件头一句话说明它改了什么 DOM |
| 测试 `*.spec.js` | 每个 `it` 上方一行「验的是什么」，同 Python 的 `test_*` |
| 组件名与文件名 | 文件名 PascalCase（`JobCard.vue`）；JSDoc 首句直接用组件名开头 |

## 验收

```bash
python tools/jsdoc_audit.py     # 认 <script>/<script setup> 段里的 JSDoc
node --check <extracted.js>     # 语法没坏（工具会抽出 script 段临时校验）
```

`git diff` 抽查同 [js.md](js.md)：改动行只有 JSDoc / 注释 / 空行，无代码 token 变化。
