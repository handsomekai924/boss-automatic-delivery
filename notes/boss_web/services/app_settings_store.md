# `boss_web.services.app_settings_store` 协议笔记

> 摘自 `boss_web/services/app_settings_store.py` 的模块文档字符串 / `#` 注释。

## 一句话

用户级应用设置：状态库 `doc('app_settings')`。

## 细节

目前三项：

- `chrome_mode`——Chrome 窗口档位。为什么它：`boss_jobs` 默认用
  `hidden`（屏幕外 + Win32 隐藏）拉 Chrome，用户不必被弹一脸窗口——
  可一旦账号命中风控、**需要人工过滑块**，隐藏的窗口连任务栏里都没有，人是
  够不着的。打包给非技术用户后必须有个界面开关能把它切成 `visible`。
- `ignored_update_version`——用户点过「忽略此版本」的版本号，见
  `boss_web.services.update_check`。
- `greeting_requirement`——自定义招呼语生成约束。空串 = 用
  `boss_web.services.greeting_prompt.GREETING_REQUIREMENT` 出厂默认；
  非空 = 用户在「系统配置 → 招呼语」改写的那段。组装提示词时现读，
  保存即生效（见 `apply_greeting_requirement`）。

做法照搬 `boss_web.services.deliver_config_store`：整包存 JSON、读时归一
化、坏值不报错只回默认。

**时效坑**：`boss_jobs.cdp_stoken._mode()` 是**调用时**才读环境变量，所以这
里一改就得立刻同步到 `os.environ`，否则要重启程序才生效——见
`apply_chrome_mode`。

### `apply_ignored_update_version`

忽略是**水位线**，不是往列表里追加：忽略 `1.2.0` 再忽略 `1.3.0` 只留下
`1.3.0`，`≤ 1.3.0` 全部静音。要更细粒度得存列表，但那会让「已经手滑忽略过
一个更新的版本，中间的旧版又跳出来」这种事发生，不是用户要的。

### `restore_on_startup`

启动时把存过的档位写回环境变量。必须在任何 Chrome 启动调用之前跑
（`cli.main` 里紧跟 `bootstrap()`）。
