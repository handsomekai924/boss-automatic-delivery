"""简历原文与分析结果的落库。

简历原文存 ``resume`` 行的 ``content_md``，解析结果整包进 ``meta``；
匹配分析存 ``analysis`` 行（带职位快照，删库也能回看）。库是
``data/boss.db``——跟登录态 / 搜索条件 / 职位同一个。
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

import boss_db

logger = logging.getLogger(__name__)


@dataclass
class ResumeDraft:
    """规则解析出的简历骨架。"""

    resume_id: str
    title: str
    source_path: str
    created_at: float
    raw: str
    sections: dict[str, str] = field(default_factory=dict)
    other_sections: dict[str, str] = field(default_factory=dict)
    skills: list[str] = field(default_factory=list)
    basic: dict[str, str] = field(default_factory=dict)
    summary: str = ""

    def to_dict(self, *, include_raw: bool = False) -> dict[str, Any]:
        data = asdict(self)
        if not include_raw:
            data.pop("raw", None)
        return data


#: 章节标题别名 → 规范名
SECTION_ALIASES: dict[str, tuple[str, ...]] = {
    "基本信息": ("基本信息", "个人信息", "联系信息", "联系方式"),
    "求职意向": ("求职意向", "求职目标", "期望岗位", "求职期望"),
    "工作经历": ("工作经历", "工作经验", "职业经历", "工作履历"),
    "项目经历": ("项目经历", "项目经验", "项目"),
    "教育经历": ("教育经历", "教育背景", "教育"),
    "技能标签": ("技能标签", "技能清单", "专业技能", "技术栈", "技能"),
    "自我评价": ("自我评价", "个人总结", "自我介绍", "个人评价"),
}


def _norm_heading(text: str) -> str:
    return re.sub(r"[\s:：*#]+", "", text or "").strip()


def parse_markdown(text: str) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """按 Markdown 标题切章节。

    :return: ``(sections, other_sections, parse_notes)``
    """
    lines = (text or "").replace("\r\n", "\n").split("\n")
    buckets: list[tuple[str, list[str]]] = [("_body", [])]
    for line in lines:
        m = re.match(r"^(#{1,4})\s+(.+?)\s*$", line)
        if m:
            buckets.append((m.group(2).strip(), []))
        else:
            buckets[-1][1].append(line)

    sections: dict[str, str] = {}
    others: dict[str, str] = {}
    notes: list[str] = []

    for heading, body_lines in buckets:
        body = "\n".join(body_lines).strip()
        if heading == "_body":
            if body:
                others["（标题前的内容）"] = body
            continue
        norm = _norm_heading(heading)
        canonical = ""
        for name, aliases in SECTION_ALIASES.items():
            for alias in aliases:
                if norm == _norm_heading(alias) or norm.startswith(_norm_heading(alias)):
                    canonical = name
                    break
            if canonical:
                break
        if canonical:
            # 同名章节往后面追加，不覆盖
            if canonical in sections and body:
                sections[canonical] = sections[canonical] + "\n\n" + body
            else:
                sections.setdefault(canonical, body)
        else:
            others[heading] = body

    for name in SECTION_ALIASES:
        if name not in sections:
            notes.append(f"未识别到「{name}」章节")
    return sections, others, notes


_SKILL_SPLIT = re.compile(r"[、,，/|;；\n]+")


def extract_skills(sections: dict[str, str]) -> list[str]:
    raw = sections.get("技能标签", "")
    skills: list[str] = []
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        # 去掉列表符号
        line = re.sub(r"^[-*+]\s+", "", line)
        line = re.sub(r"^\d+[.、)]\s+", "", line)
        for token in _SKILL_SPLIT.split(line):
            token = token.strip().strip("`* ")
            token = re.sub(r"^[A-Za-z0-9]+[.、)]\s+", "", token)
            if 1 < len(token) <= 24 and token not in skills:
                skills.append(token)
    return skills[:40]


_PHONE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def extract_basic(sections: dict[str, str]) -> dict[str, str]:
    blob = "\n".join(sections.values())
    basic: dict[str, str] = {}
    phone = _PHONE.search(blob)
    if phone:
        basic["phone"] = phone.group(1)
    email = _EMAIL.search(blob)
    if email:
        basic["email"] = email.group(0)
    for line in (sections.get("基本信息") or "").split("\n"):
        m = re.match(r"^\s*[-*]?\s*(姓名|名字)\s*[:：]\s*(.+)$", line.strip())
        if m:
            basic["name"] = m.group(2).strip()
    return basic


def parse_resume(text: str, *, resume_id: str, title: str, source_path: str, created_at: float) -> ResumeDraft:
    sections, others, _notes = parse_markdown(text)
    return ResumeDraft(
        resume_id=resume_id,
        title=title,
        source_path=source_path,
        created_at=created_at,
        raw=text,
        sections=sections,
        other_sections=others,
        skills=extract_skills(sections),
        basic=extract_basic(sections),
        summary=(sections.get("自我评价") or sections.get("求职意向") or "")[:400],
    )


# --------------------------------------------------------------------------- #
# 存储（状态库 data/boss.db）
# --------------------------------------------------------------------------- #


def _source_name(filename: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", filename or "resume.md")[:60] or "resume.md"


def _draft_from_row(row: Any) -> ResumeDraft:
    """``resume`` 行 → :class:`ResumeDraft`（章节等嵌套字段在 ``meta`` 里）。"""
    resume_id = str(row["resume_id"])
    try:
        meta = json.loads(str(row["meta"] or "{}"))
    except ValueError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return parse_resume(
        str(row["content_md"] or ""),
        resume_id=resume_id,
        title=str(row["title"] or meta.get("title") or resume_id),
        source_path=str(meta.get("source_path") or f"db://{resume_id}"),
        created_at=float(row["created_at"] or 0.0),
    )


def save_resume(text: str, *, filename: str = "resume.md") -> ResumeDraft:
    """**只存原文**（``content_md`` + 文件名）。

    LLM 解析由 :mod:`boss_web.services.resume_parser` 单独跑、单独落
    ``meta.llm``（见 :func:`save_llm_parse`）；规则切章节退居幕后，只在读原文时
    轻量分段，不再当作解析结果写库。
    """
    resume_id = "rs_" + uuid.uuid4().hex[:10]
    safe = _source_name(filename)
    created = time.time()
    conn = boss_db.acquire()
    with conn:
        conn.execute(
            "INSERT INTO resume (resume_id, title, source_name, created_at, content_md, meta) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                resume_id,
                safe,
                safe,
                created,
                text,
                json.dumps(
                    {"title": safe, "source_name": safe, "created_at": created},
                    ensure_ascii=False,
                ),
            ),
        )
    return parse_resume(
        text,
        resume_id=resume_id,
        title=safe,
        source_path=f"db://{resume_id}",
        created_at=created,
    )


def load_resume(resume_id: str) -> ResumeDraft:
    conn = boss_db.acquire()
    row = conn.execute(
        "SELECT * FROM resume WHERE resume_id = ?", (resume_id,)
    ).fetchone()
    if row is None:
        raise FileNotFoundError(resume_id)
    return _draft_from_row(row)


def list_resumes() -> list[dict[str, Any]]:
    conn = boss_db.acquire()
    rows = conn.execute(
        "SELECT * FROM resume ORDER BY created_at DESC"
    ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        try:
            meta = json.loads(str(row["meta"] or "{}"))
        except ValueError:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        meta.pop("raw", None)
        # 列表只报「解析过没有」，别把整包 LLM 结果塞进列表响应
        llm = meta.get("llm")
        if isinstance(llm, dict):
            meta["llm"] = {
                "parsed_at": llm.get("parsed_at"),
                "model": llm.get("model"),
                "has_data": isinstance(llm.get("data"), dict) and bool(llm.get("data")),
            }
        meta.setdefault("resume_id", str(row["resume_id"]))
        meta.setdefault("title", str(row["title"] or ""))
        meta.setdefault("created_at", float(row["created_at"] or 0.0))
        items.append(meta)
    return items


def delete_resume(resume_id: str) -> bool:
    conn = boss_db.acquire()
    with conn:
        cur = conn.execute("DELETE FROM resume WHERE resume_id = ?", (resume_id,))
    return cur.rowcount > 0


# --------------------------------------------------------------------------- #
# LLM 固定模板解析结果（meta.llm）
# --------------------------------------------------------------------------- #


def save_llm_parse(resume_id: str, llm_payload: dict[str, Any]) -> dict[str, Any]:
    """把 LLM 解析结果写进 ``meta.llm``，回写后的 ``llm`` 整包。

    ``llm_payload`` 形状：``{"parsed_at", "model", "data"}``。
    **只覆盖 ``meta.llm``**，其余 meta 键（title/source_name/…）原样保留。
    """
    conn = boss_db.acquire()
    row = conn.execute(
        "SELECT meta FROM resume WHERE resume_id = ?", (resume_id,)
    ).fetchone()
    if row is None:
        raise FileNotFoundError(resume_id)
    try:
        meta = json.loads(str(row["meta"] or "{}"))
    except ValueError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    meta["llm"] = llm_payload
    with conn:
        conn.execute(
            "UPDATE resume SET meta = ? WHERE resume_id = ?",
            (json.dumps(meta, ensure_ascii=False), resume_id),
        )
    return llm_payload


def load_llm_parse(resume_id: str) -> dict[str, Any] | None:
    """读 ``meta.llm``；没有解析过 → ``None``。简历不存在 → ``FileNotFoundError``。"""
    conn = boss_db.acquire()
    row = conn.execute(
        "SELECT meta FROM resume WHERE resume_id = ?", (resume_id,)
    ).fetchone()
    if row is None:
        raise FileNotFoundError(resume_id)
    try:
        meta = json.loads(str(row["meta"] or "{}"))
    except ValueError:
        return None
    if not isinstance(meta, dict):
        return None
    llm = meta.get("llm")
    return llm if isinstance(llm, dict) else None


def save_analysis(payload: dict[str, Any]) -> str:
    analysis_id = str(payload.get("analysis_id") or ("an_" + uuid.uuid4().hex[:10]))
    payload["analysis_id"] = analysis_id
    matches = payload.get("matches") or []
    scores = [m.get("match_score") or 0 for m in matches if isinstance(m, dict)]
    scores = [s for s in scores if isinstance(s, (int, float))]
    llm = payload.get("llm") or {}
    conn = boss_db.acquire()
    with conn:
        conn.execute(
            "INSERT INTO analysis (analysis_id, resume_id, resume_title, created_at, "
            "status, model, base_url, job_count, top_score, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                analysis_id,
                str(payload.get("resume_id") or ""),
                str(payload.get("resume_title") or ""),
                float(payload.get("created_at") or time.time()),
                str(payload.get("status") or "done"),
                str(llm.get("model") or ""),
                str(llm.get("base_url") or ""),
                len(matches),
                float(max(scores) if scores else 0.0),
                json.dumps(payload, ensure_ascii=False),
            ),
        )
    return analysis_id


def load_analysis(analysis_id: str) -> dict[str, Any]:
    conn = boss_db.acquire()
    row = conn.execute(
        "SELECT payload FROM analysis WHERE analysis_id = ?", (analysis_id,)
    ).fetchone()
    if row is None:
        raise FileNotFoundError(analysis_id)
    return json.loads(str(row["payload"]))


def _patch_match(analysis_id: str, encrypt_job_id: str, **fields: Any) -> dict[str, Any]:
    """改 payload 里某一条 match 的字段，回改后的那条。

    找不到 analysis → ``FileNotFoundError``；找不到那条 match → ``KeyError``。
    招呼语与发送结果都走这里——一次读改写，别开两套。
    """
    payload = load_analysis(analysis_id)
    matches = payload.get("matches")
    if not isinstance(matches, list):
        matches = []
    for item in matches:
        if isinstance(item, dict) and item.get("encrypt_job_id") == encrypt_job_id:
            item.update(fields)
            break
    else:
        raise KeyError(encrypt_job_id)
    payload["matches"] = matches
    conn = boss_db.acquire()
    with conn:
        conn.execute(
            "UPDATE analysis SET payload = ? WHERE analysis_id = ?",
            (json.dumps(payload, ensure_ascii=False), analysis_id),
        )
    return next(
        m for m in matches if isinstance(m, dict) and m.get("encrypt_job_id") == encrypt_job_id
    )


def update_greeting(analysis_id: str, encrypt_job_id: str, greeting: str) -> dict[str, Any]:
    """只改 payload 里某一条 match 的招呼语，回改后的那条。"""
    return _patch_match(analysis_id, encrypt_job_id, greeting=greeting)


#: 发送状态（写进 match 的 ``deliver_status``）
DELIVER_SENDING = "sending"
DELIVER_OK = "ok"
DELIVER_FAILED = "failed"


def update_delivery(
    analysis_id: str,
    encrypt_job_id: str,
    *,
    deliver_status: str,
    delivered_at: float | None = None,
    deliver_error: str = "",
) -> dict[str, Any]:
    """把一次发送的结果写回某条 match，回改后的那条。

    ``delivered_at`` 只在**成功**时给（前端拿它判断「已发送、别重发」）；
    失败就留空串状态，行内显示原因。
    """
    fields: dict[str, Any] = {
        "deliver_status": deliver_status,
        "deliver_error": deliver_error,
    }
    if delivered_at is not None:
        fields["delivered_at"] = delivered_at
    return _patch_match(analysis_id, encrypt_job_id, **fields)


def list_analyses() -> list[dict[str, Any]]:
    conn = boss_db.acquire()
    rows = conn.execute(
        "SELECT analysis_id, resume_id, resume_title, created_at, job_count, "
        "top_score, status FROM analysis ORDER BY created_at DESC"
    ).fetchall()
    return [
        {
            "analysis_id": str(row["analysis_id"]),
            "resume_id": row["resume_id"],
            "resume_title": row["resume_title"],
            "created_at": row["created_at"],
            "job_count": int(row["job_count"] or 0),
            "top_score": float(row["top_score"] or 0.0),
            "status": row["status"],
        }
        for row in rows
    ]


def delete_analysis(analysis_id: str) -> bool:
    conn = boss_db.acquire()
    with conn:
        cur = conn.execute("DELETE FROM analysis WHERE analysis_id = ?", (analysis_id,))
    return cur.rowcount > 0
