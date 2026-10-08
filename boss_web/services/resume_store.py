"""简历文件与分析结果的落盘。

简历原文存 ``data/resumes/{id}.md``，解析结果存 ``{id}.json``；
匹配分析存 ``data/analyses/{id}.json``（带职位快照，删库也能回看）。
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .. import config as C

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
# 存储
# --------------------------------------------------------------------------- #


def _resume_path(resume_id: str) -> Path:
    return C.RESUMES_DIR / f"{resume_id}.md"


def _meta_path(resume_id: str) -> Path:
    return C.RESUMES_DIR / f"{resume_id}.json"


def save_resume(text: str, *, filename: str = "resume.md") -> ResumeDraft:
    C.ensure_data_dirs()
    resume_id = "rs_" + uuid.uuid4().hex[:10]
    safe = re.sub(r"[^\w.\-]+", "_", filename or "resume.md")[:60] or "resume.md"
    created = time.time()
    path = _resume_path(resume_id)
    path.write_text(text, encoding="utf-8")
    draft = parse_resume(
        text,
        resume_id=resume_id,
        title=safe,
        source_path=str(path),
        created_at=created,
    )
    _meta_path(resume_id).write_text(
        json.dumps(draft.to_dict(include_raw=False), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return draft


def load_resume(resume_id: str) -> ResumeDraft:
    path = _resume_path(resume_id)
    if not path.exists():
        raise FileNotFoundError(resume_id)
    raw = path.read_text(encoding="utf-8")
    meta_path = _meta_path(resume_id)
    meta: dict[str, Any] = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except ValueError:
            meta = {}
    draft = parse_resume(
        raw,
        resume_id=resume_id,
        title=str(meta.get("title") or resume_id),
        source_path=str(path),
        created_at=float(meta.get("created_at") or path.stat().st_mtime),
    )
    return draft


def list_resumes() -> list[dict[str, Any]]:
    C.ensure_data_dirs()
    items: list[dict[str, Any]] = []
    for meta_file in sorted(C.RESUMES_DIR.glob("rs_*.json"), reverse=True):
        try:
            data = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        data.pop("raw", None)
        items.append(data)
    return items


def delete_resume(resume_id: str) -> bool:
    existed = _resume_path(resume_id).exists() or _meta_path(resume_id).exists()
    for p in (_resume_path(resume_id), _meta_path(resume_id)):
        try:
            p.unlink()
        except OSError:
            pass
    return existed


def save_analysis(payload: dict[str, Any]) -> str:
    C.ensure_data_dirs()
    analysis_id = str(payload.get("analysis_id") or ("an_" + uuid.uuid4().hex[:10]))
    payload["analysis_id"] = analysis_id
    path = C.ANALYSES_DIR / f"{analysis_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return analysis_id


def load_analysis(analysis_id: str) -> dict[str, Any]:
    path = C.ANALYSES_DIR / f"{analysis_id}.json"
    if not path.exists():
        raise FileNotFoundError(analysis_id)
    return json.loads(path.read_text(encoding="utf-8"))


def list_analyses() -> list[dict[str, Any]]:
    C.ensure_data_dirs()
    items: list[dict[str, Any]] = []
    for path in sorted(C.ANALYSES_DIR.glob("an_*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        items.append(
            {
                "analysis_id": data.get("analysis_id", path.stem),
                "resume_id": data.get("resume_id"),
                "resume_title": data.get("resume_title"),
                "created_at": data.get("created_at"),
                "job_count": len(data.get("matches") or []),
                "top_score": _top_score(data),
                "status": data.get("status", "done"),
            }
        )
    return items


def _top_score(data: dict[str, Any]) -> float:
    scores = [m.get("match_score") or 0 for m in (data.get("matches") or [])]
    return max(scores) if scores else 0.0


def delete_analysis(analysis_id: str) -> bool:
    path = C.ANALYSES_DIR / f"{analysis_id}.json"
    try:
        path.unlink()
        return True
    except OSError:
        return False
