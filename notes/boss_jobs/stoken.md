# `boss_jobs.stoken` 协议笔记

> 摘自 `boss_jobs/stoken.py` 的模块文档字符串 / 文档字符串 / `#:` 注释。

## 一句话

`__zp_stoken__` 安全网关令牌的来源、算法与全自动获取链路（逆自 `.saved_web` 里的 app~2.1a6c0514.js，模块 49657）。

## 细节

### 来源

站点前端把 `__zp_stoken__` 当成安全网关 Cookie。**它不是服务端发的**，是浏览器本地算出来再回传的。整条链路：

1. 拿着登录态打搜索类接口（如 `/wapi/zpgeek/search/joblist.json`），没带 `__zp_stoken__` 时服务端回 `code:37`「您的环境存在异常.」，并在 `zpData` 里下发**一次性挑战**：

```json
{"code":37,"message":"您的环境存在异常.",
 "zpData":{"seed":"Em6sUKm1q2j+...=","name":"e948d594","ts":1790759239936}}
```

2. 前端把这三样写进 Cookie `__zp_sseed__` / `__zp_sname__` / `__zp_sts__`（生成完立刻清掉），再去拉 **按次下发的生成脚本**：

```
GET /web/common/security-js/{name}.js        ← name 就是挑战里的 name
```

3. 这个脚本往 `window` 上挂构造函数 `ABC`。生成一行：

```
token = new ABC().z(seed, parseInt(ts) + (480 + getTimezoneOffset()) * 60000)
```

（北京时间 `getTimezoneOffset() === -480`，括号归零，所以本地直接传 `ts` 也一样；本模块按原式算，不依赖时区。）

4. 写 Cookie `__zp_stoken__=<token>`，`max-age=3840*60`（3840 分钟），`domain=.zhipin.com` `path=/`，然后把三份辅助 Cookie 清掉。

### 算法（逆自 security-js 静态结构 + 运行时追踪）

`{name}.js` 是两段 IIFE：

* **第一段**：开源的 `js-md5`（MD5 / HMAC-MD5，含 `module.exports` 出口）。
* **第二段**：obfuscator.io 风格的控制流扁平化 VM（`p=NNNN` 计算 goto + `switch` 位域状态机），出口就是 `window.ABC`。
  `ABC` 本身是空构造函数，`ABC.prototype.z` 只是
  `function(){ return l.apply(this, [581].concat(...args)) }`
  —— 581 是 VM 的入口块号。

输出形态：`0138` 固定前缀 + 一段 **58 字符子集的类 base64** 编码。剥掉末位校验字符后按标准 base64 解，起手四字节恒为 `d35dfc81`（`0138gQ` / `0138gR` 等都落在这个魔数上），长度随 seed/ts 在约 130–470 字节之间浮动。**同 seed+ts 连算两次结果不同**——生成器内部带随机量，不是纯哈希。

运行时追踪显示 `z()` 还会采一遍 **浏览器环境指纹** 并编进 token：

| 采集点 | 用途 |
|---|---|
| `document.createElement('canvas')` + 2D 上下文 | canvas 指纹 |
| `navigator.webkitTemporaryStorage` / `deviceMemory` / `hardwareConcurrency` | 硬件特征 |
| `navigator.plugins` / `mimeTypes` / `webdriver` | 插件与自动化标记 |
| `navigator.languages` / `language` | 语言 |
| `screen.availWidth/availHeight/width/height` | 屏幕 |
| `localStorage.getItem('c5jbelwo')` | 持久设备 id |
| `document.createElement('iframe')` | 环境探测 |

所以**同一段 JS 在「真浏览器」和「光板 Node」里算出来的 token 不一样**，服务端按指纹校验。本模块默认在 Node 里跑（带浏览器外壳），`compute_stoken` 的结果格式正确、可重复调用；要让服务端认，指纹得跟请求头自洽（见 `boss_jobs.config` 的 `DEFAULT_HEADERS`，UA / 平台要跟外壳对得上）。

### 全自动获取

```python
>>> from boss_jobs.stoken import StokenProvider
>>> p = StokenProvider(http=session)          # 带登录态
>>> token = p.ensure()                        # 打一次拿挑战 → 算 → 写 Cookie
>>> token[:4]
'0138'
```

也可以只算不发请求：

```python
from boss_jobs.stoken import compute_stoken, load_security_js
js = load_security_js(http, "e948d594")
token = compute_stoken(js, seed="...", ts=1790759239936)
```

### `parse_challenge`

三个字段缺一、或 `ts` 不是能用的正整数，就返回 `None`（调用方据此报「接口没给挑战」）。业务码本身不看——挑战只认 `zpData` 的形状，code 37 只是它最常见的出场方式。

### `load_security_js`

这份脚本是**按挑战里的 name 现下发**的，站点随时可以换名换实现，所以不做算法硬编码，每次都认服务端给的那份。

### `compute_stoken`

`tz_offset_minutes` 是 JS 的 `getTimezoneOffset()`；默认取本机当前时区（跟浏览器行为一致）。

### `StokenProvider.apply`

前端是 `max-age=3840*60`；`requests` 的 cookiejar 认的是绝对过期时间 `expires`，这里换算过去。

### `put_cookie`

把一枚 Cookie 稳稳地写进 cookiejar：**先清同名，再写**。

`requests` 的 jar 允许同名不同 domain 共存，而 `http_from_session` 装配时是不带 domain 写的（domain=`''`）。要是不清就补一枚 domain=`.zhipin.com` 的，请求头里会同时出现新旧两枚 `__zp_stoken__`，服务端照着旧的那枚拒——实测就是这么吃的 code 37。

### `mint_offline`

账号被风控（code 36）拿不到真挑战时，用它验证「算法通了」并量出纯铸币耗时。结果**结构合法但服务端不会认**（challenge 是样例的），别拿去打接口。

### 常量

- `SECURITY_JS_URL`：`{name}.js` 的下载地址模板。
- `STOKEN_MAX_AGE`：令牌 Cookie 的有效期（秒）= 前端写的 3840 分钟。
- `RUNNER_PATH`：生成脚本的 Node 入口（跟本文件同目录的 assets/）。
- `HELPER_COOKIES`：挑战三元组在 Cookie 里的临时名字（前端写完令牌就清掉）。
- `DEFAULT_CACHE_DIR`：security-js 的本地缓存目录（每个 name 一份，脚本内容按 name 换）。
- `NODE_BIN_ENV`：Node 可执行文件；空 = 从 PATH 里找。
- `SAMPLE_CHALLENGE`：离线铸币用的样例挑战（算法演示/计时用，服务端不会认，别拿去打接口）。
