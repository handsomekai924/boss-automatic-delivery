# `boss_web.services.login_task` 协议笔记

> 摘自 `boss_web/services/login_task.py` 的模块文档字符串 / `#` 注释。

## 一句话

登录任务状态机：把 `run_sms_login` / `AppScanLogin.run` 的阻塞回调接到网页表单上。

## 细节

网页上「发码 → 输码 → （滑块）→ 登录成功」分两步走，每步都在后台线程里跑，
HTTP 只负责启动和喂料：

- `LoginTaskManager.start_send` 起线程发短信，命中滑块时把帮助页挂出来；
- `LoginTaskManager.submit_code` 把验证码塞给阻塞中的 `code_provider`；
- `LoginTaskManager.solve_slider` 把极验票据塞给阻塞中的 `slider_solver`。

APP 扫码登录是另一条：`start_qr` 起线程跑 `AppScanLogin`，网页只需要把
二维码画出来、轮询状态，手机扫完在 App 里点确认就完事（见 `qr` 字段）。

滑块只是**人机协作管道**：官方组件渲染、人拖、票据回传。不认缺口、不伪造轨迹。

### `LoginTask.kind`

任务类型：`sms` 短信验证码 / `qr` APP 扫码。两条流的状态机长得像
（都叫 pending / logging_in / done），但界面完全是两套，前端必须按这个
字段分派，不能按 status 猜——扫码的 pending 会误触发短信的步骤条。

### `LoginTaskManager.status_payload`

`logged_in` 以库里那份为准；启动自检说过期时**压成 False**——
清库偶尔会失败（文件锁），这时候不能再让界面谎报「已登录」。

### `LoginTaskManager.refresh_slider` / `_web_slider_solver`

帮助页「重新加载」：换一张新的极验挑战，重建帮助页。

极验的 `challenge` 是一次性的——把旧帮助页重新加载会拿同一张已用过的
挑战去 initGeetest，组件必然 onError。所以这里重新拉一张，再渲染新页面。

`client` 给「重新加载」用：帮助页要换挑战时，得拿它回服务端重新拉一张。

### `LoginTask` 扫码字段

`AppScanLogin` 的 on_payload 把二维码内容写进来，网页去 `/qr/{id}/image` 取图。
票据换发（secondUuid）后二维码内容会变，`qr_revision` 让前端知道该重画了。

### `_run_qr_login`

这几个事件把前端关心的状态写进任务快照，光有 events 不够用。
