"""把 Word / PDF 简历抽成纯文本，让它和 .md / .txt 走同一条解析流水线。

普通用户的简历基本都是 Word 或 PDF，只有这个仓库的作者用 Markdown。所以上传
这一步必须能吃下二进制文档，**抽完之后的流程一行都不用改**——下游拿到的还是
一段纯文本。

依赖 ``python-docx`` / ``pypdf`` 都是**函数内才 import**：装不上时只让「上传
Word/PDF」这一个动作失败，且失败信息告诉用户怎么绕（复制到记事本另存 .txt），
而不是让整个服务起不来。
"""

from __future__ import annotations

import io
import re

#: 直接解码的纯文本后缀
TEXT_EXTS = {".md", ".markdown", ".txt"}
#: 需要抽正文的二进制文档后缀
BINARY_EXTS = {".docx", ".pdf"}
ALLOWED_EXT = TEXT_EXTS | BINARY_EXTS


class DocumentTextError(Exception):
    """抽不出文字，而且原因该讲给用户听。"""


def ext_of(filename: str) -> str:
    """取小写后缀（含点）。没有后缀返回空串。"""
    name = (filename or "").strip().lower()
    dot = name.rfind(".")
    return name[dot:] if dot > 0 else ""


def _tidy(text: str) -> str:
    """统一换行、去空字节、压掉三连空行——LLM 读起来干净，也省 token。"""
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# --------------------------------------------------------------------------- #
# Word
# --------------------------------------------------------------------------- #


def extract_docx(raw: bytes) -> str:
    try:
        from docx import Document
        from docx.oxml.ns import qn
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError as exc:  # pragma: no cover - 依赖没装
        raise DocumentTextError(
            "程序缺少读取 Word 的组件。请改用纯文本上传："
            "在 Word 里全选复制，粘贴进记事本，另存为 .txt 再传。"
        ) from exc

    try:
        document = Document(io.BytesIO(raw))
    except Exception as exc:
        raise DocumentTextError(
            "这个 Word 文件读不出来。如果它是老版本的 .doc，"
            "请用 Word 打开后「另存为」.docx 再传；也可能是文件本身损坏了。"
        ) from exc

    # 简历常用表格排版，所以正文和表格要**按文档顺序**取，不能先段落再表格——
    # 那样经历和技能会串位。这是 python-docx 官方 recipe 的做法。
    parts: list[str] = []
    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            line = Paragraph(child, document).text.strip()
            if line:
                parts.append(line)
        elif child.tag == qn("w:tbl"):
            for row in Table(child, document).rows:
                cells: list[str] = []
                for cell in row.cells:
                    value = cell.text.strip()
                    # 合并单元格会被重复列出来，去掉相邻重复
                    if value and (not cells or cells[-1] != value):
                        cells.append(value)
                if cells:
                    parts.append(" | ".join(cells))

    text = _tidy("\n".join(parts))
    if not text:
        raise DocumentTextError(
            "这个 Word 文件里没有文字。如果内容其实在图片里，请改用纯文本上传。"
        )
    return text


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #


def extract_pdf(raw: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - 依赖没装
        raise DocumentTextError(
            "程序缺少读取 PDF 的组件。请改用纯文本上传："
            "把 PDF 里的内容复制出来，粘贴进记事本，另存为 .txt 再传。"
        ) from exc

    try:
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted:
            # 不少 PDF 只是「空密码」保护，先试试能不能直接开
            try:
                opened = reader.decrypt("")
            except Exception:
                opened = 0
            if not opened:
                raise DocumentTextError("这份 PDF 有密码，请先用 PDF 阅读器去掉密码再传。")
        pages = [page.extract_text() or "" for page in reader.pages]
    except DocumentTextError:
        raise
    except Exception as exc:
        raise DocumentTextError(
            "这个 PDF 读不出来，可能文件已损坏或不是标准 PDF。"
            "可以改用 Word 或纯文本上传。"
        ) from exc

    text = _tidy("\n".join(pages))
    if not text:
        raise DocumentTextError(
            "这份 PDF 里读不出文字——多半是扫描件或图片版。"
            "请改用 Word，或把文字复制到记事本另存为 .txt 再传。"
        )
    return text


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def extract_text(raw: bytes, filename: str) -> str | None:
    """二进制文档 → 抽出的正文；纯文本 → ``None``（交回调用方按编码解码）。

    后缀不认识时也返回 ``None``，让调用方的后缀白名单去拦。
    """
    ext = ext_of(filename)
    if ext == ".docx":
        return extract_docx(raw)
    if ext == ".pdf":
        return extract_pdf(raw)
    return None
