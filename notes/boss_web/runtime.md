# `boss_web.runtime` 协议笔记

> 摘自 `boss_web/runtime.py` 的模块文档字符串 / `#` 注释。

## 一句话

运行环境探测：把「开发模式」和「打包成 exe」两种形态的路径差异收在一处。

## 细节

只依赖标准库，方便单测；对环境变量名的引用一律延迟导入，免得为了拿两个字符串就把 `boss_jobs`（连带 requests / websocket-client）整条依赖链拖进来。

两种形态的差别只有两点：

============  ==========================  ================================
             开发（`python -m boss_web`）  打包（双击 exe）
============  ==========================  ================================
只读资源      仓库根                        PyInstaller 解包目录 `_MEIPASS`
可写数据      仓库 `data/`                exe 同目录 `data/`（便携）
============  ==========================  ================================

打包是 **onedir**（`BossAutoDelivery.exe` + `_internal\\`）。onedir 下 `sys._MEIPASS` 就是 `<安装目录>\\_internal`，所以 `resource_root` / `STATIC_DIR` 照常；`executable_dir` 是安装目录，`app_data_dir` 是它旁边的 `data\\`。自更新（`boss_web.services.updater`）只换程序文件，`data\\` 原样保留——所以**不能**改回 onefile（那个解包到 `%TEMP%`，换不动）。

打包形态下**必须**调一次 `bootstrap`（在任何 store / Chrome 调用之前），否则 `boss_db` 会把库建到 `_MEIPASS` 里——那个目录进程一退就被清空，用户的简历、登录态、职位全没。

### 常量

- `DATA_DIR_NAME`：数据目录名。与仓库既有的 `data/` 同名，README 里「拷走 data 文件夹就是备份」的说法在打包后依然成立。
- `APP_DIR_NAME`：退回用户目录时用的子目录名。
- `APP_DATA_ENV`：覆盖数据目录的钩子。设了它，开发态也能强制走打包态的路径逻辑（便于验证）。

### `resource_root`

显式用 `_MEIPASS` 而不是靠模块 `__file__`，是因为后者在冻结环境下的行为依赖 PyInstaller 的实现细节。

### `executable_dir`

onefile 打出来的包，`sys.executable` 指向**用户双击的那个 exe**（不是解包临时目录），所以拿它定位「exe 同目录」是对的。

### `fallback_data_dir`

exe 同目录写不了时的备选（`%LOCALAPPDATA%\\BossAutoDelivery\\data`）。常见于 exe 被放进 `C:\\Program Files` 或只读盘。

### `is_temp_run`

exe 是不是在临时目录里跑——多半是**直接从压缩包预览双击**。那种情况下 Windows 先解压到 `%TEMP%` 再运行，数据会写进临时目录，关掉就没。这个信号用来在界面上提示用户「先解压再用」。

### `bootstrap`

开发态什么都不设（继续用仓库 `data/`，行为与改造前完全一致），除非显式 `force=True` 或设了 `APP_DATA_ENV`。打包态一律设为 `<exe 同目录>/data/`；写不进去就自动退到用户目录。用 `setdefault`：用户（或测试）显式设过的环境变量优先。

退到了备选目录就得让界面告诉用户「东西存哪了」，否则找不到备份（`info["portable"] = "0"`）。
