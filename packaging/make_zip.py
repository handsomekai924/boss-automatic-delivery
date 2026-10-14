"""把 ``dist\\BossAutoDelivery\\`` 打成发行 zip。

产物：``dist\\BossAutoDelivery-{version}.zip``，**扁平布局**（zip 根 = exe + ``_internal/``），
用户解压进哪个目录，哪个目录就是安装目录；自更新的 bat 也是直接解压进安装目录。

版本号用正则读 ``boss_web/__init__.py``——不能 import 包，import 会把 boss_db 之类的
副作用全带起来（同 run_boss.py 的顺序讲究）。

跳过 ``data/``、``__pycache__``、``*.pdb``、嵌套 zip：dist 目录里可能残留上一次
运行生成的用户数据，绝不能打进发行包。但 ``_internal/base_library.zip`` 是
**唯一的例外**——PyInstaller 把 stdlib（含 ``encodings``）装在那个 zip 里，
「跳过 zip」顺手把它一起丢过，打出来的包装上去 exe 直接
``No module named 'encodings'`` 起不来（真踩过，见 :data:`KEEP_ZIP_NAMES`）。
"""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST_APP = ROOT / "dist" / "BossAutoDelivery"
EXE_NAME = "BossAutoDelivery.exe"

#: 这些绝不进发行包
SKIP_DIR_NAMES = {"data", "__pycache__", "chrome_profile", "logs"}
SKIP_SUFFIXES = {".pdb", ".part"}
#: 唯一允许进包的 zip。PyInstaller 的 stdlib（含 ``encodings``）就在它里面，
#: 少一个字节 exe 都起不来；而「跳过嵌套 zip」是按后缀一刀切的，所以这里必须
#: 单独开口子。
KEEP_ZIP_NAMES = {"base_library.zip"}

#: 产物里**必须有**的成员。写进 zip 之后再核一遍：光在源目录里存在不够，
#: 过滤规则（上面那些 SKIP_*）才是真正动刀的地方，翻车就翻在这。
REQUIRED_MEMBERS = (EXE_NAME, "_internal/base_library.zip")


def read_version() -> str:
    text = (ROOT / "boss_web" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.M)
    if not m:
        raise SystemExit("读不到 boss_web/__init__.py 里的 __version__")
    return m.group(1)


def iter_files(src: Path):
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
            pass  # 一层子目录里的同名 exe 也收（异常布局时不至于漏）
        yield path, rel


def _force_utf8_stdout() -> None:
    """Windows 控制台默认 GBK，print 里的「✓ / 个文件」会直接 ``UnicodeEncodeError``。

    打包脚本挂在 build.bat 末尾，print 炸了 build.bat 也就跟着红——zip 其实
    已经写出来了，用户却以为打包失败（真踩过）。和 ``boss_web.runtime`` 同款
    处理，但这里不 import 包（import 会拖副作用起来）。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 老 Python / 重定向到文件
            pass


def _packed(names: set[str], needle: str) -> bool:
    """zip 里有没有 ``needle``（根下或某个子目录下都算）。

    和 ``updater.validate_zip`` 同款宽容度：真出过一层 ``BossAutoDelivery/``
    包着 exe 的布局，按根路径硬比会误判成缺文件。
    """
    return any(n == needle or n.endswith("/" + needle) for n in names)


def _discard(out: Path) -> None:
    """坏包立刻删掉——留在 ``dist\\`` 里迟早被当成好包发出去。"""
    try:
        out.unlink()
    except OSError:
        pass


def main() -> int:
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
