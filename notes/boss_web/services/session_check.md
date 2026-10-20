# `boss_web.services.session_check` 协议笔记

> 摘自 `boss_web/services/session_check.py` 的模块文档字符串 / `#` 注释。

## 一句话

启动时的登录态自检：拿库里的凭证打一次用户信息接口，别等任务开跑才发现没登录。

## 细节

`GET /api/auth/status` 只看库里有没有 token/Cookie，**不问服务端**。凭证过期后
它照样报「已登录」，用户点「开始抓取」才撞见「当前登录状态已失效」——那时候任务
已经起了一半。

启动时自检一次（`schedule_startup_check`）：

| 状态 | 含义 |
| --- | --- |
| `valid` | 服务端认；库里 `user` 顺手刷成最新的 |
| `expired` | 服务端明说登录失效 → **清掉库里那份**（更新数据库，别再谎报已登录） |
| `absent` | 库里本来就没有登录态，不发请求 |
| `unknown` | 网络不通 / 别的错 → **不动库**，断网不该把好凭证抹掉 |

「是不是真的失效」有判据：跟 `boss_jobs.errors.JobApiError.is_session_expired`
同规矩——话术里明说了登录问题才算。`getUserInfo` 回个 `code 1`「系统繁忙」
也走 `boss_login.errors.SessionExpired`（那边把所有非 0 都归成它），
一律清库会把还在有效期的登录态白白抹掉。

测完才开浏览器（见 `cli._open_browser_when_ready`）：网页一打开看到的会话
状态就是定论，不用再刷新一次才正确。

### `looks_like_session_expired`

`getUserInfo` 的失败会被 `boss_login.client.ZhipinLoginClient.fetch_user_info`
一律包成 `boss_login.errors.SessionExpired`，但那不代表「凭证过期」——
「系统繁忙」「接口不存在」也在里面。只有话术点名了登录/失效/过期才清库。
