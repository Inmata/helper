"""最近收到的图片索引：供艾特回复按引用或“同一人刚发的图”找到本地图片。

原生文本服务在图片进入下载队列时记一条 ``pending``，下载结束（ready/failed/skipped）后更新同一条；
只保存会话、发送者、状态、本地路径、md5 和原始消息编号，不保存 XML、CDN 地址或密钥。
进程内单例、按群有界，在插件分发队列之外写入，所以不会被正在回复的艾特阻塞；
插件热重载不影响已记录的图片。
"""
from __future__ import annotations

import threading
import time
import hashlib
from pathlib import Path
from collections import OrderedDict, deque
from dataclasses import dataclass, replace

PENDING, READY, FAILED, SKIPPED = "pending", "ready", "failed", "skipped"
STATUSES = (PENDING, READY, FAILED, SKIPPED)
GROUP_IMAGES_PER_ROOM = 48
SENT_IMAGES_PER_ROOM = 16
STICKERS_PER_ROOM = 64
MAX_ROOMS = 256
GENERATED_IMAGES = Path(__file__).resolve().parents[1] / ".runtime" / "generated_images"
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
    wx_type: int = 3
    outgoing: bool = False
    registered: bool = False  # 机器人发送并登记的产物，清空上下文后按数据库记录失效


def _key(data):
    native = str(data.get("native_message_id") or "")
    if native and native != "0":
        return "id:" + native
    if data.get("sent_md5"):
        return "sent:" + data["sent_md5"]
    sequence = data.get("sequence")
    return f"seq:{sequence}" if type(sequence) is int else ""


def _md5(data):
    metadata = data.get("image_metadata")
    value = metadata.get("md5") if isinstance(metadata, dict) else ""
    return value.lower() if isinstance(value, str) and len(value) == 32 else ""


class RecentImages:
    def __init__(self, *, per_room=GROUP_IMAGES_PER_ROOM, rooms=MAX_ROOMS, clock=time.monotonic):
        self.rooms = rooms
        self.clock = clock
        self._limits = {(3, False): per_room, (3, True): SENT_IMAGES_PER_ROOM, (47, False): STICKERS_PER_ROOM}
        self._rooms = {channel: OrderedDict() for channel in self._limits}
        self._changed = threading.Condition()

    def note(self, data):
        """记下一条图片事件；同一条消息再次到达时更新状态，保留首次看到的时刻。无效事件返回 None。"""
        if not isinstance(data, dict):
            return None
        room, key, status = data.get("chat_id") or "", _key(data), data.get("image_status")
        if not isinstance(room, str) or not room or not key or status not in STATUSES:
            return None
        wx_type = data.get("wx_type", 3)
        outgoing = wx_type == 3 and bool(data.get("is_self"))
        channel = (wx_type, outgoing)
        rooms = self._rooms.get(channel)
        if rooms is None:
            return None
        path = data.get("image_path") if status == READY else ""
        error = data.get("image_error") if status in (FAILED, SKIPPED) else ""
        native_id = key[3:] if key.startswith("id:") else ""
        md5 = _md5(data)
        registered = bool(data.get("sent_md5"))
        with self._changed:
            records = rooms.get(room)
            if records is None:
                records = rooms[room] = deque(maxlen=self._limits[channel])
                while len(rooms) > self.rooms:
                    rooms.popitem(last=False)
            else:
                rooms.move_to_end(room)
            for index, old in enumerate(records):
                # 发送回执可能比本地登记晚到；恢复同一张图时保留已关联的微信编号。
                same_sent = outgoing and md5 and old.md5 == md5 and (not native_id or not old.native_message_id)
                if old.key == key or same_sent:
                    record = replace(old, key=key if native_id else old.key,
                                     native_message_id=native_id or old.native_message_id,
                                     status=status, md5=md5 or old.md5, path=str(path or ""),
                                     registered=old.registered or registered,
                                     error=str(error or ""))
                    records[index] = record
                    break
            else:
                record = ImageRecord(room=room, sender=str(data.get("sender_id") or ""), key=key, status=status,
                                     seen_at=self.clock(), native_message_id=native_id, used=outgoing,
                                     md5=md5, path=str(path or ""), error=str(error or ""), wx_type=wx_type,
                                     outgoing=outgoing, registered=registered)
                records.append(record)
            self._changed.notify_all()
        return record

    def _records(self, room, wx_type=3, *, include_sent=False):
        with self._changed:
            received = tuple(self._rooms[wx_type, False].get(room, ()))
            if include_sent and wx_type == 3:
                return tuple(self._rooms[3, True].get(room, ())) + received
            return received

    def sent(self, room, sender, path, *, md5="", svrid=""):
        path = Path(path).resolve()
        md5 = md5 or hashlib.md5(path.read_bytes()).hexdigest()
        return self.note({"chat_id": room, "sender_id": sender, "is_self": True, "sent_md5": md5,
                          "native_message_id": svrid, "image_status": READY, "image_path": str(path),
                          "image_metadata": {"md5": md5}})

    def restore_sent(self, members, room, *, quote=None, source_id=0):
        if quote and quote.get("chatusr") and members.account not in (quote["chatusr"], quote.get("fromusr")):
            return
        rows = members.sent_images(room)[:SENT_IMAGES_PER_ROOM]
        allowed = {row["md5"] for row in rows}
        with self._changed:
            sent = self._rooms[3, True]
            records = sent.get(room)
            if records is not None:
                sent[room] = deque((r for r in records if not r.registered or r.md5 in allowed),
                                   maxlen=SENT_IMAGES_PER_ROOM)
        if quote:
            digest = (quote.get("image_md5") or "").lower()
            rows = [row for row in rows if row["md5"] == digest]
        for row in reversed(rows):
            if Path(row["path"]).is_file():
                self.sent(room, members.account, row["path"], md5=row["md5"])
        if (quote and quote.get("ref_type") == "3" and quote.get("image_md5") and
                members.account in (quote.get("fromusr"), quote.get("chatusr")) and
                self.quoted(room, svrid=quote.get("svrid", ""), md5=quote["image_md5"]) is None):
            # 旧版本没有登记产物；真实引用明确来自本账号，且原图摘要完全相同时才能回填。
            for path in GENERATED_IMAGES.iterdir() if GENERATED_IMAGES.is_dir() else ():
                if path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
                    continue
                if hashlib.md5(path.read_bytes()).hexdigest() == quote["image_md5"].lower():
                    record = self.sent(room, members.account, path, md5=quote["image_md5"], svrid=quote.get("svrid", ""))
                    members.record_sent_image(room, record.path, record.md5, source_id)
                    break

    def quoted(self, room, *, svrid="", md5="", wx_type=3):
        """按引用里的原消息编号找图，找不到再按 md5 找本群最近一张。"""
        records = self._records(room, wx_type, include_sent=True)
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

    def recent(self, room, sender, *, before, within, from_others=False, wx_type=3):
        """提问前 ``within`` 秒内、还没被看过的图（按到达顺序）：默认是这个人自己发的，``from_others`` 时是别人发的。"""
        return tuple(record for record in self._records(room, wx_type)
                     if (record.sender != sender) == from_others and not record.used and
                     before - within <= record.seen_at <= before + ARRIVAL_SLACK_SECONDS)

    def mark_used(self, record):
        """这张图已被看过：之后不再按“刚发的图”选中，引用它仍然找得到。"""
        with self._changed:
            records = self._rooms[record.wx_type, record.outgoing].get(record.room, ())
            for index, old in enumerate(records):
                if old.key == record.key:
                    records[index] = replace(old, used=True)
                    return records[index]
        return None

    def wait(self, record, timeout, *, stopped=None):
        """等 pending 的图片下载结束，最多 ``timeout`` 秒；返回最新状态（仍可能是 pending）。"""
        deadline = self.clock() + timeout
        with self._changed:
            while True:
                current = next((item for item in self._rooms[record.wx_type, record.outgoing].get(record.room, ())
                                if item.key == record.key), record)
                remaining = deadline - self.clock()
                if current.status != PENDING or remaining <= 0 or (stopped is not None and stopped.is_set()):
                    return current
                self._changed.wait(min(remaining, 0.05) if stopped is not None else remaining)

    def clear(self):
        with self._changed:
            for rooms in self._rooms.values():
                rooms.clear()
            self._changed.notify_all()


RECENT_IMAGES = RecentImages()
