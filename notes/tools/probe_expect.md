# `tools.probe_expect` 协议笔记

> 摘自 `tools/probe_expect.py` 的模块文档字符串 / `#` 注释。

## 一句话

P3 实测校准：把「求职期望流」五个待定点一次打清楚（见 docs/job-expect-flow-plan.md §七）；只读列表接口，不发消息、不改账号状态。

## 细节

### 模块：探针范围

只打**只读列表**接口（expect/list、recommend/job/list、search/joblist），不发消息、不改账号状态。每次探针都把原始 code / zpData 顶层键打出来，方便对着注释回填。

输出是人读的探针报告；要 JSON 加 `--json`。

### `main` 点 1 + 2：expect/list.json 的鉴权 & 三类期望的真实字段名

先**不带** `__zp_stoken__` 打一发（点 1），再带令牌打一发。`strip_cookie` 要按名清干净（同名 cookie 先清再写的老坑）。别让 `_ensure_stoken` 又给塞回去。

点 2：对照 raw 里的 `encryptId` / `encryptExpectId` 键名；本账号期望列表是空的时字段名对称性还是靠防御性双键名兜着。

### `main` 点 5：期望流 / 搜索流 GET vs POST

GET 走生产的 stoken 重试（先 `_ensure_stoken`，撞 37 还会退避换新）；裸 `_request_json` 不 `_ensure_stoken`，会把 37 误判成「方法不行」。POST 用同一枚令牌，只为确认服务端认不认 POST。

### `main` 点 3 + 4：期望上下文与兼职参数

走生产路径的 stoken 重试——裸 `_get_json` 不 `_ensure_stoken`，会把「缺令牌」误判成「参数不行」。两发之间 `time.sleep(1.0)` 隔开，免得被安全网关当过频连环 37。

点 4：with/only 两发对比——若 only 就 code 0 且有职位，服务端不要 `partTimeExpect`。

点 3：带不带 `encryptExpectId` 结果集一样 → 更像「个性化信号」，不是范围限定；完全不相交 → 像是「限定在该期望内搜」；部分重叠 → 可能是排序/个性化加权，不是硬过滤。
