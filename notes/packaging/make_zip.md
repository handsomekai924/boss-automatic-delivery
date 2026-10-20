# `packaging.make_zip` 协议笔记

> 摘自 `packaging/make_zip.py` 的模块文档字符串 / `#:` 注释 / 文档字符串。

## 一句话

把 `dist\BossAutoDelivery\` 打成发行 zip 的过滤规则、`base_library.zip` 例外、以及打包脚本的 Windows 控制台/布局坑（真踩过）。

## 细节

### 模块：产物布局与版本号

产物：`dist\BossAutoDelivery-{version}.zip`，**扁平布局**（zip 根 = exe + `_internal/`），用户解压进哪个目录，哪个目录就是安装目录；自更新的 bat 也是直接解压进安装目录。

版本号用正则读 `boss_web/__init__.py`——不能 import 包，import 会把 boss_db 之类的副作用全带起来（同 run_boss.py 的顺序讲究）。

### `KEEP_ZIP_NAMES` / 过滤规则

跳过 `data/`、`__pycache__`、`*.pdb`、嵌套 zip：dist 目录里可能残留上一次运行生成的用户数据，绝不能打进发行包。但 `_internal/base_library.zip` 是**唯一的例外**——PyInstaller 把 stdlib（含 `encodings`）装在那个 zip 里，「跳过 zip」顺手把它一起丢过，打出来的包装上去 exe 直接 `No module named 'encodings'` 起不来（真踩过）。

「跳过嵌套 zip」是按后缀一刀切的，所以 `KEEP_ZIP_NAMES` 必须单独开口子。

### `REQUIRED_MEMBERS`

产物里**必须有**的成员。写进 zip 之后再核一遍：光在源目录里存在不够，过滤规则（`SKIP_*`）才是真正动刀的地方，翻车就翻在这。

### `iter_files`

一层子目录里的同名 exe 也收（异常布局时不至于漏）。

### `_force_utf8_stdout`

Windows 控制台默认 GBK，print 里的「✓ / 个文件」会直接 `UnicodeEncodeError`。

打包脚本挂在 build.bat 末尾，print 炸了 build.bat 也就跟着红——zip 其实已经写出来了，用户却以为打包失败（真踩过）。和 `boss_web.runtime` 同款处理，但这里不 import 包（import 会拖副作用起来）。

### `_packed`

和 `updater.validate_zip` 同款宽容度：真出过一层 `BossAutoDelivery/` 包着 exe 的布局，按根路径硬比会误判成缺文件。

### `_discard`

坏包立刻删掉——留在 `dist\` 里迟早被当成好包发出去。

### `main` 失败分支

缺 `base_library.zip` 的包换上去，exe 起来就 `No module named 'encodings'`——用户更新「成功」完却再也打不开程序。报错、把 zip 删掉，绝不留在 `dist\` 里等人发。
