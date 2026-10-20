# `boss_jobs.cli` 协议笔记

> 摘自 `boss_jobs/cli.py` 的模块文档字符串 / `#` 注释。

## 一句话

CLI 帮助里被精简掉的检索流说明与 stoken 自动补令牌流程。

## 细节

### 自动补 `__zp_stoken__`

搜索流 / 期望流还会**自动**补 `__zp_stoken__`：每次抓页前判一次过期，过期了就拉起一台 Chrome（CDP）让站点自己算一枚、落到 `doc('stoken')`、再镜像进 `doc('session')`，下次直接用（令牌约 64 小时）。没有单独的「取令牌」命令。

### `--expect` / `--filter` 互斥

`--expect`（期望流）与 `--filter`（搜索流）**互斥**——两者都在选「这次走哪条检索流」。期望流可以用库里的共享筛选维度（城市/薪资/经验/…，经 `JobExpectFilter.from_search` 搬过去），但**永远不带关键词**（站点点期望 tab 时就把 `query` 删了）。要「期望 + 关键词」就走搜索流（库里配好 `encryptExpectId`）。

`--filter` 是「这次就用这份」的只读入参；不传则用库里的条件。

### `_cmd_fetch`

推荐页 special/zone 已下线（2026-10-13），「全部不限」没有检索语义了。

### `_assemble_expect_filter`

共享筛选维度（城市/薪资/经验/学历/行业/规模，兼职态 payType/partTime）从库里的搜索条件搬过来——**一份筛选条件两用**，只是期望流换上 `encryptExpectId`。`query` 不搬。

临时期望：已经是序列化好的 expectInfo 串就原样透传；是对象则按前端形状序列化。推荐 tab 还要 `mixExpectType`；id 对得上列表里的混合期望才带得出来。默认取哪个的规则在 `from_expects`：`partTimeExpectList[0]` / `expectList[0]` / `mix`。前端 `getFormData().city = mixLocation || defaultCity`：混合期望优先。

### `EXPECT_SPECS`

`--expect` 认的取值（写进 help，跟解析规则同一处维护）。
