"""Blacklist rules, immutable evaluation snapshots, and audit persistence."""

from __future__ import annotations

import json
import logging
import re
import string
import time
import unicodedata
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

import boss_db

logger = logging.getLogger(__name__)

COMPANY = "company"
JD_KEYWORD = "jd_keyword"
COMPANY_MODES = frozenset({"exact", "regex"})
JD_MODES = frozenset({"contains", "regex"})
JD_SCOPES = frozenset({"job_name", "job_desc", "both"})


@dataclass(frozen=True)
class Rule:
    rule_id: str
    rule_type: str
    pattern: str
    match_mode: str
    scope: str = ""
    description: str = ""
    enabled: bool = True
    last_error: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["enabled"] = bool(self.enabled)
        return data


@dataclass(frozen=True)
class RuleMatch:
    rule: dict[str, Any]
    matched_text: str
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {**self.rule, "matched_text": self.matched_text, "source": self.source}


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def normalize_company(value: Any) -> str:
    """Normalize cosmetic company-name differences without broad deletion."""
    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    # 内部空白都是排版噪音：「甲 公司」和「甲公司」应视为同一家
    text = re.sub(r"\s+", "", text)
    # Common full-width punctuation has already been handled by NFKC. Treat
    # visually equivalent separators alike, but retain meaningful characters.
    text = re.sub(r"[·•・∙]", "·", text)
    text = re.sub(r"[\-_—–－]+", "-", text)
    text = re.sub(r"[（）()]", "(", text)
    text = text.replace("）", ")")
    return text


def validate_rule_payload(
    rule_type: str, pattern: str, match_mode: str, scope: str = ""
) -> None:
    if rule_type not in {COMPANY, JD_KEYWORD}:
        raise ValueError("rule_type 必须是 company 或 jd_keyword")
    if not str(pattern or "").strip():
        raise ValueError("规则内容不能为空")
    allowed = COMPANY_MODES if rule_type == COMPANY else JD_MODES
    if match_mode not in allowed:
        raise ValueError(f"{rule_type} 不支持匹配方式：{match_mode}")
    if rule_type == COMPANY and scope:
        raise ValueError("公司规则不支持匹配范围")
    if rule_type == JD_KEYWORD and scope not in JD_SCOPES:
        raise ValueError("JD 规则范围必须是 job_name、job_desc 或 both")
    if match_mode == "regex":
        try:
            re.compile(str(pattern))
        except re.error as exc:
            raise ValueError(f"正则表达式无效：{exc}") from None


def _rule_from_row(row: Any) -> Rule:
    return Rule(
        rule_id=str(row["rule_id"]),
        rule_type=str(row["rule_type"]),
        pattern=str(row["pattern"] or ""),
        match_mode=str(row["match_mode"] or ""),
        scope=str(row["scope"] or ""),
        description=str(row["description"] or ""),
        enabled=bool(row["enabled"]),
        last_error=str(row["last_error"] or ""),
        created_at=str(row["created_at"] or ""),
        updated_at=str(row["updated_at"] or ""),
    )


def list_rules(
    *, rule_type: str | None = None, enabled: bool | None = None, path: Path | str | None = None
) -> list[dict[str, Any]]:
    conn = boss_db.acquire(path)
    sql = "SELECT * FROM blacklist_rules"
    where: list[str] = []
    params: list[Any] = []
    if rule_type:
        where.append("rule_type = ?")
        params.append(rule_type)
    if enabled is not None:
        where.append("enabled = ?")
        params.append(1 if enabled else 0)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, rule_id"
    rows = conn.execute(sql, params).fetchall()
    items = []
    for row in rows:
        item = _rule_from_row(row).to_dict()
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM blacklist_hits WHERE rule_id = ?", (item["rule_id"],)
        ).fetchone()
        item["hit_count"] = int(count["n"] if count else 0)
        items.append(item)
    return items


def list_enabled_rules(*, path: Path | str | None = None) -> list[dict[str, Any]]:
    return list_rules(enabled=True, path=path)


def get_rule(rule_id: str, path: Path | str | None = None) -> dict[str, Any] | None:
    conn = boss_db.acquire(path)
    row = conn.execute("SELECT * FROM blacklist_rules WHERE rule_id = ?", (rule_id,)).fetchone()
    return _rule_from_row(row).to_dict() if row else None


def create_rule(payload: Mapping[str, Any], path: Path | str | None = None) -> dict[str, Any]:
    rule_type = str(payload.get("rule_type") or payload.get("type") or "")
    pattern = str(payload.get("pattern") or "").strip()
    mode = str(payload.get("match_mode") or payload.get("mode") or "")
    scope = str(payload.get("scope") or ("" if rule_type == COMPANY else "both"))
    enabled = bool(payload.get("enabled", True))
    if enabled:
        validate_rule_payload(rule_type, pattern, mode, scope)
    elif rule_type not in {COMPANY, JD_KEYWORD} or mode not in (COMPANY_MODES | JD_MODES):
        raise ValueError("停用规则的类型或匹配方式也必须有效")
    if rule_type == COMPANY and scope:
        raise ValueError("公司规则不支持匹配范围")
    if rule_type == JD_KEYWORD and scope not in JD_SCOPES:
        raise ValueError("JD 规则范围无效")
    rule_id = str(payload.get("rule_id") or "rl_" + uuid.uuid4().hex[:12])
    created = now_text()
    conn = boss_db.acquire(path)
    with conn:
        conn.execute(
            "INSERT INTO blacklist_rules "
            "(rule_id,rule_type,pattern,match_mode,scope,description,enabled,last_error,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (rule_id, rule_type, pattern, mode, scope, str(payload.get("description") or ""),
             1 if enabled else 0, "", created, created),
        )
    return get_rule(rule_id, path) or {}


def update_rule(rule_id: str, payload: Mapping[str, Any], path: Path | str | None = None) -> dict[str, Any]:
    old = get_rule(rule_id, path)
    if old is None:
        raise KeyError(rule_id)
    merged = {**old, **dict(payload), "rule_id": rule_id}
    # hit_count is response-only and must not enter validation/storage.
    rule_type = str(merged.get("rule_type") or merged.get("type") or "")
    pattern = str(merged.get("pattern") or "").strip()
    mode = str(merged.get("match_mode") or merged.get("mode") or "")
    scope = str(merged.get("scope") or ("" if rule_type == COMPANY else "both"))
    enabled = bool(merged.get("enabled", True))
    if enabled:
        validate_rule_payload(rule_type, pattern, mode, scope)
    elif rule_type not in {COMPANY, JD_KEYWORD} or mode not in (COMPANY_MODES | JD_MODES):
        raise ValueError("停用规则的类型或匹配方式也必须有效")
    if rule_type == COMPANY and scope:
        raise ValueError("公司规则不支持匹配范围")
    if rule_type == JD_KEYWORD and scope not in JD_SCOPES:
        raise ValueError("JD 规则范围无效")
    stamp = now_text()
    conn = boss_db.acquire(path)
    with conn:
        conn.execute(
            "UPDATE blacklist_rules SET rule_type=?,pattern=?,match_mode=?,scope=?,description=?,"
            "enabled=?,last_error=?,updated_at=? WHERE rule_id=?",
            (rule_type, pattern, mode, scope, str(merged.get("description") or ""),
             1 if enabled else 0, "", stamp, rule_id),
        )
    return get_rule(rule_id, path) or {}


def set_rule_enabled(rule_id: str, enabled: bool, path: Path | str | None = None) -> dict[str, Any]:
    rule = get_rule(rule_id, path)
    if rule is None:
        raise KeyError(rule_id)
    if enabled:
        validate_rule_payload(rule["rule_type"], rule["pattern"], rule["match_mode"], rule["scope"])
    conn = boss_db.acquire(path)
    with conn:
        conn.execute(
            "UPDATE blacklist_rules SET enabled=?,last_error=?,updated_at=? WHERE rule_id=?",
            (1 if enabled else 0, "", now_text(), rule_id),
        )
    return get_rule(rule_id, path) or {}


def delete_rule(rule_id: str, path: Path | str | None = None) -> bool:
    conn = boss_db.acquire(path)
    with conn:
        cur = conn.execute("DELETE FROM blacklist_rules WHERE rule_id = ?", (rule_id,))
    return cur.rowcount > 0


def _find(pattern: str, mode: str, text: str) -> str:
    if mode == "contains":
        return pattern if pattern in text else ""
    if mode == "exact":
        return text if normalize_company(pattern) == normalize_company(text) else ""
    match = re.search(pattern, text)
    return match.group(0) if match else ""


def _record_rule_error(rule_id: Any, error: Exception) -> None:
    """Persist a runtime regex failure without changing historical hit rows."""
    if not rule_id:
        return
    try:
        conn = boss_db.acquire()
        with conn:
            conn.execute(
                "UPDATE blacklist_rules SET last_error=?, updated_at=? WHERE rule_id=?",
                (str(error)[:500], now_text(), str(rule_id)),
            )
    except Exception:  # noqa: BLE001 - evaluator must never break a crawl
        logger.debug("无法记录黑名单规则运行错误", exc_info=True)


def evaluate_company(brand_name: str, rules: Iterable[Mapping[str, Any]]) -> list[RuleMatch]:
    normalized = normalize_company(brand_name)
    result: list[RuleMatch] = []
    # 公司名缺失无法比对：不算命中（自动投递由调用方按「状态不完整」保守拦截）
    if not normalized:
        return result
    for rule in rules:
        if not rule.get("enabled") or rule.get("rule_type") != COMPANY:
            continue
        try:
            # The configured pattern is the left-hand side.  Passing the
            # candidate as both arguments makes every exact rule match every
            # company and makes regex rules execute the company name itself.
            hit = _find(
                str(rule.get("pattern") or ""),
                str(rule.get("match_mode") or ""),
                normalized,
            )
        except (re.error, TypeError, ValueError) as exc:
            logger.warning("公司黑名单规则 %s 执行失败：%s", rule.get("rule_id"), exc)
            _record_rule_error(rule.get("rule_id"), exc)
            continue
        if hit:
            result.append(RuleMatch(dict(rule), hit, "brand_name"))
    return result


def evaluate_jd(
    job_name: str, job_desc: str, rules: Iterable[Mapping[str, Any]]
) -> list[RuleMatch]:
    result: list[RuleMatch] = []
    for rule in rules:
        if not rule.get("enabled") or rule.get("rule_type") != JD_KEYWORD:
            continue
        scope = str(rule.get("scope") or "both")
        fields = [("job_name", job_name)] if scope == "job_name" else [("job_desc", job_desc)] if scope == "job_desc" else [("job_name", job_name), ("job_desc", job_desc)]
        try:
            for source, text in fields:
                hit = _find(str(rule.get("pattern") or ""), str(rule.get("match_mode") or ""), text or "")
                if hit:
                    result.append(RuleMatch(dict(rule), hit, source))
                    break
        except (re.error, TypeError, ValueError) as exc:
            logger.warning("JD 黑名单规则 %s 执行失败：%s", rule.get("rule_id"), exc)
            _record_rule_error(rule.get("rule_id"), exc)
    return result


def save_hit(
    *, task_id: str = "", job_id: str = "", job_name: str = "", brand_name: str = "",
    match: RuleMatch | None = None, action: str = "stored", jd_status: str = "", error: str = "",
    path: Path | str | None = None,
) -> str:
    rule = match.rule if match else {}
    log_id = "bh_" + uuid.uuid4().hex[:12]
    conn = boss_db.acquire(path)
    with conn:
        conn.execute(
            "INSERT INTO blacklist_hits (log_id,task_id,job_id,job_name,brand_name,rule_id,rule_type,"
            "pattern,match_mode,match_scope,matched_text,action,jd_status,error,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (log_id, task_id, job_id, job_name, brand_name, str(rule.get("rule_id") or ""),
             str(rule.get("rule_type") or ""), str(rule.get("pattern") or ""),
             str(rule.get("match_mode") or ""), match.source if match else str(rule.get("scope") or ""),
             match.matched_text if match else "", action, jd_status, error, now_text()),
        )
    return log_id


def list_hits(rule_id: str | None = None, *, limit: int = 100, path: Path | str | None = None) -> list[dict[str, Any]]:
    conn = boss_db.acquire(path)
    if rule_id:
        rows = conn.execute("SELECT * FROM blacklist_hits WHERE rule_id=? ORDER BY created_at DESC LIMIT ?", (rule_id, limit)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM blacklist_hits ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def snapshot_rules(rules: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(rule) for rule in rules]
