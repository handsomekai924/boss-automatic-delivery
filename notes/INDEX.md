# notes 索引

代码里的文档字符串只留**作用、入参、返回值**。协议逆向、实测记录、历史背景留在这里。

风格规范见 [docstring-style.md](docstring-style.md)（已迁到 `.claude/skills/doc-style/`，含 JS / Vue3）。

| 文件 | 源模块 | 一句话 |
|------|--------|--------|
| [_tests/test_cli.md](_tests/test_cli.md) | `tests.test_cli` | 滑块失败要报风控话术，不能把票据说成走 `--verify-token` |
| [_tests/test_expect_filter.md](_tests/test_expect_filter.md) | `tests.test_expect_filter` | 四种期望 kind 的主参数形状对齐站点 `getFormData()` |
| [_tests/test_packaging_scripts.md](_tests/test_packaging_scripts.md) | `tests.test_packaging_scripts` | 打包脚本的回归护栏，钉死上次踩过的坑 |
| [_tests/test_web_deliver.md](_tests/test_web_deliver.md) | `tests.test_web_deliver` | 「开聊提醒」是提示弹窗，不是停批信号 |
| [_tests/test_web_session_check.md](_tests/test_web_session_check.md) | `tests.test_web_session_check` | 启动登录态自检：验真伪、失效清库、断网不动库 |
| [boss_db/__init__.md](boss_db/__init__.md) | `boss_db.__init__` | 三类表的分工与线程本地连接模型（为什么 `doc` 存整包 JSON） |
| [boss_filter/client.md](boss_filter/client.md) | `boss_filter.client` | 筛选条件线上接口客户端，登录态复用落盘 Cookie |
| [boss_filter/expect.md](boss_filter/expect.md) | `boss_filter.expect` | 期望流四个 tab 共用 `recommend/job/list.json` 的参数装配 |
| [boss_filter/search.md](boss_filter/search.md) | `boss_filter.search` | 搜索维度拼成 `joblist.json` 认的查询串参数 |
| [boss_filter/tables.md](boss_filter/tables.md) | `boss_filter.tables` | 筛选项兜底常量表，文案与 code 与线上接口逐项对齐 |
| [boss_jobs/cdp_capture.md](boss_jobs/cdp_capture.md) | `boss_jobs.cdp_capture` | CDP 网络抓包，含 WS 全帧落盘 |
| [boss_jobs/cdp_stoken.md](boss_jobs/cdp_stoken.md) | `boss_jobs.cdp_stoken` | `__zp_stoken__` 的真浏览器获取与过期判断 |
| [boss_jobs/chat.md](boss_jobs/chat.md) | `boss_jobs.chat` | MQTT over WebSocket + 手写 protobuf 帧的聊天协议 |
| [boss_jobs/cli.md](boss_jobs/cli.md) | `boss_jobs.cli` | CLI 帮助里被精简掉的检索流说明与 stoken 补令牌流程 |
| [boss_jobs/client.md](boss_jobs/client.md) | `boss_jobs.client` | 职位列表客户端：真实 wapi 路径 + 每页即时清洗入库 |
| [boss_jobs/config.md](boss_jobs/config.md) | `boss_jobs.config` | 接口路由、业务码、网关令牌常量的实测结论 |
| [boss_jobs/errors.md](boss_jobs/errors.md) | `boss_jobs.errors` | 「开聊提醒」配额弹窗的包体形状与码位语义 |
| [boss_jobs/models.md](boss_jobs/models.md) | `boss_jobs.models` | 职位 DTO 与即时清洗规则，含字段名不对称记录 |
| [boss_jobs/stoken.md](boss_jobs/stoken.md) | `boss_jobs.stoken` | `__zp_stoken__` 的来源、算法与全自动获取链路 |
| [boss_login/client.md](boss_login/client.md) | `boss_login.client` | 短信登录客户端完整流程 |
| [boss_login/config.md](boss_login/config.md) | `boss_login.config` | 接口地址、请求头、业务码常量的线上核实状态 |
| [boss_login/crypto.md](boss_login/crypto.md) | `boss_login.crypto` | AES 加密，复刻登录页 `re(plain, atob(E))` |
| [boss_login/qrcode.md](boss_login/qrcode.md) | `boss_login.qrcode` | APP 扫码登录，复刻 `BossAppScan` 流程 |
| [boss_login/verify.md](boss_login/verify.md) | `boss_login.verify` | 滑块验证协议：拉挑战、交本人解、组装票据 |
| [boss_web/cli.md](boss_web/cli.md) | `boss_web.cli` | 网页控制台命令行入口 |
| [boss_web/runtime.md](boss_web/runtime.md) | `boss_web.runtime` | 开发模式与打包 exe 两种形态的路径差异 |
| [boss_web/services/app_settings_store.md](boss_web/services/app_settings_store.md) | `boss_web.services.app_settings_store` | 用户级应用设置，状态库 `doc('app_settings')` |
| [boss_web/services/crawl_task.md](boss_web/services/crawl_task.md) | `boss_web.services.crawl_task` | 后台抓取任务：线程跑爬取，网页轮询进度 |
| [boss_web/services/deliver_task.md](boss_web/services/deliver_task.md) | `boss_web.services.deliver_task` | 一键投递：串行打招呼并发出招呼语 |
| [boss_web/services/greeting_prompt.md](boss_web/services/greeting_prompt.md) | `boss_web.services.greeting_prompt` | 招呼语生成约束：出厂默认 + 用户自定义，运行期注入模板 |
| [boss_web/services/llm_client.md](boss_web/services/llm_client.md) | `boss_web.services.llm_client` | OpenAI 兼容的 Chat Completions 客户端 |
| [boss_web/services/login_task.md](boss_web/services/login_task.md) | `boss_web.services.login_task` | 登录任务状态机，把阻塞回调接到网页表单 |
| [boss_web/services/match_task.md](boss_web/services/match_task.md) | `boss_web.services.match_task` | 匹配任务：简历 vs 岗位，并行调 LLM 出匹配度与招呼语 |
| [boss_web/services/resume_parser.md](boss_web/services/resume_parser.md) | `boss_web.services.resume_parser` | 简历 LLM 固定模板解析 |
| [boss_web/services/session_check.md](boss_web/services/session_check.md) | `boss_web.services.session_check` | 启动时拿库里凭证打一次用户信息接口验登录态 |
| [boss_web/services/troubleshoot.md](boss_web/services/troubleshoot.md) | `boss_web.services.troubleshoot` | 技术异常翻成人话 + Chrome 环境检测 |
| [boss_web/services/update_check.md](boss_web/services/update_check.md) | `boss_web.services.update_check` | 对着 Gitee 发行版比版本号 |
| [boss_web/services/updater.md](boss_web/services/updater.md) | `boss_web.services.updater` | 应用内自更新：下载 zip → 校验 → bat 换文件重启，推 WS 进度 |
| [boss_web/static/js/update.md](boss_web/static/js/update.md) | `boss_web/static/js/update.js` | 自更新前端：`showUpdateDone` 只刷新不关窗的三个坑 + 时序 |
| [packaging/make_zip.md](packaging/make_zip.md) | `packaging.make_zip` | 发行 zip 过滤规则与 Windows 打包坑 |
| [tools/mock_server.md](tools/mock_server.md) | `tools.mock_server` | 本地假服务端，离线演练登录/发码/滑块/筛选接口 |
| [tools/probe_expect.md](tools/probe_expect.md) | `tools.probe_expect` | 求职期望流五个待定点的 P3 实测校准 |
