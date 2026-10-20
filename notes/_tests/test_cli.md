# `tests.test_cli` 协议笔记

> 摘自 `tests/test_cli.py` 的测试 docstring。

## 一句话

滑块挑战失败时要报风控话术，并且不能再把滑块票据说成走 `--verify-token`。

## 细节

### 模块

单独成一个模块是有原因的：cli.py 曾经把一个不存在于 config 的名字导入进来
（`from .config import DEFAULT_SESSION_PATH`，当时还没有这个名字），整套测试却
全绿——因为没有任何一个测试 import 过 cli。库是对的，入口是坏的，而入口没人跑。

### `seed_code`

CLI 只保留 login/whoami/logout/probe，没有独立的发码命令；login 的
`--code` 非交互路径要求事先就知道验证码，所以这一步从库里发。
换成单独的客户端实例是有意的：不会把票据/冷却状态带进随后的 CLI 登录。

### `test_slider_challenge_reports_risk_not_generic_error`

真实站点实测形状：code=400061 / "请完成滑块验证"。

修之前这里会退化成「接口返回失败（code=400061）」，用户完全不知道下一步该干什么。

处理方式那段曾经错写成「用 --verify-token 传回来」——那是 SECURITY_CHECK(37) 的路，滑块的票据是 challenge/validate/seccode 三件套、由帮助页自动回传，压根不走 `--verify-token`。照着那句去做只会白跑一趟，所以这里钉住它**不**再出现。

`--no-helper` 是关键：不给的话 CLI 会拉起本地帮助页等人拖滑块，测试就挂在那里等 300 秒超时了。

### `test_verify_token_does_not_unblock_the_slider`

`--verify-token` 是 SECURITY_CHECK(37) 那条路的，不是滑块的。

滑块票据只有表单三键 `challenge` / `validate` / `seccode`，而且必须挂在
`smsCodeV2` 上。拿 `verifyToken` 去，真机照样 400061。

早期假服务端把 `verifyToken` 当滑块票据，这条死路就被测成了绿的；
契约以登录页 chunk 为准。`--no-helper` 顺带保证不会挂 300 秒等人拖滑块。

### `test_login_auto_solves_slider_on_the_send_step`

登录成功后验证码一次性失效，STORE 会把这条记录清掉——所以看的是发码请求。
重试那次必须打 smsCodeV2 并带着极验三件套——只调 validate 过不去。

### `test_login_auto_solves_slider_and_retries_phone_v2`

登录那步跟发码一样要过滑块：重试打 phoneV2，票据在表单里。

真机跑出来的形状是「发码过了、登录仍 400061」——登录页 Ce() 把 verifyInfo
原样挂到 login 上，所以那一步也必须自带票据。je() 换成了 encryptedAccount，
所以表单里没有 `phone`；Ce() 不传 `t`，所以没有 `version`。

### `test_default_db_lives_in_project_data_dir`

状态库曾经在 `~/.boss_login/` 下，换个目录跑 `python -m boss_login` 就会
读写错文件——whoami 报「本地没有登录态」，而登录明明刚成功。

### `test_announce_survives_gbk_console`

Windows 控制台是 GBK，emoji 会抛 UnicodeEncodeError。

假服务端曾在写完冷却记录之后打印验证码，编码失败直接把请求打成 500，
客户端一重试就撞上刚写进去的限流——一个纯日志问题伪装成业务故障。
