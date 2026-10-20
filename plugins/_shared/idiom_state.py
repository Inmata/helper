"""接龙状态的只读解释：字段校验与投影，不含玩法与词库。

出题方（idiom_chain）与读取方（mention_chat）共用同一套字段解释，
避免同一份 payload 被两处按不同规则理解；各插件仍自行决定
空闲时限、投影条数等策略。
"""
from __future__ import annotations

from datetime import datetime, timezone

from wxbot_framework.messaging.text import FOUR_CHINESE


def current_and_used(state):
    """校验接龙进度，返回 ``(当前成语, 四字成语元组)``；结构不合法返回 None。"""
    if not isinstance(state, dict):
        return None
    current, used = state.get("current"), state.get("used")
    if not isinstance(current, str) or not FOUR_CHINESE.fullmatch(current):
        return None
    if not isinstance(used, list):
        return None
    words = tuple(word for word in used if isinstance(word, str) and FOUR_CHINESE.fullmatch(word))
    if current not in words:
        return None
    return current, words


def updated_age(state, *, now=None):
    """距上次更新的秒数；时间戳缺失或不可解析时返回 None。"""
    try:
        saved = datetime.fromisoformat(state.get("updated_at"))
    except (TypeError, ValueError, AttributeError):
        return None
    if saved.tzinfo is None:
        saved = saved.replace(tzinfo=timezone.utc)
    return ((now or datetime.now(timezone.utc)) - saved).total_seconds()


def is_move(text, current):
    """四字且首字等于当前接字，就是要交给接龙插件判定的答案。"""
    return bool(FOUR_CHINESE.fullmatch(text) and text[0] == current[-1])


def game_context(state, *, sample=10):
    """人格回复用的只读投影：当前成语、接字与最近用词。"""
    current = state["current"]
    used = [word for word in state.get("used", []) if isinstance(word, str)][-sample:]
    context = {"current": current, "next_char": current[-1], "used": used}
    for key in ("rounds", "goal", "hints_left", "swaps_left"):
        if type(state.get(key)) is int:
            context[key] = state[key]
    return context
