"""打包脚本的回归护栏：钉死 build.bat / make_zip.py 上次踩过的坑。"""

from __future__ import annotations

import importlib.util
import os
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILD_BAT = ROOT / "packaging" / "build.bat"
MAKE_ZIP = ROOT / "packaging" / "make_zip.py"


def _load_make_zip():
    """按文件路径加载打包脚本——不能让 `import packaging.make_zip` 去碰真 dist。"""
    spec = importlib.util.spec_from_file_location("packaging_make_zip", MAKE_ZIP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_dist(root: Path, *, with_base_library: bool = True) -> Path:
    """造一棵假 `dist\\BossAutoDelivery\\`，带可读版本号；文件用随机字节避开「产物太小」守卫。"""
    (root / "boss_web").mkdir(parents=True, exist_ok=True)
    (root / "boss_web" / "__init__.py").write_text(
        '__version__ = "9.9.9"\n', encoding="utf-8"
    )
    app = root / "dist" / "BossAutoDelivery"
    (app / "_internal").mkdir(parents=True)
    (app / "BossAutoDelivery.exe").write_bytes(b"MZ" + os.urandom(4096))
    if with_base_library:
        (app / "_internal" / "base_library.zip").write_bytes(b"PK" + os.urandom(4096))
    return app


def test_build_batt必须是_crlf_且纯_ascii():
    """验证 build.bat 是 CRLF 行尾且全文纯 ASCII。"""
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
    """验证打包脚本在跑 PyInstaller 前先清空 dist。"""
    text = BUILD_BAT.read_bytes().decode("ascii")
    wipe = text.index('rd /s /q "dist"')
    pyinstaller = text.index("-m PyInstaller")
    assert wipe < pyinstaller, "清 dist 必须发生在 PyInstaller 之前"
    after = text[wipe:]
    assert 'if exist "dist"' in after.split("PyInstaller")[0]
    assert "exit /b 1" in after.split("PyInstaller")[0]


def test_build_bat_先建前端再打包():
    """前端没建就打包 = 双击开白屏；build.bat 必须先 pnpm build，缺 node 时说人话。"""
    text = BUILD_BAT.read_bytes().decode("ascii")
    pnpm_build = text.index("pnpm run build")
    pyinstaller = text.index("-m PyInstaller")
    assert pnpm_build < pyinstaller, "前端构建必须发生在 PyInstaller 之前"
    assert "where pnpm" in text, "先探 pnpm 在不在"
    assert "pnpm not found" in text, "缺 node 环境要报可读错误"


def test_make_zip_把_stdout_切成_utf8():
    """验证 make_zip 把 stdout 切成 UTF-8。"""
    src = MAKE_ZIP.read_text(encoding="utf-8")
    assert 'reconfigure(encoding="utf-8"' in src, "GBK 控制台下 print 会炸，整次打包跟着红"


def test_make_zip_不能丢掉_base_library_zip(tmp_path):
    """验证 base_library.zip 不会被「跳过嵌套 zip」误删——少了它整包作废。"""
    spec = importlib.util.spec_from_file_location("packaging_make_zip", MAKE_ZIP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    app = tmp_path / "BossAutoDelivery"
    (app / "_internal").mkdir(parents=True)
    (app / "BossAutoDelivery.exe").write_bytes(b"MZ")
    (app / "_internal" / "base_library.zip").write_bytes(b"PK")
    (app / "_internal" / "stale.zip").write_bytes(b"PK")
    (app / "_internal" / "boss_web.pdb").write_bytes(b"x")
    (app / "data").mkdir()
    (app / "data" / "boss.db").write_bytes(b"x")

    rels = {rel.as_posix() for _, rel in mod.iter_files(app)}
    assert "_internal/base_library.zip" in rels, "少了它整包作废"
    assert "_internal/stale.zip" not in rels
    assert "_internal/boss_web.pdb" not in rels
    assert not any(r.startswith("data/") for r in rels)


def test_make_zip_好包照常出且自我核对通过(tmp_path, monkeypatch):
    """护栏不能误伤正常打包：该出的包还得出来。"""
    mod = _load_make_zip()
    app = _fake_dist(tmp_path)
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "DIST_APP", app)

    assert mod.main() == 0

    out = tmp_path / "dist" / "BossAutoDelivery-9.9.9.zip"
    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
    assert "BossAutoDelivery.exe" in names
    assert "_internal/base_library.zip" in names


def test_make_zip_产物缺_base_library_就拒绝发货(tmp_path, monkeypatch):
    """验证产物缺 base_library.zip 时报错并删掉 zip，绝不留在 dist 里。"""
    mod = _load_make_zip()
    app = _fake_dist(tmp_path, with_base_library=False)
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "DIST_APP", app)

    with pytest.raises(SystemExit) as ei:
        mod.main()

    assert "base_library.zip" in str(ei.value)
    assert not (tmp_path / "dist" / "BossAutoDelivery-9.9.9.zip").exists(), (
        "坏包必须删掉，不能留在 dist 里"
    )
