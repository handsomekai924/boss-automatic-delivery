"""列出项目里所有模块的命令与子命令。

独立脚本：不依赖登录/筛选的业务逻辑，只 introspect 各模块 ``cli.build_parser()``
构造出来的 argparse 解析器，所以新增命令后不用改这里。

    python tools/cli_commands.py                # 人读表格
    python tools/cli_commands.py --json         # 机器可读
    python tools/cli_commands.py --module boss_filter
    python tools/cli_commands.py --options      # 连选项一起列
"""

from __future__ import annotations

import argparse
import importlib
import json
import pkgutil
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

#: 项目根（tools 的上一级），用来找包
ROOT = Path(__file__).resolve().parent.parent




@dataclass(frozen=True)
class Option:
    """一个命令行选项（``--json`` / ``--out PATH`` 这种）。"""

    flags: str
    help: str = ""
    default: str | None = None


@dataclass(frozen=True)
class Command:
    """一个子命令；``options`` 是它自己的选项（含父解析器并进来的）。"""

    name: str
    help: str = ""
    options: tuple[Option, ...] = ()


@dataclass(frozen=True)
class ModuleCli:
    """一个可执行模块：``python -m <name> <command> …``。"""

    name: str
    help: str = ""
    commands: tuple[Command, ...] = ()
    global_options: tuple[Option, ...] = field(default_factory=tuple)




def _option_from_action(action) -> Option | None:
    """把 argparse 的 action 压成 :class:`Option`；不是选项就返回 None。"""
    flags = " / ".join(action.option_strings or ())
    if not flags:
        return None
    default = action.default
    if default is None or default is argparse.SUPPRESS:
        shown = None
    elif isinstance(default, (str, int, float, bool)):
        shown = str(default)
    else:
        shown = repr(default)
    return Option(flags=flags, help=(action.help or "").strip(), default=shown)


def _options_of(parser: argparse.ArgumentParser) -> tuple[Option, ...]:
    """解析器上的普通选项（跳过子命令动作与 -h/--help）。"""
    out: list[Option] = []
    for action in parser._actions:  # noqa: SLF001 - argparse 没有公开的枚举接口
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            continue
        if action.dest == "help":
            continue
        opt = _option_from_action(action)
        if opt is not None:
            out.append(opt)
    return tuple(out)


def _subparsers_of(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    """取解析器上的子命令表（dest -> 子解析器）。"""
    for action in parser._actions:  # noqa: SLF001
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            return dict(action.choices)
    return {}


def _subcommand_help(action) -> dict[str, str]:
    """子命令的一行帮助。argparse 把它挂在 ``_choices_actions`` 上，不在解析器上。"""
    return {item.dest: (item.help or "").strip() for item in getattr(action, "_choices_actions", [])}


def walk_parser(parser: argparse.ArgumentParser, name: str, help_text: str = "") -> ModuleCli:
    """把一个 ``build_parser()`` 的结果摊平成 :class:`ModuleCli`。"""
    commands: list[Command] = []
    help_map: dict[str, str] = {}
    for action in parser._actions:  # noqa: SLF001
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            help_map = _subcommand_help(action)
            break

    for cmd_name, cmd_parser in _subparsers_of(parser).items():
        first = (cmd_parser.description or "").strip().splitlines()
        commands.append(
            Command(
                name=cmd_name,
                help=help_map.get(cmd_name) or (first[0] if first else ""),
                options=_options_of(cmd_parser),
            )
        )

    summary = (help_text or (parser.description or "")).strip().splitlines()
    return ModuleCli(
        name=name,
        help=summary[0] if summary else "",
        commands=tuple(commands),
        global_options=_options_of(parser),
    )




def discover_modules(only: str | None = None) -> list[ModuleCli]:
    """在项目根下找带 ``cli.build_parser`` 的包，逐个挖命令。

    :param only: 只看这个模块名（如 ``boss_filter``）；不给就全看。
    """
    # 本脚本可能从 tools/ 直接跑，项目根不一定在 sys.path 上
    root_str = str(ROOT)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)

    found: list[ModuleCli] = []
    for info in sorted(pkgutil.iter_modules([root_str]), key=lambda m: m.name):
        if only and info.name != only:
            continue
        if not info.ispkg:
            continue
        try:
            cli = importlib.import_module(f"{info.name}.cli")
        except Exception:  # 没有 cli 子模块，或导入失败——不当成命令行模块
            continue
        build = getattr(cli, "build_parser", None)
        if build is None:
            continue
        parser = build()
        doc = (sys.modules[cli.__name__].__doc__ or "").strip().splitlines()
        found.append(walk_parser(parser, info.name, doc[0] if doc else ""))
    return found




def _fmt_option(opt: Option) -> str:
    """拼一行选项。帮助里已写过「默认 …」就不再重复附加。"""
    text = f"{opt.flags}  {opt.help}".rstrip()
    if opt.default not in (None, "None") and "默认" not in (opt.help or ""):
        text += f"（默认 {opt.default}）"
    return text


def to_json(modules: list[ModuleCli], *, with_options: bool = False) -> str:
    payload = []
    for mod in modules:
        item = {"module": mod.name, "help": mod.help, "commands": []}
        for cmd in mod.commands:
            entry: dict = {"name": cmd.name, "help": cmd.help}
            if with_options:
                entry["options"] = [asdict(o) for o in cmd.options]
            item["commands"].append(entry)
        if with_options:
            item["globalOptions"] = [asdict(o) for o in mod.global_options]
        payload.append(item)
    return json.dumps(payload, ensure_ascii=False, indent=2)


def to_text(modules: list[ModuleCli], *, with_options: bool = False) -> str:
    lines: list[str] = []
    for mod in modules:
        lines.append(f"python -m {mod.name}  {('- ' + mod.help) if mod.help else ''}".rstrip())
        if not mod.commands:
            lines.append("  （没有子命令）")
            continue
        width = max(len(c.name) for c in mod.commands)
        for cmd in mod.commands:
            lines.append(f"  {cmd.name:<{width}}  {cmd.help}".rstrip())
            if with_options:
                for opt in cmd.options:
                    lines.append(f"      {_fmt_option(opt)}")
        if with_options and mod.global_options:
            lines.append("  全局选项:")
            for opt in mod.global_options:
                lines.append(f"      {_fmt_option(opt)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"




def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli_commands",
        description="列出项目里所有模块的命令与子命令",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--json", action="store_true", help="按 JSON 输出")
    parser.add_argument("--options", action="store_true", help="连选项一起列")
    parser.add_argument("--module", default=None, help="只看这个模块（如 boss_filter）")
    return parser


def main(argv: list[str] | None = None) -> int:
    _force_utf8_streams()
    args = build_parser().parse_args(argv)
    modules = discover_modules(only=args.module)
    if not modules:
        target = f"模块 {args.module}" if args.module else "项目根"
        print(f"✗ 在{target}下没找到带 cli.build_parser 的包", file=sys.stderr)
        return 2
    text = to_json(modules, with_options=args.options) if args.json else to_text(
        modules, with_options=args.options
    )
    sys.stdout.write(text)
    return 0


def _force_utf8_streams() -> None:
    """Windows 控制台默认 GBK，中文帮助会炸；统一按 UTF-8 出。"""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        buffer = getattr(stream, "buffer", None)
        if buffer is not None and getattr(stream, "encoding", "").lower() != "utf-8":
            setattr(
                sys,
                name,
                __import__("io").TextIOWrapper(buffer, encoding="utf-8", errors="replace"),
            )


if __name__ == "__main__":
    raise SystemExit(main())
