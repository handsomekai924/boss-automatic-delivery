# `boss_web.services.updater` 协议笔记

> 摘自 `boss_web/services/updater.py` 的模块文档字符串 / `#` 注释。

## 一句话

应用内自更新：下载 zip → 校验 → 交给 bat 停服/换文件/重启，全程推 WS 进度。

## 细节

为什么要 bat：Windows 上运行中的 exe 和 `_internal\` 里的 DLL 都被锁着，
**自己换不掉自己**。所以 Python 只负责「下载 + 校验 + 生成脚本 + 转发进度 +
退出」，替换动作交给独立的 cmd 进程。

与 bat 的约定（都放在 `%TEMP%\BossAutoDelivery-update\`）：

| 文件 | 写入方 | 格式 |
| --- | --- | --- |
| `*.zip` | Python | 更新包本体 |
| `extract\` | bat | 解压暂存（确认有 exe 才动安装目录） |
| `progress.log` | bat | 行式 `stage\|percent\|text`（ASCII） |
| `update.log` | bat | 人话诊断，失败时用户看这个 |
| `done.json` | bat | 成功回执 `{"ok":true,"version":...}`，新进程读一次 |

bat **先验证后破坏**：zip 不在/解压不出 exe 就直接退出，安装目录一个文件都不碰。

### 常量

- 更新工作目录名挂在 %TEMP% 下，不在安装目录里——安装目录待会儿要被清。
- 更新包大小上限。发行包 ~30MB，给 100 倍冗余防意外。
- `DOWNLOAD_TIMEOUT` 是 `(连接超时, 读超时)`。
- `done.json` 的有效期：太久远的回执不当真（免得旧标记误触发「更新完成」）。
- bat 的 stage → 界面中文。bat 本体保持纯 ASCII（路径/中文塞进 bat 极易出事）。
- 服务监听的端口：bat 重启时要按**原端口**拉起来，浏览器标签页才能重连上。
- bat 正文纯 ASCII。路径/端口/版本一律走环境变量——中文与空格路径写进 bat 是事故高发区。
- 要展示的过程日志（bat 用 `>>` 追加写，永不截断）。
- 诊断页默认读的尾部字节数；用户可在接口上再要大一点。

### `_BAT_TEMPLATE` 注释（写在 bat 内部）

bat guard 3：onedir package must carry `_internal\base_library.zip`。
PyInstaller keeps the whole stdlib (encodings, codecs...) in that zip.
Without it the swapped-in exe dies at startup with
"No module named 'encodings'", leaving the user with an app that cannot
open. Refuse the package BEFORE the wipe - an old app beats no app.

### `spawn_bat`

必须给它一个真实存在（但没窗口）的控制台。

不能用 `DETACHED_PROCESS`：那样 cmd 完全没有控制台，bat 里**任何管道都会
永久挂死**——`tasklist ... | findstr ...` 正好是停止等待循环的第一句，更新
会一声不响地停在 85%；同一环境里 `tasklist` 重定向到文件也是空输出、
`pause` 也会挂住。`CREATE_NO_WINDOW` 给的是真控制台、只是没窗口，
管道 / `tasklist` / `pause` 全部正常；失败原因看 `update.log`（页面能看）。

### `request_exit`

用 `should_exit`（uvicorn 官方停止位）让监听端口干净释放，新进程才能绑回
**同一端口**——浏览器标签页就是靠这个重连上来的。`os._exit` 保证即使有
请求挂着也一定会死，文件锁才能放开给 bat 换文件。

### `_run_update_job`

端口优先用调用方给的，其次启动时记下的——一定要传给 bat，
否则新进程会另挑一个端口，浏览器那个标签页就再也连不回来了。

bat 解压阶段旧服务还活着，进度从 `progress.log` 转发到 WS。
