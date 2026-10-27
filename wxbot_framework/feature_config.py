"""新增后台功能的配置校验；不开启时不创建线程或请求模型。"""
import math

from .messaging.text import validate_target
from .plugin_engine.manifest import announcement_text


def feature_options(config):
    if not isinstance(config.get("memory", {}), dict) or not isinstance(config.get("hourly_chat", {}), dict):
        raise ValueError("memory 和 hourly_chat 应为JSON对象")
    memory = {"enabled": False, "batch_size": 50, "flush_seconds": 600,
              "persona_refresh_hours": 24, "persona_days": 7,
              "seed_file": ".runtime/group_knowledge_seed.json", **config.get("memory", {})}
    hourly = {
        "enabled": False,
        "start_hour": 9,
        "end_hour": 23,
        "evening_start_hour": 18,
        "active_min_minutes": 120,
        "active_max_minutes": 180,
        "evening_min_minutes": 180,
        "evening_max_minutes": 300,
        "activity_window_minutes": 180,
        "daily_limit": 6,
        "cooldown_minutes": 45,
        "stale_candidate_minutes": 15,
        "min_messages": 2,
        "min_speakers": 2,
        "context_limit": 200,
        "quiet_minutes": 0,
        **config.get("hourly_chat", {}),
    }
    for key, settings in (("memory", memory), ("hourly_chat", hourly)):
        if type(settings["enabled"]) is not bool:
            raise ValueError(key + ".enabled 必须是布尔值")
    numeric = (
        (memory, "batch_size", 1, 200),
        (memory, "persona_refresh_hours", 0, 720),
        (memory, "persona_days", 1, 60),
        (hourly, "start_hour", 0, 23),
        (hourly, "end_hour", 1, 24),
        (hourly, "evening_start_hour", 0, 24),
        (hourly, "active_min_minutes", 15, 1440),
        (hourly, "active_max_minutes", 15, 1440),
        (hourly, "evening_min_minutes", 15, 1440),
        (hourly, "evening_max_minutes", 15, 1440),
        (hourly, "activity_window_minutes", 1, 1440),
        (hourly, "daily_limit", 1, 24),
        (hourly, "cooldown_minutes", 1, 1440),
        (hourly, "stale_candidate_minutes", 1, 1440),
        (hourly, "min_messages", 1, 100),
        (hourly, "min_speakers", 1, 100),
        (hourly, "context_limit", 1, 5000),
        (hourly, "quiet_minutes", 0, 1440),
    )
    for settings, key, lower, upper in numeric:
        if type(settings[key]) is not int or not lower <= settings[key] <= upper:
            raise ValueError(f"{key} 应为{lower}–{upper}的整数")
    if hourly["start_hour"] >= hourly["end_hour"]:
        raise ValueError("主动发言 start_hour 必须早于 end_hour")
    # evening_start_hour 允许落在窗口外：窄窗口可能整段都在傍晚或白天，
    # 此时窗口内的所有时刻自然归入同一档，不再因默认值 18 拒绝启动。
    for prefix in ("active", "evening"):
        if hourly[f"{prefix}_min_minutes"] > hourly[f"{prefix}_max_minutes"]:
            raise ValueError(f"{prefix}_min_minutes 不能大于 {prefix}_max_minutes")
    if hourly["min_speakers"] > hourly["min_messages"]:
        raise ValueError("min_speakers 不能大于 min_messages")
    seconds = memory["flush_seconds"]
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 1:
        raise ValueError("memory.flush_seconds 应为至少1秒的有限数值")
    if not isinstance(memory["seed_file"], str) or not memory["seed_file"].strip() or "\0" in memory["seed_file"]:
        raise ValueError("memory.seed_file 应为本地JSON路径")
    hourly = {"enabled": config.get("hourly_chat", {}).get("enabled", False),
              **{key: value for key, value in hourly.items() if key != "enabled"}}
    return memory, hourly


def maintenance_options(config, *, contributors=None):
    """维护入口的纯选项校验：只读配置，不建库、不启线程、不请求模型。"""
    options = config.get("maintenance", {})
    if not isinstance(options, dict):
        raise ValueError("maintenance 应为 JSON 对象")
    options = {"default_room": None, "upgrade_file": None, "announcement": None,
               "announcements": [], **options}
    if options["default_room"] is not None:
        validate_target(options["default_room"])
        if not options["default_room"].endswith("@chatroom"):
            raise ValueError("维护默认目标必须是群 ID")
    path = options["upgrade_file"]
    if path is not None and (not isinstance(path, str) or not path.strip() or "\0" in path):
        raise ValueError("upgrade_file 应为本地文件路径或 null")
    legacy = options["announcement"]
    announcements = options["announcements"]
    if not isinstance(announcements, list):
        raise ValueError("maintenance.announcements 应为公告数组")
    if legacy is not None and announcements:
        raise ValueError("maintenance.announcement 与 announcements 不能同时配置")
    if legacy is not None:
        announcements = [legacy]
    seen = set()
    normalized = []
    for notice in announcements:
        if not isinstance(notice, dict):
            raise ValueError("每份公告应为 JSON 对象")
        notice_id, room = notice.get("id"), notice.get("room")
        text = notice.get("text")
        plugins = notice.get("plugins", [])
        if (not isinstance(notice_id, str) or not notice_id.strip() or len(notice_id) > 80 or
                not isinstance(room, str)):
            raise ValueError("公告需要 id、room")
        validate_target(room)
        if not room.endswith("@chatroom") or notice_id in seen:
            raise ValueError("公告目标群或 ID 不正确/重复")
        if text is not None and (not isinstance(text, str) or "\0" in text):
            raise ValueError("公告 text 应为字符串或 null")
        if not isinstance(plugins, list) or not all(isinstance(name, str) and name for name in plugins):
            raise ValueError("公告 plugins 应为插件名数组")
        if len(set(plugins)) != len(plugins):
            raise ValueError("公告 plugins 不能重复")
        text_bytes = len((text or "").encode("utf-8"))
        if text_bytes > 900:
            raise ValueError("公告正文不能超过 900 个 UTF-8 字节")
        seen.add(notice_id)
        normalized.append({"id": notice_id, "room": room, "plugins": list(plugins), "text": text})
    options["announcement"] = legacy
    options["announcements"] = normalized
    for notice in normalized:
        if len(announcement_text(notice, contributors).encode("utf-8")) > 900:
            raise ValueError(f"公告 {notice['id']} 含插件贡献后超过 900 个 UTF-8 字节")
    return options
