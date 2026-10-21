# `boss_web.services.match_task` 协议笔记

> 摘自 `boss_web/services/match_task.py` 的模块文档字符串 / `#` 注释。

## 一句话

匹配任务：简历 vs 全库/勾选岗位，并行调 LLM，出匹配度 + 优缺点 + 招呼语。

## 细节

跟老的 `boss_web.services.resume_analyzer` **共用输出模板**（含 `pros` / `cons`），差别在这边：

- 简历侧优先吃 **LLM 固定模板解析结果**（`meta.llm.data` 的 summary / skills / work / intent），没解析过才退回规则切章节的摘要；
- 职位侧注入 **JD 正文**（`job_desc`），没抓到就用标签/技能兜底；
- 范围是**全库全部岗位**或勾选的 job_id（C4 已拍板，不再 top_k 封顶）；
- 结果写 `analysis` 表，每条 match 带 `encrypt_job_id`，招呼语可后改；
- 单条失败可 `rematch_job` 原地重跑——**只改不增**，不新插 analysis 行。

并行（最多 `MATCH_CONCURRENCY` 条同时在途），可随时取消；单条失败只记流水，不拖垮整批。

### `GREETING_SYSTEM_PROMPT` / `GREETING_USER_TEMPLATE`

只重打招呼语用的短 prompt：不跑整套人岗评估，省 token 也更聚焦。

招呼语约束不再烤进模板——`USER_TEMPLATE` / `GREETING_USER_TEMPLATE` 里只留
`{greeting_requirement}` 字段，组装时调
`greeting_prompt.resolve_greeting_requirement()` 现读用户设置。这样用户文本
里的 `{}` 也不会被 `.format()` 二次解释炸掉，改完设置下次生成就生效。

### `MatchTask._run_parallel`

并发跑：最多 `MATCH_CONCURRENCY` 条在途。

派活窗口略大于并发数（留点排队余量）；取消时立刻不再派新活，
已经在跑的那几条收完就收工，结果照常并入，不丢已完成的。

窗口满了，收至少一条再派，取消才不会被大队列拖住。

### `MatchTask._match_job`

跑一个职位并把结果落进任务；失败只记 `error`，不抛。

排队期间被取消，直接不跑。

发送要用的（C6），顺手带上，免得到时候再翻 raw_json。

岗位维度的发送史（30 天冷却依据）。快照只记「匹配时」的值；
读接口会按 jobs 表当前值刷新冷却状态。

Match-time blacklist/JD state is an immutable decision snapshot.
Delivery must use these fields rather than rereading mutable rules.

### `_pick_jobs`

范围解析优先级：`crawl_task_id` > `job_ids` > 全库。

抓取任务范围由 `crawl_task_jobs` 解析（与库中仍存在的职位取交集），
前端不用传全量 id。

一键匹配：全库全部岗位（C4 已拍板，不再 top_k 封顶）。

### `RESULT_FIELDS` / `rematch_job`

重跑结果要回写的分析字段。职位快照（job_name / security_id / …）与
发送状态（deliver_*）不在此列——回写时原样复用库里已有的数据。

对一条 match 重跑人岗匹配，结果**原地回写**（只改不增：不新插 analysis 行）。

职位数据复用库里已有的（`jobs` 表；职位删了才拿 match 里的快照兜底），
回写只覆盖 `RESULT_FIELDS`，职位快照与发送状态保持原样。

- 重跑成功 → 分析字段整体换成新结果，`error` 清空；
- 重跑又失败 → **只换 `error`**，其余（含用户手改的招呼语）不动。
