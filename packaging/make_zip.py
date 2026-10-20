"""把 `dist\\BossAutoDelivery\\` 打成发行 zip。

产物扁平布局（zip 根 = exe + `_internal/`），解压进哪个目录哪个就是安装目录。
版本号用正则读 `boss_web/__init__.py`，不 import 包。

Attributes:
    SKIP_DIR_NAMES: 不进发行包的目录名。
    SKIP_SUFFIXES: 不进发行包的文件后缀。
    KEEP_ZIP_NAMES: 唯一允许进包的嵌套 zip；`base_library.zip` 装着 PyInstaller 的
        stdlib（含 `encodings`），缺了 exe 起不来。
    REQUIRED_MEMBERS: 产物里必须有的成员，写进 zip 后再核一遍。
"""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST_APP = ROOT / "dist" / "BossAutoDelivery"
EXE_NAME = "BossAutoDelivery.exe"

SKIP_DIR_NAMES = {"data", "__pycache__", "chrome_profile", "logs"}
SKIP_SUFFIXES = {".pdb", ".part"}
KEEP_ZIP_NAMES = {"base_library.zip"}

REQUIRED_MEMBERS = (EXE_NAME, "_internal/base_library.zip")


def read_version() -> str:
    """从 `boss_web/__init__.py` 里正则抠出版本号；读不到时抛 `SystemExit`。"""
    text = (ROOT / "boss_web" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.M)
    if not m:
        raise SystemExit("读不到 boss_web/__init__.py 里的 __version__")
    return m.group(1)


def iter_files(src: Path):
    """列出 src 下该打进包的文件，产出 `(path, 相对路径)`。"""
    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(src)
        if any(part in SKIP_DIR_NAMES for part in rel.parts[:-1]):
            continue
        suffix = path.suffix.lower()
        if suffix in SKIP_SUFFIXES:
            continue
        if suffix == ".zip" and path.name.lower() not in KEEP_ZIP_NAMES:
            continue
        if path.name == EXE_NAME and rel.parent != Path("."):
            pass
        yield path, rel


def _force_utf8_stdout() -> None:
    """把 stdout/stderr 切成 UTF-8，避开 GBK 控制台的 `UnicodeEncodeError`。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 老 Python / 重定向到文件
            pass


def _packed(names: set[str], needle: str) -> bool:
    """zip 里有没有 `needle`（根下或某个子目录下都算）。"""
    return any(n == needle or n.endswith("/" + needle) for n in names)


def _discard(out: Path) -> None:
    """坏包立刻删掉，避免被当成好包发出去。"""
    try:
        out.unlink()
    except OSError:
        pass


def main() -> int:
    """打发行 zip 并自检关键成员；空包或缺件时删掉产物并抛 `SystemExit`。"""
    _force_utf8_stdout()
    if not (DIST_APP / EXE_NAME).is_file():
        raise SystemExit(f"找不到 {DIST_APP / EXE_NAME}，先跑 PyInstaller")
    if not (DIST_APP / "_internal").is_dir():
        raise SystemExit(f"找不到 {DIST_APP / '_internal'}，这个产物不是 onedir 布局")

    version = read_version()
    out = ROOT / "dist" / f"BossAutoDelivery-{version}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()

    count = 0
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path, rel in iter_files(DIST_APP):
            zf.write(path, arcname=str(rel).replace("\\", "/"))
            count += 1
        names = set(zf.namelist())

    if count == 0 or out.stat().st_size < 1024:
        _discard(out)
        raise SystemExit("打出来的 zip 是空的，检查 dist\\BossAutoDelivery\\ 内容")

    missing = [n for n in REQUIRED_MEMBERS if not _packed(names, n)]
    if missing:
        _discard(out)
        raise SystemExit(
            "产物里缺关键文件，这个包换上去程序会起不来，已删掉、不许发：\n  "
            + "\n  ".join(missing)
            + "\n（base_library.zip 装的是 PyInstaller 的整个 stdlib，含 encodings；"
            "少了它 exe 启动就 No module named 'encodings'）"
        )

    size_mb = out.stat().st_size / (1024 * 1024)
    print(f"✓ {out.name}  {count} 个文件  {size_mb:.1f} MB")
    print(f"  内容根：{EXE_NAME} + _internal\\（扁平布局，解压进哪个目录哪个就是安装目录）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
