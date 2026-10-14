# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包定义：onedir 目录包 + zip 发行。

构建（在仓库根目录执行）::

    packaging\\build.bat

产物：``dist\\BossAutoDelivery\\``（exe + ``_internal\\``）和
``dist\\BossAutoDelivery-{version}.zip``（发到 Gitee Release 附件）。

要点全在注释里，改之前先看 :mod:`boss_web.runtime` 的模块 docstring。
"""

import importlib.util
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

#: segno 只在 boss_login.qrcode 的函数体里 try/except 着延迟导入，静态分析标成
#: 「delayed, optional」，venv 漏装也照样出 exe——用户那边就是二维码裂图、黑窗
#: 一点报错都没有（真机踩过）。构建期主动挡一道，别把「能跑但出不了图」的包发出去。
if importlib.util.find_spec("segno") is None:
    raise SystemExit(
        "构建环境缺少 segno，先装再打包：\n"
        '  .venv\\Scripts\\python.exe -m pip install "segno>=1.6.0"'
    )

#: spec 文件所在目录由 PyInstaller 注入；仓库根是它的上一级。
#: 用绝对路径而不是相对路径，免得受构建时 cwd / --workpath 影响。
ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))

# uvicorn 用 importlib 按字符串挑「事件循环 / HTTP / WebSocket / lifespan」实现，
# 静态分析一个都看不到。漏掉哪个，exe 都是「双击就闪退」。
hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    # uvicorn 的可选加速件（装了 uvicorn[standard] 就在），走扩展模块
    "httptools",
    "h11",
    "websockets",
    "watchfiles",
    "anyio._backends._asyncio",
    # 运行期依赖：有的延迟导入，有的在包内再导入自己的子模块
    "paho.mqtt.client",
    "websocket",
    # 漏了 certifi = 所有 HTTPS 请求 CERTIFICATE_VERIFY_FAILED，
    # 而且报错指向 SSL 不指向打包，极难排查。必须显式带上。
    "certifi",
    "cryptography",
    "multipart",
    "python_multipart",
    "python_docx",  # Word 简历解析
    "pypdf",  # PDF 简历解析
    # 画二维码 PNG。boss_login.qrcode 是函数内 try 导入，PyInstaller 标 optional
    # 就会漏；没进包 = 扫码登录永远裂图（响应 502 的 JSON 被 <img> 当坏图显示）。
    "segno",
    # 这两个包的子模块是靠 python-docx 内部结构间接导入的，静态分析容易漏；
    # lxml 是 python-docx 的 C 扩展后端，漏了 Word 上传会在运行时才炸。
    "lxml",
    "lxml.etree",
    "docx.opc.constants",
    "docx.oxml.ns",
    "docx.table",
    "docx.text.paragraph",
    "pypdf._crypt_providers",
    # 入口脚本里的 boss_web.cli 是「先 bootstrap 后 import」，显式兜一层
    "boss_web.cli",
    "boss_web.app",
    "boss_web.api",
] + collect_submodules("uvicorn")

# (源, 打包后相对路径)。静态资源整目录打进去——前端和 LLM 引导截图都在里面，
# 漏了的话 app.py 的 if static_dir.exists() 为假，首页/SPA 全都不注册，浏览器开出来是空壳。
datas = [
    (os.path.join(ROOT, "boss_web", "static"), "boss_web/static"),
    # stoken.py 的 Node 备用算法链要用；生产走 CDP 用不到，但缺了会抛路径错
    (os.path.join(ROOT, "boss_jobs", "assets", "run_abc.js"), "boss_jobs/assets"),
    # python-docx 自带空白模板，缺了会在 Document() 时报找不到文件
    *collect_data_files("docx"),
]

a = Analysis(
    [os.path.join(ROOT, "run_boss.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "numpy",
        "PIL",
        "pytest",
        "tests",
        "tools",
        "setuptools",
        "pkg_resources",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

# onedir：exe 与 _internal\\ 平级。自更新时 bat 只换 install 目录下的程序文件，
# 旁边的 data\\（登录态/职位库）原样保留——所以不能用 onefile（那个解包到 %TEMP%）。
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BossAutoDelivery",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX 压过的 DLL 常被杀软误报成木马，省下的体积不值这个麻烦
    console=True,  # 留黑窗：出问题有处可看，Ctrl+C 能退出
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="BossAutoDelivery",
)
