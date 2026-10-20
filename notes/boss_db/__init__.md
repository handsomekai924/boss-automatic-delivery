# `boss_db.__init__` 协议笔记

> 摘自 `boss_db/__init__.py` 的模块文档字符串与「连接」章节的 `#` 注释。

## 一句话

统一状态库的设计分工与连接并发模型：为什么 `doc` 存整包 JSON、实体才建真表，以及为什么每个线程必须各自持连接。

## 细节

### 三类表的分工

以前登录态、搜索条件、stoken、LLM 配置、简历、分析各自躺在 `*.json` 里，职位另有一份 `jobs.db`。
现在**只有一份库** `data/boss.db`：

- **`doc`**
  单例文档（会话 / 搜索条件 / stoken / LLM 配置 / 投递配置）。它们本身就是「一份文档」，
  字段由各模块自己的 dataclass 管，所以整包存 JSON，不拆列。
  payload 就是当年那个文件的正文，迁移 = 把文本搬进一行。
- **`resume` / `analysis`**
  实体集合，有 CRUD、要按时间排序、要按 id 删——真正的表。
  嵌套字段（章节、匹配结果）整包进 JSON 列，和 `jobs.job_labels` 一个惯例。
- **`jobs` / `fetch_pages`**
  职位与抓取流水，DDL 从 `boss_jobs.store` 原样搬来。

路径覆盖顺序：显式 `path` → 环境变量 `BOSS_DB` → `data/boss.db`。
用户点名的 JSON 文件读写**不走**这里（`boss_filter export --out`、`search_filter_from_file` 那类）。

### 连接：线程本地 + 进程内跟踪

`sqlite3.Connection` 不是并发安全的。

FastAPI 线程池会并发跑同步端点。以前 `_conns` 只缓存一个连接给所有线程共用，
并发 execute/fetch 会把 sqlite3 内部状态搅乱——轻则 `InterfaceError`（bad parameter
or other API misuse），重则 `Row` 变成残缺元组（`IndexError: tuple index out of range`）。

现在每个线程各自持连接：文件库靠 WAL 并发读写，`:memory:` 同线程内共享一份。
`_conns` 只为 `close_db` / `close_all` 收尾做进程内登记。

迁移要串行跑（`_migrate_lock`）：web 线程池会并发 `connect` 同一个默认库。

### 迁移的副作用

迁成功的 JSON 源文件会**删掉**；`jobs.db` 只改名成 `jobs.db.migrated`，留个后路。
单条失败只 warning，不挡别的条。幂等：`_MIGRATED_FLAG` 闸门挡着，跑第二次是 no-op。
