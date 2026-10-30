"""最近收到的图片索引：供艾特回复按引用或“同一人刚发的图”找到本地图片。

原生文本服务在图片进入下载队列时记一条 ``pending``，下载结束（ready/failed/skipped）后更新同一条；
只保存会话、发送者、状态、本地路径、md5 和原始消息编号，不保存 XML、CDN 地址或密钥。
进程内单例、按群有界，在插件分发队列之外写入，所以不会被正在回复的艾特阻塞；
插件热重载不影响已记录的图片。
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, replace

PENDING, READY, FAILED, SKIPPED = "pending", "ready", "failed", "skipped"
STATUSES = (PENDING, READY, FAILED, SKIPPED)
MAX_PER_ROOM = 64
MAX_ROOMS = 256
# 文本与图片分属不同轮询批次，允许图片比提问晚一点被看到。
ARRIVAL_SLACK_SECONDS = 1.0


@dataclass(frozen=True)
class ImageRecord:
    room: str
    sender: str
    key: str
    status: str
    seen_at: float  # 首次看到这条图片消息的 time.monotonic()，与 Message.received_at 同一时钟
    native_message_id: str = ""
    md5: str = ""
    path: str = ""
    error: str = ""
    used: bool = False  # 已被一次看图成功用过：不再算“刚发的图”，仍可被引用


def _key(data):
    native = str(data.get("native_message_id") or "")
    if native and native != "0":
        return "id:" + native
    sequence = data.get("sequence")
    return f"seq:{sequence}" if type(sequence) is int else ""


def _md5(data):
    metadata = data.get("image_metadata")
    value = metadata.get("md5") if isinstance(metadata, dict) else ""
    return value.lower() if isinstance(value, str) and len(value) == 32 else ""


class RecentImages:
    def __init__(self, *, per_room=MAX_PER_ROOM, rooms=MAX_ROOMS, clock=time.monotonic):
        self.per_room = per_room
        self.rooms = rooms
        self.clock = clock
        self._rooms = OrderedDict()  # room -> deque[ImageRecord]，最近活跃的群在末尾
        self._changed = threading.Condition()

    def note(self, data):
        """记下一条图片事件；同一条消息再次到达时更新状态，保留首次看到的时刻。无效事件返回 None。"""
        if not isinstance(data, dict):
            return None
        room, key, status = data.get("chat_id") or "", _key(data), data.get("image_status")
        if not isinstance(room, str) or not room or not key or status not in STATUSES:
            return None
        path = data.get("image_path") if status == READY else ""
        error = data.get("image_error") if status in (FAILED, SKIPPED) else ""
        with self._changed:
            records = self._rooms.get(room)
            if records is None:
                records = self._rooms[room] = deque(maxlen=self.per_room)
                while len(self._rooms) > self.rooms:
                    self._rooms.popitem(last=False)
            else:
                self._rooms.move_to_end(room)
            for index, old in enumerate(records):
                if old.key == key:
                    record = replace(old, status=status, md5=_md5(data) or old.md5, path=str(path or ""),
                                     error=str(error or ""))
                    records[index] = record
                    break
            else:
                record = ImageRecord(room=room, sender=str(data.get("sender_id") or ""), key=key, status=status,
                                     seen_at=self.clock(), native_message_id=key[3:] if key.startswith("id:") else "",
                                     md5=_md5(data), path=str(path or ""), error=str(error or ""))
                records.append(record)
            self._changed.notify_all()
        return record

    def _records(self, room):
        with self._changed:
            return tuple(self._rooms.get(room, ()))

    def quoted(self, room, *, svrid="", md5=""):
        """按引用里的原消息编号找图，找不到再按 md5 找本群最近一张。"""
        records = self._records(room)
        if svrid:
            for record in reversed(records):
                if record.native_message_id == svrid:
                    return record
        if md5:
            md5 = md5.lower()
            for record in reversed(records):
                if record.md5 == md5:
                    return record
        return None

    def recent(self, room, sender, *, before, within):
        """这个人在提问前 ``within`` 秒内发的、还没被看过的图（按到达顺序）。"""
        return tuple(record for record in self._records(room)
                     if record.sender == sender and not record.used and
                     before - within <= record.seen_at <= before + ARRIVAL_SLACK_SECONDS)

    def mark_used(self, record):
        """这张图已被看过：之后不再按“刚发的图”选中，引用它仍然找得到。"""
        with self._changed:
            records = self._rooms.get(record.room, ())
            for index, old in enumerate(records):
                if old.key == record.key:
                    records[index] = replace(old, used=True)
                    return records[index]
        return None

    def wait(self, record, timeout):
        """等 pending 的图片下载结束，最多 ``timeout`` 秒；返回最新状态（仍可能是 pending）。"""
        deadline = self.clock() + timeout
        with self._changed:
            while True:
                current = next((item for item in self._rooms.get(record.room, ()) if item.key == record.key), record)
                remaining = deadline - self.clock()
                if current.status != PENDING or remaining <= 0:
                    return current
                self._changed.wait(remaining)

    def clear(self):
        with self._changed:
            self._rooms.clear()
            self._changed.notify_all()


RECENT_IMAGES = RecentImages()
