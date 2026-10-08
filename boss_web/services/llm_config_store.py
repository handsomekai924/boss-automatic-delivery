"""LLM 配置存取：``data/llm_config.json``（gitignore），API 回显时打码。

可配置的只有 **api_key / base_url / model** 三项。
温度、max_tokens、超时是系统固定参数（见 :mod:`boss_web.config`），
读写都会被强制成常量，界面和接口都改不动。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from .. import config as C

logger = logging.getLogger(__name__)


@dataclass
class LLMConfig:
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    model: str = ""
    # 下面三项是系统固定值，构造时会被 normalize() 拉齐到常量
    temperature: float = C.LLM_TEMPERATURE
    max_tokens: int = C.LLM_MAX_TOKENS
    timeout: float = C.LLM_TIMEOUT

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    def normalize(self) -> "LLMConfig":
        """把采样参数拉回系统常量（历史配置文件里可能还留着旧值）。"""
        return LLMConfig(
            api_key=self.api_key,
            base_url=self.base_url,
            model=self.model,
            temperature=C.LLM_TEMPERATURE,
            max_tokens=C.LLM_MAX_TOKENS,
            timeout=C.LLM_TIMEOUT,
        )

    def to_dict(self) -> dict[str, Any]:
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
            "timeout": C.LLM_TIMEOUT,
        }
        return data


def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:3]}***{key[-4:]}"


def load_config(path: Path | str | None = None) -> LLMConfig:
    p = Path(path or C.LLM_CONFIG_PATH)
    if not p.exists():
        return LLMConfig()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("LLM 配置读不出来，按空配置处理：%s", exc)
        return LLMConfig()
    if not isinstance(raw, dict):
        return LLMConfig()
    known = {f.name for f in fields(LLMConfig)}
    return LLMConfig(**{k: v for k, v in raw.items() if k in known}).normalize()


def save_config(cfg: LLMConfig, path: Path | str | None = None) -> Path:
    p = Path(path or C.LLM_CONFIG_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(
        json.dumps(cfg.normalize().to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    try:
        os.chmod(tmp, 0o600)
    except OSError:  # pragma: no cover - Windows
        pass
    os.replace(tmp, p)
    logger.info("LLM 配置已保存到 %s", p)
    return p


#: 用户可改的字段；其余一律忽略
_UPDATABLE = ("api_key", "base_url", "model")


def update_config(payload: dict[str, Any], path: Path | str | None = None) -> LLMConfig:
    """合并更新可配置项：api_key 留空则保留旧值。

    ``temperature`` / ``max_tokens`` / ``timeout`` 即使传进来也会被丢掉，
    采样参数由系统常量说了算。
    """
    current = load_config(path)
    data = current.to_dict()
    for key in _UPDATABLE:
        if key in payload and payload[key] is not None:
            value = str(payload[key]).strip()
            if key == "api_key" and not value:
                continue  # 空 key = 不改
            data[key] = value
    cfg = LLMConfig(**data).normalize()
    save_config(cfg, path)
    return cfg
