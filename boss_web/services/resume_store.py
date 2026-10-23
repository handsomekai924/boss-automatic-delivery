"""简历原文与分析结果的落库。

原文存 `resume` 行的 `content_md`，解析结果进 `meta`；匹配分析存 `analysis` 行。
库是 `data/boss.db`——跟登录态 / 搜索条件 / 职位同一个。

Attributes:
    SECTION_ALIASES: 章节标题别名 → 规范名。
    DELIVER_SENDING: 发送状态：进行中。
    DELIVER_OK: 发送状态：成功。
    DELIVER_FAILED: 发送状态：失败。
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

from ..runtime import app_data_dir

logger = logging.getLogger(__name__)


REVISION_EXPORT_DIR_NAME = "resume_revisions"


def revisions_export_dir() -> Path:
    """简历优化导出根目录（`app_data_dir()/exports/resume_revisions`），自动创建。

    Returns:
        可写的导出目录。
    """
    folder = app_data_dir() / "exports" / REVISION_EXPORT_DIR_NAME
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _safe_filename(raw: str) -> str:
    """把标题收成安全的文件名字段（与 `_source_name` 同规避字符）。"""
    return re.sub(r"[^\w.\-]+", "_", str(raw or "").strip())[:60] or "resume"


def revision_path(title: str, suffix: str, *, stamp: str) -> Path:
    """排一个不重名的导出文件路径。

    Args:
        title: 底稿标题（用于文件名字段）。
        suffix: 扩展名（`.md` / `.txt`）。
        stamp: 时间戳串（`%Y%m%d-%H%M`）。

    Returns:
        唯一存在的目标路径，重名自动追加 `-2`。
    """
    base = revisions_export_dir() / f"{_safe_filename(title)}-定向-{stamp}"
    path = base.with_suffix(suffix)
    n = 2
    while path.exists():
        path = base.with_name(f"{base.name}-{n}{suffix}")
        n += 1
    return path


@dataclass
class ResumeDraft:
    """规则解析出的简历骨架。

    Attributes:
        resume_id: 简历 id。
        title: 标题。
        source_path: 来源路径。
        created_at: 创建时间戳。
        raw: 简历原文。
        sections: 规范章节 → 正文。
        other_sections: 未归类章节 → 正文。
        skills: 技能标签。
        basic: 基本信息（姓名 / 电话 / 邮箱）。
        summary: 摘要。
    """

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
        """转字典；默认不带原文。"""
        data = asdict(self)
        if not include_raw:
            data.pop("raw", None)
        return data


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
    """标题去空白与装饰符，方便别名比对。"""
    return re.sub(r"[\s:：*#]+", "", text or "").strip()


def parse_markdown(text: str) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """按 Markdown 标题切章节。

    Returns:
        `(sections, other_sections, parse_notes)`。
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
    """从「技能标签」章节抽出技能列表。"""
    raw = sections.get("技能标签", "")
    skills: list[str] = []
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
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
    """从章节正文里抠姓名 / 电话 / 邮箱。"""
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
    """规则切章节，组装简历骨架。"""
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


def _source_name(filename: str) -> str:
    """把上传文件名收成安全的库内标题。"""
    return re.sub(r"[^\w.\-]+", "_", filename or "resume.md")[:60] or "resume.md"


def _draft_from_row(row: Any) -> ResumeDraft:
    """`resume` 行 → `ResumeDraft`（章节等嵌套字段在 `meta` 里）。"""
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
    """只存原文（`content_md` + 文件名）。

    LLM 解析单独跑、单独落 `meta.llm`；规则切章节只在读原文时轻量分段。
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
    """读一份简历骨架；不存在抛 `FileNotFoundError`。"""
    conn = boss_db.acquire()
    row = conn.execute(
        "SELECT * FROM resume WHERE resume_id = ?", (resume_id,)
    ).fetchone()
    if row is None:
        raise FileNotFoundError(resume_id)
    return _draft_from_row(row)


def list_resumes() -> list[dict[str, Any]]:
    """列出简历摘要；LLM 结果只报「解析过没有」。"""
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
    """删除简历；删到了返回 True。"""
    conn = boss_db.acquire()
    with conn:
        cur = conn.execute("DELETE FROM resume WHERE resume_id = ?", (resume_id,))
    return cur.rowcount > 0


def save_llm_parse(resume_id: str, llm_payload: dict[str, Any]) -> dict[str, Any]:
    """把 LLM 解析结果写进 `meta.llm`，回写后的 `llm` 整包。

    只覆盖 `meta.llm`，其余 meta 键原样保留。
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
    """读 `meta.llm`；没有解析过 → `None`。简历不存在 → `FileNotFoundError`。"""
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


def _top_score(matches: Any) -> float:
    """matches 里的最高分（没有 / 全不是数字 → 0），跟 analysis 行的 `top_score` 列同口径。"""
    scores = [m.get("match_score") or 0 for m in (matches or []) if isinstance(m, dict)]
    scores = [s for s in scores if isinstance(s, (int, float))]
    return float(max(scores) if scores else 0.0)


def save_analysis(payload: dict[str, Any]) -> str:
    """新插一条分析结果；返回 analysis_id。"""
    analysis_id = str(payload.get("analysis_id") or ("an_" + uuid.uuid4().hex[:10]))
    payload["analysis_id"] = analysis_id
    matches = payload.get("matches") or []
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
                _top_score(matches),
                json.dumps(payload, ensure_ascii=False),
            ),
        )
    return analysis_id


def load_analysis(analysis_id: str) -> dict[str, Any]:
    """读整包分析 payload；不存在抛 `FileNotFoundError`。"""
    conn = boss_db.acquire()
    row = conn.execute(
        "SELECT payload FROM analysis WHERE analysis_id = ?", (analysis_id,)
    ).fetchone()
    if row is None:
        raise FileNotFoundError(analysis_id)
    return json.loads(str(row["payload"]))


def _patch_match(analysis_id: str, encrypt_job_id: str, **fields: Any) -> dict[str, Any]:
    """改 payload 里某一条 match 的字段，回改后的那条。

    找不到 analysis → `FileNotFoundError`；找不到那条 match → `KeyError`。
    只 UPDATE 已有行，不新插；`top_score` 列跟着 matches 重新收敛。
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
            "UPDATE analysis SET payload = ?, top_score = ? WHERE analysis_id = ?",
            (json.dumps(payload, ensure_ascii=False), _top_score(matches), analysis_id),
        )
    return next(
        m for m in matches if isinstance(m, dict) and m.get("encrypt_job_id") == encrypt_job_id
    )


def update_match_result(analysis_id: str, encrypt_job_id: str, **fields: Any) -> dict[str, Any]:
    """单条重跑匹配后回写那条 match——只改不增。

    只覆盖传进来的分析字段；职位快照与发送状态等原有数据原样复用。
    """
    return _patch_match(analysis_id, encrypt_job_id, **fields)


def update_greeting(analysis_id: str, encrypt_job_id: str, greeting: str) -> dict[str, Any]:
    """只改 payload 里某一条 match 的招呼语，回改后的那条。"""
    return _patch_match(analysis_id, encrypt_job_id, greeting=greeting)


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

    `delivered_at` 只在成功时给；失败就留空串状态，行内显示原因。
    """
    fields: dict[str, Any] = {
        "deliver_status": deliver_status,
        "deliver_error": deliver_error,
    }
    if delivered_at is not None:
        fields["delivered_at"] = delivered_at
    return _patch_match(analysis_id, encrypt_job_id, **fields)


def list_analyses() -> list[dict[str, Any]]:
    """列出分析摘要（不含整包 payload）。"""
    conn = boss_db.acquire()
    rows = conn.execute(
        "SELECT analysis_id, resume_id, resume_title, created_at, job_count, "
        "top_score, status, model FROM analysis ORDER BY created_at DESC"
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
            "model": str(row["model"] or ""),
        }
        for row in rows
    ]


def delete_analysis(analysis_id: str) -> bool:
    """删除分析；删到了返回 True。"""
    conn = boss_db.acquire()
    with conn:
        cur = conn.execute("DELETE FROM analysis WHERE analysis_id = ?", (analysis_id,))
    return cur.rowcount > 0


REVISION_RUNNING = "running"
REVISION_DRAFT = "draft"
REVISION_SAVED = "saved"
REVISION_DISCARDED = "discarded"
REVISION_ERROR = "error"
REVISION_CANCELLED = "cancelled"

_REVISION_COLUMNS = (
    "revision_id", "resume_id", "resume_title", "created_at", "updated_at",
    "status", "target_kind", "target_json", "crawl_task_id", "job_count",
    "original_md", "revised_md", "change_json", "model", "export_format",
    "saved_path", "saved_resume_id", "error",
)


def _revision_dict(row: Any, *, full: bool = False) -> dict[str, Any]:
    """`resume_revisions` 行 → dict；`full=True` 时带原文 / 修改稿全文。"""
    def _safe_get(key: str, default: Any = "") -> Any:
        """安全访问 Row 列，列不存在时返回默认值。"""
        try:
            return row[key] if row[key] is not None else default
        except (KeyError, IndexError):
            return default

    data = {
        "revision_id": str(row["revision_id"]),
        "resume_id": str(row["resume_id"]),
        "resume_title": str(row["resume_title"] or ""),
        "created_at": float(row["created_at"] or 0.0),
        "updated_at": float(row["updated_at"] or 0.0),
        "status": str(row["status"] or ""),
        "mode": str(_safe_get("mode", "targeted")),
        "target_kind": str(row["target_kind"] or "jobs"),
        "crawl_task_id": str(row["crawl_task_id"] or ""),
        "job_count": int(row["job_count"] or 0),
        "model": str(row["model"] or ""),
        "export_format": str(row["export_format"] or "md"),
        "saved_path": str(row["saved_path"] or ""),
        "saved_resume_id": str(row["saved_resume_id"] or ""),
        "error": str(row["error"] or ""),
    }
    if full:
        data.update(
            {
                "original_md": str(row["original_md"] or ""),
                "revised_md": str(row["revised_md"] or ""),
                "changes": _load_change_json(str(row["change_json"] or "")),
                "target": _load_target_json(str(row["target_json"] or "")),
                "selected_sections": _load_selected_sections(str(_safe_get("selected_sections", "[]"))),
                "optimization_prompt": str(_safe_get("optimization_prompt", "")),
            }
        )
    return data


def _load_change_json(raw: str) -> list[dict[str, Any]]:
    """`change_json` 文本 → 列表；坏 JSON 回空列表。"""
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _load_target_json(raw: str) -> dict[str, Any]:
    """`target_json` 文本 → dict；坏 JSON 回空 dict。"""
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _load_selected_sections(raw: str) -> list[str]:
    """`selected_sections` 文本 → 列表；坏 JSON 回空列表。"""
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def create_revision(
    *,
    revision_id: str,
    resume_id: str,
    resume_title: str,
    target_kind: str,
    target_json: dict[str, Any],
    crawl_task_id: str,
    job_count: int,
    original_md: str,
    mode: str = "targeted",
    selected_sections: list[str] | None = None,
    optimization_prompt: str = "",
) -> str:
    """新插一条简历优化草稿（status = running），返回 revision_id。

    `original_md` 在生成时刻落死——底稿简历随后被删，对比与追溯仍成立。

    Args:
        mode: 优化模式，'targeted' 或 'free'。
        selected_sections: 自由模式选择的章节列表（targeted 模式可为空）。
        optimization_prompt: 自由模式的优化提示词（targeted 模式可为空）。
    """
    now = time.time()
    conn = boss_db.acquire()
    with conn:
        conn.execute(
            "INSERT INTO resume_revisions (revision_id, resume_id, resume_title, "
            "created_at, updated_at, status, mode, target_kind, target_json, crawl_task_id, "
            "job_count, original_md, selected_sections, optimization_prompt) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                revision_id,
                resume_id,
                resume_title or "",
                now,
                now,
                REVISION_RUNNING,
                mode or "targeted",
                target_kind or "jobs",
                json.dumps(target_json or {}, ensure_ascii=False),
                crawl_task_id or "",
                int(job_count or 0),
                original_md or "",
                json.dumps(selected_sections or [], ensure_ascii=False),
                optimization_prompt or "",
            ),
        )
    return revision_id


def load_revision(revision_id: str) -> dict[str, Any]:
    """读一条简历优化（全文）；不存在抛 `FileNotFoundError`。"""
    conn = boss_db.acquire()
    row = conn.execute(
        "SELECT * FROM resume_revisions WHERE revision_id = ?", (revision_id,)
    ).fetchone()
    if row is None:
        raise FileNotFoundError(revision_id)
    return _revision_dict(row, full=True)


def list_revisions(*, limit: int, offset: int) -> tuple[list[dict[str, Any]], int]:
    """简历优化记录列表（摘要）；返回 `(items, total)`，最近的在前。"""
    conn = boss_db.acquire()
    total_row = conn.execute("SELECT COUNT(*) AS n FROM resume_revisions").fetchone()
    total = int(total_row["n"] or 0)
    rows = conn.execute(
        "SELECT * FROM resume_revisions ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    return [_revision_dict(row) for row in rows], total


def update_revision_result(
    revision_id: str,
    *,
    revised_md: str,
    changes: list[dict[str, Any]],
    model: str,
    status: str = REVISION_DRAFT,
    error: str = "",
) -> bool:
    """把一次生成结果原地写回草稿行（只改不增）；返回是否真的更新到。"""
    conn = boss_db.acquire()
    with conn:
        cur = conn.execute(
            "UPDATE resume_revisions SET updated_at = ?, status = ?, revised_md = ?, "
            "change_json = ?, model = ?, error = ? WHERE revision_id = ?",
            (
                time.time(),
                status,
                revised_md or "",
                json.dumps(changes or [], ensure_ascii=False),
                model or "",
                error or "",
                revision_id,
            ),
        )
    return cur.rowcount > 0


def mark_revision_status(revision_id: str, status: str, *, error: str = "") -> bool:
    """只改状态（取消 / 丢弃 / 出错）；返回是否真的更新到。

    Args:
        revision_id: 修改记录 id。
        status: 目标状态。
        error: 可选错误文案。

    Returns:
        有没有真的更新到行。
    """
    conn = boss_db.acquire()
    with conn:
        cur = conn.execute(
            "UPDATE resume_revisions SET updated_at = ?, status = ?, error = ? "
            "WHERE revision_id = ?",
            (time.time(), status, error or "", revision_id),
        )
    return cur.rowcount > 0


def mark_revision_saved(
    revision_id: str,
    *,
    saved_path: str,
    saved_resume_id: str,
    export_format: str,
) -> bool:
    """确认保存成功后，把状态置 `saved` 并记写盘路径 / 新简历 id。"""
    conn = boss_db.acquire()
    with conn:
        cur = conn.execute(
            "UPDATE resume_revisions SET updated_at = ?, status = ?, saved_path = ?, "
            "saved_resume_id = ?, export_format = ? WHERE revision_id = ?",
            (
                time.time(),
                REVISION_SAVED,
                saved_path or "",
                saved_resume_id or "",
                export_format or "md",
                revision_id,
            ),
        )
    return cur.rowcount > 0


def delete_revision(revision_id: str) -> bool:
    """删一条修改记录（只删库内记录，不删已写盘文件）；返回是否删到。"""
    conn = boss_db.acquire()
    with conn:
        cur = conn.execute(
            "DELETE FROM resume_revisions WHERE revision_id = ?", (revision_id,)
        )
    return cur.rowcount > 0
