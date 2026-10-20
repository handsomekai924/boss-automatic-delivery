"""LLM 配置存取：状态库 `data/boss.db` 的 `doc('llm_config')`，API 回显时打码。

可配置的只有 api_key / base_url / model 三项；采样与超时参数是系统固定值，
读写都会被强制成常量，界面和接口都改不动。

Attributes:
    _UPDATABLE: 用户可改的字段名；其余一律忽略。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

import boss_db

from .. import config as C

logger = logging.getLogger(__name__)


@dataclass
class LLMConfig:
    """LLM 配置项。

    Attributes:
        api_key: API 密钥；空串 = 未配置。
        base_url: 上游兼容 OpenAI 的接口地址。
        model: 模型名。
        temperature: 采样温度，构造时被 `normalize()` 拉齐到系统常量。
        max_tokens: 单次回答上限，同上。
        timeout: 请求超时（秒），同上。
    """

    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    model: str = ""
    temperature: float = C.LLM_TEMPERATURE
    max_tokens: int = C.LLM_MAX_TOKENS
    timeout: float = C.LLM_TIMEOUT

    @property
    def configured(self) -> bool:
        """三项可配置字段是否都已填好。"""
        return bool(self.api_key and self.base_url and self.model)

    def normalize(self) -> "LLMConfig":
        """把采样参数拉回系统常量（历史配置里可能还留着旧值）。"""
        return LLMConfig(
            api_key=self.api_key,
            base_url=self.base_url,
            model=self.model,
            temperature=C.LLM_TEMPERATURE,
            max_tokens=C.LLM_MAX_TOKENS,
            timeout=C.LLM_TIMEOUT,
        )

    def to_dict(self) -> dict[str, Any]:
        """归一化后转字典。"""
        return asdict(self.normalize())

    def masked(self) -> dict[str, Any]:
        """给网页看的形态：api_key 只留尾 4 位，采样参数标成系统固定。"""
        data = self.to_dict()
        data["api_key"] = mask_key(self.api_key)
        data["has_key"] = bool(self.api_key)
        data["configured"] = self.configured
        data["fixed"] = {
            "temperature": C.LLM_TEMPERATURE,
            "max_tokens": C.LLM_MAX_TOKENS,
            "resume_parse_max_tokens": C.LLM_RESUME_PARSE_MAX_TOKENS,
            "match_max_tokens": C.LLM_MATCH_MAX_TOKENS,
            "timeout": C.LLM_TIMEOUT,
        }
        return data


def mask_key(key: str) -> str:
    """密钥打码：只留前 3 与尾 4；短串整串打星。"""
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:3]}***{key[-4:]}"


def load_config(path: Path | str | None = None) -> LLMConfig:
    """读状态库里的 LLM 配置；没有 / 坏了 → 空配置（不报错）。

    Args:
        path: 状态库路径；省略 = `BOSS_DB` = `data/boss.db`。
    """
    try:
        raw = boss_db.doc_get_raw(boss_db.DOC_LLM_CONFIG, path)
    except (OSError, sqlite3.Error) as exc:
        logger.warning("状态库打不开，LLM 配置按空配置处理：%s", exc)
        return LLMConfig()
    if raw is None:
        return LLMConfig()
    try:
        data = json.loads(raw)
    except ValueError as exc:
        logger.warning("LLM 配置读不出来，按空配置处理：%s", exc)
        return LLMConfig()
    if not isinstance(data, dict):
        return LLMConfig()
    known = {f.name for f in fields(LLMConfig)}
    return LLMConfig(**{k: v for k, v in data.items() if k in known}).normalize()


def save_config(cfg: LLMConfig, path: Path | str | None = None) -> Path:
    """写回状态库；返回库路径。

    Args:
        cfg: 要保存的配置。
        path: 状态库路径；省略 = `BOSS_DB` = `data/boss.db`。
    """
    resolved = boss_db.doc_set(boss_db.DOC_LLM_CONFIG, cfg.normalize().to_dict(), path)
    logger.info("LLM 配置已保存到 %s", resolved)
    return resolved


_UPDATABLE = ("api_key", "base_url", "model")


def update_config(payload: dict[str, Any], path: Path | str | None = None) -> LLMConfig:
    """合并更新可配置项：api_key 留空则保留旧值；系统固定字段一律忽略。"""
    current = load_config(path)
    data = current.to_dict()
    for key in _UPDATABLE:
        if key in payload and payload[key] is not None:
            value = str(payload[key]).strip()
            if key == "api_key" and not value:
                continue
            data[key] = value
    cfg = LLMConfig(**data).normalize()
    save_config(cfg, path)
    return cfg
