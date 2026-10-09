"""Word / PDF 简历抽正文。

用户手里的简历九成是 .docx 或 .pdf，所以这条路必须能走通，且**失败时说的话
得能照着做**（扫描件、老 .doc、加密 PDF 各有一条专属提示）。

这里自己造最小的 .docx / .pdf 样本，不往仓库里塞二进制测试文件。
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from boss_web.api.resume import MAX_BINARY_BYTES
from boss_web.app import create_app
from boss_web.services import document_text as dt


# --------------------------------------------------------------------------- #
# 样本构造
# --------------------------------------------------------------------------- #


def make_docx(*, with_table: bool = True, empty: bool = False) -> bytes:
    """造一份带表格的 .docx——简历最常见的排版就是表格。"""
    from docx import Document

    document = Document()
    if not empty:
        document.add_paragraph("张三")
        document.add_paragraph("13800138000 · zhangsan@example.com")
        if with_table:
            table = document.add_table(rows=2, cols=2)
            table.cell(0, 0).text = "工作经历"
            table.cell(0, 1).text = "示例科技 · 后端工程师"
            table.cell(1, 0).text = "技能"
            table.cell(1, 1).text = "Python、FastAPI"
        document.add_paragraph("自我评价：热爱工程化。")
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def make_pdf(text: str = "Zhang San", *, blank: bool = False) -> bytes:
    """手搓一份最小 PDF。pypdf 只会读不会写文字，所以自己拼对象表。"""
    content = b"" if blank else f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET".encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length "
        + str(len(content)).encode()
        + b" >>\nstream\n"
        + content
        + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    startxref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
        f"startxref\n{startxref}\n%%EOF\n"
    ).encode()
    return bytes(out)


def make_encrypted_pdf(password: str = "s3cret") -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.encrypt(password)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


@pytest.fixture
def client():
    return TestClient(create_app(), raise_server_exceptions=False)


# --------------------------------------------------------------------------- #
# 后缀判定
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name,expected",
    [
        ("a.docx", ".docx"),
        ("A.DOCX", ".docx"),
        ("我的简历.Pdf", ".pdf"),
        ("resume.md", ".md"),
        ("没有后缀", ""),
        (".hidden", ""),
        ("", ""),
    ],
)
def test_ext_of(name, expected):
    assert dt.ext_of(name) == expected


def test_allowed_ext_covers_word_and_pdf():
    assert {".docx", ".pdf"} <= dt.ALLOWED_EXT


def test_extract_text_defers_markdown_and_text_to_caller():
    """纯文本不走抽取——交回调用方按编码解码，行为与改造前一致。"""
    assert dt.extract_text("## 技能".encode(), "a.md") is None
    assert dt.extract_text("普通文本".encode("gbk"), "a.txt") is None
    assert dt.extract_text(b"xx", "a.exe") is None


# --------------------------------------------------------------------------- #
# Word
# --------------------------------------------------------------------------- #


def test_docx_extracts_paragraphs_and_tables():
    text = dt.extract_text(make_docx(), "简历.docx")

    assert "张三" in text
    assert "13800138000" in text
    # 表格内容也要抽出来，否则用表格排版的简历会变成空白
    assert "示例科技" in text
    assert "Python、FastAPI" in text
    assert "热爱工程化" in text


def test_docx_keeps_document_order():
    """段落和表格必须按原文顺序出现（先段落再表格会把经历和技能串位）。"""
    text = dt.extract_text(make_docx(), "简历.docx")
    assert text.index("张三") < text.index("工作经历") < text.index("热爱工程化")


def test_docx_empty_raises_friendly_error():
    with pytest.raises(dt.DocumentTextError) as ei:
        dt.extract_text(make_docx(empty=True), "空.docx")
    assert "没有文字" in str(ei.value)


def test_old_doc_format_is_told_to_convert():
    """`.doc` 是另一种二进制格式，python-docx 读不了——得教用户另存为。"""
    with pytest.raises(dt.DocumentTextError) as ei:
        dt.extract_text(b"\xd0\xcf\x11\xe0old binary", "老简历.docx")
    assert "另存为" in str(ei.value)
    assert ".docx" in str(ei.value)


def test_corrupt_docx_does_not_leak_exception_class():
    with pytest.raises(dt.DocumentTextError) as ei:
        dt.extract_text(b"not a zip at all", "坏.docx")
    message = str(ei.value)
    assert "PackageNotFoundError" not in message
    assert "BadZipFile" not in message


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #


def test_pdf_extracts_text():
    text = dt.extract_text(make_pdf("Zhang San Python"), "简历.pdf")
    assert "Zhang San" in text


def test_scanned_pdf_gets_scan_specific_hint():
    """扫描件是最常见的失败原因，提示必须点名「扫描件 / 图片」，不能只说读不出。"""
    with pytest.raises(dt.DocumentTextError) as ei:
        dt.extract_text(make_pdf(blank=True), "扫描件.pdf")
    message = str(ei.value)
    assert "扫描" in message
    assert ".txt" in message


def test_encrypted_pdf_tells_user_to_remove_password():
    with pytest.raises(dt.DocumentTextError) as ei:
        dt.extract_text(make_encrypted_pdf(), "加密.pdf")
    assert "密码" in str(ei.value)


def test_corrupt_pdf_does_not_leak_exception_class():
    with pytest.raises(dt.DocumentTextError) as ei:
        dt.extract_text(b"%PDF-1.4 garbage", "坏.pdf")
    assert "PdfReadError" not in str(ei.value)


def test_tidy_collapses_blank_runs():
    assert dt._tidy("a\n\n\n\n\nb") == "a\n\nb"
    assert dt._tidy("a  \nb") == "a\nb"


# --------------------------------------------------------------------------- #
# 上传接口
# --------------------------------------------------------------------------- #


def test_upload_docx(client):
    r = client.post(
        "/api/resume/upload",
        files={
            "file": (
                "我的简历.docx",
                make_docx(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert "张三" in body["raw"], "抽出来的正文要原样落库，解析接口读的就是它"
    assert "示例科技" in body["raw"]
    assert body["resume_id"].startswith("rs_")
    client.delete(f"/api/resume/item/{body['resume_id']}")


def test_upload_pdf(client):
    r = client.post(
        "/api/resume/upload",
        files={"file": ("简历.pdf", make_pdf("Li Si"), "application/pdf")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["resume_id"].startswith("rs_")
    client.delete(f"/api/resume/item/{body['resume_id']}")


def test_upload_broken_docx_returns_actionable_422(client):
    r = client.post(
        "/api/resume/upload",
        files={"file": ("简历.docx", b"garbage", "application/octet-stream")},
    )
    assert r.status_code == 422
    assert "另存为" in r.json()["message"]


def test_reject_message_mentions_word_and_pdf(client):
    """别再只写「只支持 Markdown」——用户手里根本没有 Markdown。"""
    r = client.post(
        "/api/resume/upload",
        files={"file": ("resume.doc", b"xx", "application/msword")},
    )
    assert r.status_code == 422
    message = r.json()["message"]
    assert "Word" in message and "PDF" in message


def test_binary_resumes_get_a_larger_size_ceiling(client, monkeypatch):
    """带照片的 Word/PDF 很容易超 2MB，纯文本的 2MB 限制不该套在它们头上。"""
    assert MAX_BINARY_BYTES > 2 * 1024 * 1024

    r = client.post(
        "/api/resume/upload",
        files={"file": ("大简历.docx", b"x" * (3 * 1024 * 1024), "application/octet-stream")},
    )
    # 3MB 的 Word 通过体积检查，倒在「读不出来」上——证明没被 2MB 卡死
    assert r.status_code == 422
    assert "超过" not in r.json()["message"]


def test_text_resume_still_capped_at_2mb(client):
    r = client.post(
        "/api/resume/upload",
        files={"file": ("big.txt", b"x" * (3 * 1024 * 1024), "text/plain")},
    )
    assert r.status_code == 422
    assert "超过" in r.json()["message"]
