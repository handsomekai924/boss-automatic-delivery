"""打包脚本的回归护栏。

这些是 bat/shell 脚本，单测跑不了真构建，但可以把「上次踩过的坑」钉死：

* ``build.bat`` 必须 CRLF + 纯 ASCII——LF-only 或中文会把 cmd 解析撕碎，
  表现是 ``'yInstaller' is not recognized`` 这种碎片报错（真踩过）。
* 打包前必须清空 ``dist\\``——旧布局的 onefile exe / ``data\\`` 混在产物里，
  用户会把错的东西发出去（真踩过）。
* ``make_zip.py`` 必须把 stdout 切成 UTF-8——GBK 控制台下 ``✓`` 会
  ``UnicodeEncodeError``，zip 写完了脚本却报红（真踩过）。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD_BAT = ROOT / "packaging" / "build.bat"
MAKE_ZIP = ROOT / "packaging" / "make_zip.py"


def test_build_batt必须是_crlf_且纯_ascii():
    data = BUILD_BAT.read_bytes()
    crlf = data.count(b"\r\n")
    bare_lf = data.count(b"\n") - crlf
    assert crlf > 0
    assert bare_lf == 0, "LF-only bat 会被 cmd 撕碎"
    assert data.count(b"\r") == crlf, "不允许混用裸 CR"
    assert all(b < 128 for b in data), "bat 里出现非 ASCII 会在切换代码页时拆行"


def test_build_bat_rem_行引号必须成对():
    """cmd 会数 rem 行里的双引号；奇数个就把后面几行当成字符串内容。"""
    text = BUILD_BAT.read_bytes().decode("ascii")
    for line in text.split("\r\n"):
        if line.lower().startswith("rem"):
            assert line.count('"') % 2 == 0, f"rem 行引号不成对: {line!r}"


def test_打包前必须清空_dist():
    text = BUILD_BAT.read_bytes().decode("ascii")
    wipe = text.index('rd /s /q "dist"')
    pyinstaller = text.index("PyInstaller")
    assert wipe < pyinstaller, "清 dist 必须发生在 PyInstaller 之前"
    # 删失败要停下来，不能带着脏 dist 继续打
    after = text[wipe:]
    assert 'if exist "dist"' in after.split("PyInstaller")[0]
    assert "exit /b 1" in after.split("PyInstaller")[0]


def test_make_zip_把_stdout_切成_utf8():
    src = MAKE_ZIP.read_text(encoding="utf-8")
    assert 'reconfigure(encoding="utf-8"' in src, "GBK 控制台下 print 会炸，整次打包跟着红"
