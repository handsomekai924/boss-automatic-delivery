# `boss_web.services.crawl_task` 协议笔记

> 摘自 `boss_web/services/crawl_task.py` 的模块文档字符串 / `#` 注释。

## 一句话

后台抓取任务：线程跑 `JobClient.crawl`，网页轮询进度。

## 细节

除了内存里的「当前任务」，每次 `start()` 还会落一行 `crawl_tasks` 档案，
每页入库时把本批职位写进 `crawl_task_jobs`——历史抓取可回看条件与结果，
也能「对本批岗位一键发起匹配」。落库失败**不中断抓取**（任务本体优先），
只在日志里告警「历史记录未保存」。

### `BLANK_SEARCH_MSG`

搜索流条件全空时的报错（推荐页 special/zone 已下线，「全部不限」没有检索语义了）。
`start()` 走 `ValidationWebError` 快速失败；万一漏进线程，
`_run` 抛 `ValueError` 由 `humanize` 原样透传。

### `CrawlTaskManager.start`

旧「推荐页」（use_search=false）已移除：兼容字段一律当搜索流，
传 false 也归一成 true 并留一条警告。

快速失败：搜索流条件全空就别开线程了（推荐页 special/zone 已下线）。

历史档案：启动就落一行，后面每页归属 / 结束回写都在这行上累进。

### `CrawlTaskManager._run`

期望 id 在 config_snapshot.search 里（search_payload() 写出），
collection 上那份是同值的副本；两边都认，优先 collection。

共享筛选维度直接从 search payload 搬（一份条件两用），
期望主参数单独给。from_search 不搬 query/scene——期望流永远
不带关键词；query 非空且 expect_kind 已选时以 expect_kind 为准
（期望流优先），UI 上应提示「关键词在期望流下不生效」。

temp_expect 时把 cfg.expect_info 塞进 expect_info（from_search 透传）。

两条流二选一：没选期望就走搜索（旧 use_search=false 的
推荐页 special/zone 已移除，不再有第三条流兜底）。

搜索流 / 期望流都要 `__zp_stoken__`，可能拉真 Chrome，先把状态亮出来。

线程里兜住，状态给前端；线程不走 FastAPI 的异常处理器，人话化得在这儿做。
