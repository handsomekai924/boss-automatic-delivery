# `boss_jobs.cdp_stoken` 协议笔记

> 摘自 `boss_jobs/cdp_stoken.py` 的模块文档字符串 / 文档字符串 / `#:` 注释。

## 一句话

`__zp_stoken__` 的真浏览器获取：Chrome CDP 拉起 → 站点自算 → 落盘 + 过期判断。

## 细节

### 为什么不用 Node 硬算

`boss_jobs.stoken` 那条「拿挑战 → 下 `security-js` → Node 跑 `ABC.z`」链路**算法是对的**，但服务端不认：`ABC.z` 会顺手采一遍浏览器环境指纹（canvas / plugins / screen / localStorage 设备 id …）编进 token，光板 Node 的外壳跟真实 Chrome 对不上，打过去就是 `code 37`。

所以这里换一条路：**让真 Chrome 自己把 token 算出来**。Chrome 本来就是站点的原生环境，指纹、时区、UA 全自洽，不需要逆向、不需要补指纹，更不需要碰 cookie 加密（Chrome 127+ 的 `app_bound_encrypted_key` 那套也不用动）。

### 做法（CDP = Chrome DevTools Protocol）

1. 找一台 Chrome（`BOSS_CHROME_BIN` / 常见安装路径 / PATH）。
2. 先试连已有的调试端口 `127.0.0.1:9222`；连不上就用**独立的 user-data-dir** 拉起一台（`--remote-debugging-port` 在默认 profile 上会被 Chrome 拒绝，这里专门开一个 `.chrome_profile`，跟日常那台 Chrome 互不打架）。
3. `Storage.setCookies` 把状态库里 `doc('session')` 的登录 Cookie 灌进去——免得每次都要手工登一遍。
4. `Target.createTarget` 打开 `https://www.zhipin.com/web/geek/jobs`，让站点自己的前端把 `__zp_stoken__` 算出来写进 Cookie（3840 分钟）。
5. 轮询 `Storage.getCookies` 把它读出来，连同 `expires` 一起落到状态库的 `doc('stoken')` 行。

### 边界

* **只读 Cookie，不解密 Cookie。** 读靠 CDP 调试口，那台 Chrome 是自己拉起来的，用户知情。不去抠 Chrome 的 `Cookies` SQLite，更不去注入进程解 `app_bound_encrypted_key`。
* **拉起来的 Chrome 默认留着**（本进程内下次复用，秒连；`BOSS_CDP_KEEP=0` 则抓完就关）。**但进程一退就关**——见下面的「退出收尾」。
* **复用的那台绝不动。** 用户自己开的 Chrome（手动带了调试口）不在我们的册子上，退出时不会去关它。
* **滑块/风控（code 36）不在这里处理。** 拿不到 token 就如实报错，让用户自己去浏览器过人机，客户端不去绕。

### 窗口可见性（`BOSS_CHROME_MODE`）

**默认 `hidden`**：拉 Chrome 只是为了让站点自己算令牌，用户没必要被弹一脸窗口。

* `hidden`——屏幕外起 + Win32 `SW_HIDE`，连任务栏/Alt+Tab 里都没有（**默认**）。
* `visible`——照常显示。**要人工过滑块/人机验证时得用这个**：隐藏的窗口人够不着（任务栏里也没有），只能靠这个档位把它叫出来。
* `offscreen`——屏幕外，但 `IsWindowVisible` 仍为真，还挂在任务栏/Alt+Tab 里。
* `headless`——`--headless=new`，没有窗口。**别用**，见下。

实测（2026-10-09，Chrome 153，同一账号，四个档位轮着拉）：

* `offscreen` / `hidden` 都把窗口从用户眼前挪开，站点 JS 照跑，约 2s 出令牌，拿它打 `joblist.json` 服务端照收（`code 0`，15 条）。要"前台看不见"就用这两个。
* `headless` **不合规**：页面一开就被站点重定向到 `/web/passport/zp/verify.html?…&code=36`（「安全验证 / 账号可能存在异常访问行为」），等 40s 也等不到 `__zp_stoken__`。跟渲染能力无关——无头下 WebGL（ANGLE + 真 GPU）、插件数、时区跟有头一模一样，**是站点的风控在挑环境**。所以这一档只留着备查，别拿去跑。
* 非 `visible` 档位都会让 Chrome 把窗口判成「被遮挡」，页面里 `document.visibilityState === 'hidden'`。取令牌不受影响，但这个差异记着——这正是 `hidden` 档位额外挂 `--disable-backgrounding-occluded-windows` / `--disable-renderer-backgrounding` / `--disable-background-timer-throttling` 和 `CalculateNativeWinOcclusion` 的原因：否则被遮挡的窗口会被停画、后台定时器被冻结，站点 JS 可能就算不动令牌。

### 退出收尾（`BOSS_CDP_CLOSE_ON_EXIT`）

进程退出时 `close_launched_browsers` 会把**我们拉起来的** Chrome 一起关掉（先 CDP `Browser.close` 让它自己退，没退就杀进程）。`atexit` + `SIGTERM` 两条路都接了，Ctrl+C 和正常结束都算；只有被强杀（`taskkill /F`）才收不了。

不想要这个行为（比如想让浏览器跨进程留着复用），设 `BOSS_CDP_CLOSE_ON_EXIT=0`。

### 用法

```python
from boss_jobs.cdp_stoken import CdpStokenProvider
p = CdpStokenProvider(http=session)     # http 里带登录 Cookie
token = p.ensure()                      # 有过期检查，过期才拉 Chrome
token = p.ensure(force=True)            # 无视过期，强制换新
p = CdpStokenProvider(http=session, mode="visible")  # 要人工过验证码时
```

### 常量

- `CDP_PORT_ENV` / `DEFAULT_CDP_PORT`：调试端口（`--remote-debugging-port`）。
- `CHROME_BIN_ENV`：Chrome 可执行文件；不设就按常见安装路径 → PATH 的顺序找。
- `CHROME_PROFILE_ENV` / `DEFAULT_CHROME_PROFILE`：独立 user-data-dir。**必须独立**：Chrome 在默认 profile 上会拒开调试口。
- `CHROME_KEEP_ENV`：抓完是否留着 Chrome。默认留着，**本进程内**下次秒连；进程一退还是会被 `close_launched_browsers` 收走。
- `CHROME_CLOSE_ON_EXIT_ENV`：进程退出时是否把我们拉起来的 Chrome 一起关掉。默认关。
- `CHROME_MODE_ENV` / `CHROME_MODES` / `DEFAULT_CHROME_MODE`：窗口可见性档位。
- `DEFAULT_DB_PATH`：令牌账本落在状态库的哪一行。库路径走 `BOSS_DB`。
- `EXPIRY_MARGIN`：快过期就提前换新的余量（秒）。
- `RENEW_COOLDOWN`：强制换新的冷却（秒）。撞 code 37 有时只是「请求太快」，连环拉 Chrome 既慢又更容易把风控惹出来。
- `ACQUIRE_TIMEOUT`：等站点写出 token 的超时（秒）。真浏览器首次开页要拉 chunk，给宽点。
- `TOKEN_PAGE`：要 token 就得跑站点 JS 的那个页面。
- `_NON_COOKIE_KEYS`：登录态里不需要往 Chrome 灌的键。
- `_CHROME_CANDIDATES`：常见的 Chrome 安装路径。
- `_OFFSCREEN_POS`：`offscreen` 把窗口摆到这儿（`-32000` 是 Win32 的保留值，别用）。
- `_SW_HIDE`：Win32 ShowWindow 的 SW_HIDE。

### `StokenRecord`

站点给的是 Cookie `expires`；没给就按前端的 `max-age=3840*60` 从 `minted_at` 推。

### `StokenStore`

跟登录态分开放是有意的——`doc('session')` 的 `cookies` 是纯 `str → str`，塞不进过期时间；而过期判断恰恰是最关键的那一环。取出来的 token 会另外**镜像**一份进 `doc('session')`，让 `boss_jobs.client.http_from_session` 老规矩继续生效。

### `_mode_flags` / `_disable_features`

hidden 档位大头是拉起来之后 Win32 SW_HIDE；这里先摆到屏幕外，免得藏之前闪一下。窗口一旦不可见，Chrome 会停画 + 冻结后台定时器，于是站点 JS 可能算不动令牌，这几条开关是专门的解药。Windows 上 Chrome 靠 `CalculateNativeWinOcclusion` 判断「窗口被别的窗口盖住了」→ 停画。

### `_hide_windows`

把这台 Chrome 的顶层窗口 `SW_HIDE` 掉。只在 Windows 上有意义。Chrome 开窗口比开调试口晚一拍，所以找不到就等到 timeout。

### `_Launched`

**只登记自己拉的**——复用现成调试口那台（用户自己开的）绝不入册，退出一律不碰。`ws_url` 尾巴那截 uuid 是每个浏览器实例独有的，按它连回去就不会误伤后来占了这个端口的别人。

### `_FATAL_CONSOLE_EVENTS` / `_install_console_handler`

「进程一定活不下来」的控制台事件，可以放心在这儿收尾。

`CTRL_C_EVENT` **刻意不在里面**：那条路 Python 自己抛 `KeyboardInterrupt`，交给 `atexit` 收；而且万一上层把 KeyboardInterrupt 吃掉了、程序没退，提前把热着的 Chrome 关了纯属白关。`CTRL_BREAK_EVENT` 实测必死（退出码 `0xC000013A`，Python 层收尾一律不跑），所以在这儿收。

Windows：控制台把进程硬干掉的那几条路，也把 Chrome 收走。`CTRL_CLOSE_EVENT`（用户点终端窗口的 X）、`CTRL_LOGOFF_EVENT`、`CTRL_SHUTDOWN_EVENT`、`CTRL_BREAK_EVENT` 这几条都走不到 `atexit`。⚠️ `CTRL_C_EVENT` **不碰**，一律返回 FALSE 放行。

### `close_launched_browsers`

先走 CDP `Browser.close` 让它自己退（profile 收得干净），没退就杀进程——那是我们自己的子进程，杀了不牵连谁。**复用现成调试口那种一律不碰。**

### `launch_chrome`

⚠️ 必须独立 profile：Chrome 136+ 直接拒绝对默认 profile 开 `--remote-debugging-port`。独立 profile 的登录态由本模块每次从状态库 `doc('session')` 灌进去，不用手工登。

Chrome 111+ 默认拒掉陌生 Origin 的 WS 握手；调试口是本地闭环，放开它。hidden 档位藏不藏得上都继续：顶多用户看见一个后台窗口，令牌该拿还是能拿。

### `connect_or_launch`

端口被别的 Chrome 占着（握手还被拒）时，自动往后挪一两个端口，免得跟用户自己那台死磕。`mode` 只影响**自拉**那一支。

### `CdpClient`

只用少数几个命令：`Storage.setCookies/getCookies`、`Target.getTargets/createTarget/attachToTarget`、`Page.navigate`。不碰 cookie 存储文件，也不注入页面跑脚本——令牌由站点自己写。

`suppress_origin`：Chrome 111+ 会对陌生 Origin 回 403，调试口本来就是本地闭环，不发 Origin 头最干净。

`call` 的 `session_id`：页面级命令（`Page.navigate` 等）要带；browser 级（`Storage.*` / `Target.*`）不用。

### `CdpClient.set_login_cookies`

只灌真正的登录态，`__zp_stoken__` 不灌——那枚要让站点自己算，否则指纹还是对不上。

### `CdpClient.clear_cookie`

CDP 的 `Storage` 域只有「全清」没有「按名删」，全清会把登录态一起端掉；所以在站点页面里改 `document.cookie` 的到期时间。

### `CdpClient.open_page`

已经有 zhipin 的 target 就复用它 `Page.navigate`，否则新开一个。不等页面 `load`——等的是 Cookie，够快。

### `CdpStokenProvider`

跟 `boss_jobs.stoken.StokenProvider` 同一个 `ensure(force=)` 接口，所以 `JobClient.fetch_search_page` 的编排不用改。

### `CdpStokenProvider.ensure`

顺序：

1. 账本里还新鲜 → 直接用（Cookie 里还不是这枚才写回去）；
2. 账本没有、但会话里已经有人（手工拷的）→ 信它，等撞 37 再说；
3. 其余（过期 / `force`）→ 拉 Chrome 换新，落盘。

两处**刻意不动**，免得日志/状态库被刷屏：已经是这枚令牌时不再 `_persist`；`force` 也有冷却（`RENEW_COOLDOWN`）。force 的冷却刻意不拿 `record.minted_at` 当起点——那是落盘时间。

### `CdpStokenProvider._acquire_from_chrome`

先把旧的过期掉再重载：否则站点看到 Cookie 还在就不重算，force 换新时拿到的还是同一枚被拒过的令牌。

### 调试口探测：两个 loopback + 不走代理

Chrome 默认绑 `127.0.0.1`，**被占时它退到 `[::1]`**（日志里照样写
`DevTools listening on ws://[::1]:9222/…`）。只问 IPv4 就会把「明明开了」误判成
「没开」——真机踩过。`_LOOPBACK_HOSTS = ("127.0.0.1", "[::1]")` 两个都问。

另一个坑：**不走环境变量里的 HTTP 代理**。开着梯子 / 构了 `HTTP_PROXY` 时，默认
opener 会连 loopback 一起劫走，表现同样是「Chrome 起了但连不上调试口」。所以用
`build_opener(ProxyHandler({}))`。

### 端口回退

端口被别的程序占着时自动往后挪（试 `first_port` ~ `first_port+2`）。判定标准
`_port_usable`：任一 loopback 能 `bind` 就行——Chrome 自己也是这么退的。两个都被
占着才值得换端口。

⚠️ **每次换端口都换一个 user-data-dir**：同一台 Chrome 的 profile 被锁着，换端口不
换 profile 会被已有的实例接走请求，调试口根本不会在新端口上开。

### Chrome 自己的 stderr 日志

调试口起不来时，原因就在 Chrome 自己的 stderr 里（`bind() returned an error` /
`DevTools listening on …` / 策略拦截）。以前直接扔 `DEVNULL`，出问题只能靠猜「是
不是安全软件」。现在落到 `profile/chrome-stderr.log`（追加写，超 1 MiB 下次启动前
清掉），`_chrome_log_hint` 只挑和调试口有关的行，拼进失败提示。

### 「profile 被占」和「被安全软件拦了」要分开报

启动进程提前退出时有两副面孔：

- `_profile_populated(profile)` 为真（有 `Local State` / `Default`）→ profile 被
  别的 Chrome 占着，请求被转交过去了；
- 为假（目录还是空的）→ Chrome 压根没起来，多半被安全软件拦了，或 Chrome 装得不对。

两者的处置完全不同，不能都报成「被别的 Chrome 占着」。
