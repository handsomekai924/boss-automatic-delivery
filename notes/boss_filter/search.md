# `boss_filter.search` 协议笔记

> 摘自 `boss_filter/search.py` 的模块文档字符串 / `#:` 注释。

## 一句话

搜索条件装配：把筛选维度拼成 `/wapi/zpgeek/search/joblist.json` 认的查询串参数。

## 细节

参数名不是猜的，来自职位页 chunk `job~1.59fdd3bf.js` 的 `getFormData()`:

```
{city, experience, payType, partTime, degree, industry, scale,
 salary, jobType}          ← 数组用逗号 join，单项原样
+ query / page / pageSize / scene / encryptExpectId
```

对照同 chunk 的筛选状态 `Q`：`payType/partTime/experience/degree/industry/scale/stage` 是多选，`city/jobType/salary` 是单选。

选中值一律用 `FilterConditions` 里的 **code**（如薪资 10-20K = `405`），不是显示名。

### `JobSearchFilter.encrypt_expect_id`

期望上下文（前端 `lastExpectId`）。「期望 tab + 输入关键词」时前端会把最近一次选中的期望跟着关键词带进搜索流：`s.encryptExpectId = lastExpectId`。空 = 不带；非空 → `to_params` 输出 `encryptExpectId`。

**不算检索维度**：`is_blank` 故意不把它算进去，免得「只带期望、没关键词」误判成搜索流，抢走本该走期望流的分支。

**实测（2026-10-13）**：带不带它结果集 9/10 重叠——是**个性化/排序信号**，不是范围限定。所以 UI 按「静默带出」处理（只写这个字段、`expect_kind` 留空）是对的，别当成「限定在该期望内搜」。
