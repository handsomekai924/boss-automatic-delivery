"""招呼语生成约束，供完整匹配和单独重生成共用。

`GREETING_REQUIREMENT` 是出厂默认；用户可在「系统配置 → 招呼语」改写，
改写结果存在 `app_settings` 里。组装提示词时用 `resolve_greeting_requirement`
现读——自定义非空用自定义，空则回落本常量。
"""

from __future__ import annotations

from pathlib import Path

GREETING_REQUIREMENT = (
    "以求职者第一人称「我」写给招聘方 HR。"
    "全程站在求职者立场做自我推荐，突出「我」的优势和亮点。"
    "禁止以 HR/招聘方口吻说话——不得出现「我们公司」「我们团队」「我们正在招」「欢迎投递」「期待你的加入」等表述，也不要介绍公司或岗位。"
    "要求：100–150 字，自然、具体、不吹牛，不堆形容词和空话。不出现姓名等个人身份信息。"
    "用岗位相关的具体事实（经验、技能、成果、匹配点）说明我为什么合适，可量化就量化。"
    "表达对该岗位的真实兴趣和可沟通意愿，语气诚恳、简洁、有分寸。"
)


def resolve_greeting_requirement(path: Path | str | None = None) -> str:
    """取当前生效的招呼语约束：用户自定义优先，空则用出厂默认。

    Args:
        path: 状态库路径；省略 = `BOSS_DB` = `data/boss.db`。

    Returns:
        要插进提示词的约束文本。
    """
    from .app_settings_store import load_settings
    
    custom = load_settings(path).greeting_requirement.strip()
    return custom or GREETING_REQUIREMENT
