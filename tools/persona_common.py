"""人格补充工具的公共部分：只读打开成员库，不建表、不写库、不回显密钥。"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wxbot_framework.conversation.members import MemberStore  # noqa: E402


def readonly_members(config_path):
    """按 bot_config 构造成员库，并把唯一连接换成 URI mode=ro 的只读连接。"""
    config_path = Path(config_path).resolve()
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    members = MemberStore.from_config(config, config_path.parent)
    if members is None:
        raise SystemExit("bot_config 未配置 members_config_file")
    if not members.path.exists():
        raise SystemExit("数据库不存在")
    db = sqlite3.connect(members.path.as_uri() + "?mode=ro", uri=True, check_same_thread=False)
    db.row_factory = sqlite3.Row
    # 跳过 prepare（建表与导入档案都会写库）；之后所有读取都走这条只读连接。
    members.database._db = db
    members._prepared = True
    return members, config


def pick_rooms(members, room):
    """返回 [(序号标签, 群ID)]；输出只用序号标签，不打印群 ID。"""
    rooms = sorted(members.groups)
    if room:
        if room not in rooms:
            raise SystemExit("该群不在配置中")
        rooms = [room]
    return [(f"群{index}", value) for index, value in enumerate(rooms, 1)]
