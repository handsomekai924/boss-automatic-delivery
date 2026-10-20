# `tests.test_packaging_scripts` 协议笔记

> 摘自 `tests/test_packaging_scripts.py` 的模块文档字符串与函数注释。

## 一句话

打包脚本的回归护栏：bat/shell 单测跑不了真构建，把上次踩过的坑钉死。

## 细节

* `build.bat` 必须 CRLF + 纯 ASCII——LF-only 或中文会把 cmd 解析撕碎，
  表现是 `'yInstaller' is not recognized` 这种碎片报错（真踩过）。
* 打包前必须清空 `dist\`——旧布局的 onefile exe / `data\` 混在产物里，
  用户会把错的东西发出去（真踩过）。
* `make_zip.py` 必须把 stdout 切成 UTF-8——GBK 控制台下 `✓` 会
  `UnicodeEncodeError`，zip 写完了脚本却报红（真踩过）。
* `base_library.zip` 必须进包：PyInstaller 的 stdlib（含 `encodings`）全装在里面。
  曾经「跳过嵌套 zip」按后缀一刀切把它丢过，打出来的包装上去 exe 直接
  `ModuleNotFoundError: No module named 'encodings'`——自更新换了文件之后
  程序就再也开不了（真踩过）。
* 产物自检：源目录里有没有不算数，过滤规则才是真正动刀的地方。缺
  `base_library.zip` 的包要报错并把 zip 删掉，绝不留在 `dist\` 里等人发
  （真踩过，还是发到 Gitee 上去了）。
