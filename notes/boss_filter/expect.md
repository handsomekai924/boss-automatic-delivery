# `boss_filter.expect` 协议笔记

> 摘自 `boss_filter/expect.py` 的模块文档字符串 / `#` 注释。

## 一句话

期望流（`recommend/job/list.json`）的参数装配：推荐 / 兼职 / 指定期望 / 临时期望四个 tab 共用同一条接口。

## 细节

「求职期望流」是第三条检索路径：推荐 / 兼职 / 指定期望三个 tab 共用同一条接口，差异只在参数形态（站点 chunk `job~0.0c757050.js` 的 `getFormData()` 是**四**分支，含临时期望）。刻意**不塞进** `JobSearchFilter`——参数形状是三选一的模式，混进 `to_params()` 会让那条搜索流的分支难测。独立小类更贴前端 `getFormData()`。

kind 对应前端 `currentJobTab`：

| kind | 期望主参数 |
| --- | --- |
| recommend | `city` + `encryptExpectId` + `mixExpectType`（推荐 tab） |
| part_time | `jobType=1903` + `encryptExpectId`（兼职 tab） |
| expect | `encryptExpectId`（指定期望 tab） |
| temp | `expectInfo`（JSON，临时期望 tab） |

筛选维度（`city/salary/experience/degree/industry/scale`，兼职态 `payType/partTime`）与搜索流同一套 code、同一套拼法（多选逗号 join）——期望流**可以带筛选，就是不带关键词**。故意不收 `query`/`position`/`stage` 等 Y 类键：前端点期望 tab 时 `onExpectChange` 会 `delete formData[Y]`（`Y` 首位就是 `query`），期望流请求里永远没有关键词。要关键词就走搜索流（`JobSearchFilter.encrypt_expect_id`，「期望 + 关键词」组合），不要往期望流里塞 query——那不是站点的行为。

另外注意：**改筛选不切搜索态**（前端 `onFilterChange` 不动 `isSearch`），所以「期望 + 薪资/经验/城市」是在期望流上表达的，不是搜索流。

### `JobExpectFilter.to_params`

这套参数是给 **GET** 用的——**实测（2026-10-13）**同参数 POST 回 code 10「未知请求方法」，前端 `hc` 封装那条 POST form 不能照搬。另外**不要**给搜索流补 `partTimeExpect`：兼职态只发 `encryptExpectId` 就够。

兼职态分支：前端 `getConfigFields()` 兼职态还会把 `partTimeExpect` 一并透传，但 `getFormData()` 主参数是同值的 `encryptExpectId`。**实测（2026-10-13，本账号有 4 条兼职期望后复测）**：只发 `encryptExpectId` + `jobType=1903` 就 code 0 / 15 条 / hasMore=true；再叠一份 `partTimeExpect` 同样 code 0——**服务端不要它**，所以这里刻意只发 `encryptExpectId`。
