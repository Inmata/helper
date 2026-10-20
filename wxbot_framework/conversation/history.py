"""聊天原文、快照、generation 与上下文重置。

成员身份与群目录由 ``members.py`` 负责；这里只处理“说过什么”：
落库、按天/按群读取、跨天与清空后的版本校验。写入共用成员库的
同一连接与事务，不自己持有连接。
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta

from ..commands import effective_text
from ..timeutil import iso_utc, parse_utc
from .members import (BEIJING, ChatSnapshot, ConversationSnapshot, GroupChatSnapshot, MemberError,
                      SnapshotExpired)


class ConversationHistory:
    """按账号、群与成员 ID 隔离的聊天原文；由 ``MemberStore`` 持有。"""

    def __init__(self, members):
        self.members = members
        self._active_day = None

    @property
    def _lock(self):
        return self.members.database.lock

    def _day_bounds(self):
        local = self.members.now().astimezone(BEIJING)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return (start.date().isoformat(), iso_utc(start),
                iso_utc(start + timedelta(days=1)))

    def rollover(self):
        """即使没有新消息也可调用；停机期间跨天则在启动时补清理。"""
        members = self.members
        with self._lock:
            day, start, _ = self._day_bounds()
            if day == self._active_day:
                return False
            with members.transaction() as db:
                for room in members.groups:
                    db.execute("""INSERT INTO chat_buffers(account,room,local_day) VALUES(?,?,?)
                        ON CONFLICT(account,room) DO UPDATE SET local_day=excluded.local_day,
                        generation=chat_buffers.generation+? WHERE chat_buffers.local_day<>excluded.local_day""",
                               (members.account, room, day, int(members.daily_clear)))
                    if members.daily_clear:
                        db.execute("""DELETE FROM messages WHERE account=? AND room=?
                            AND replace(received_at,'+00:00','Z')<?""",
                                   (members.account, room, iso_utc(start)))
            self._active_day = day
            return True

    def clear(self, room):
        members = self.members
        if room not in members.groups:
            raise MemberError("这个群尚未启用聊天记录")
        with self._lock:
            self.rollover()
            with members.transaction() as db:
                count = db.execute("DELETE FROM messages WHERE account=? AND room=?",
                                   (members.account, room)).rowcount
                db.execute("UPDATE chat_buffers SET generation=generation+1 WHERE account=? AND room=?",
                           (members.account, room))
            return count

    def today_snapshot(self, name, room):
        """完整读取当天的成员文本，数量不受控制台 history 的条数限制。"""
        members = self.members
        with self._lock:
            self.rollover()
            member = members.resolve(name, room)
            if member.wxid is None:
                raise MemberError(f"{member.name} 尚未绑定微信 ID")
            day, start, end = self._day_bounds()
            db = members.connection()
            state = db.execute("SELECT generation FROM chat_buffers WHERE account=? AND room=?",
                               (members.account, room)).fetchone()
            if state is None:
                raise MemberError("这个群尚未启用聊天记录")
            rows = db.execute("""SELECT * FROM messages WHERE account=? AND room=? AND sender_id=?
                AND replace(received_at,'+00:00','Z')>=? AND replace(received_at,'+00:00','Z')<?
                AND bot_generated=0 ORDER BY id""",
                              (members.account, room, member.wxid, iso_utc(start), iso_utc(end))).fetchall()
            return ChatSnapshot(member, day, state[0], tuple(dict(row) for row in rows))

    def today_group_snapshot(self, room):
        """完整读取本群今天所有成员的文本，未绑定者也保留身份区分。"""
        members = self.members
        if room not in members.groups:
            raise MemberError("这个群尚未启用聊天记录")
        with self._lock:
            self.rollover()
            day, start, end = self._day_bounds()
            db = members.connection()
            state = db.execute("SELECT generation FROM chat_buffers WHERE account=? AND room=?",
                               (members.account, room)).fetchone()
            rows = db.execute("""SELECT m.*, p.name AS speaker_name FROM messages m
                LEFT JOIN people p ON p.account=m.account AND p.room=m.room AND p.wxid=m.sender_id
                WHERE m.account=? AND m.room=?
                AND replace(m.received_at,'+00:00','Z')>=? AND replace(m.received_at,'+00:00','Z')<?
                AND m.bot_generated=0 ORDER BY m.id""", (members.account, room, start, end)).fetchall()
            speakers, messages = {}, []
            for row in rows:
                item = dict(row)
                number = speakers.setdefault(item["sender_id"], len(speakers) + 1)
                # 发给模型的稳定编号替代真实微信 ID；昵称只用于可读性。
                item["speaker"] = f"P{number}"
                item["speaker_name"] = (item["speaker_name"] or members.safe_wechat_name(item["sender_id"], room)
                                        or f"未绑定成员{number}")
                messages.append(item)
            return GroupChatSnapshot(room, day, state[0], tuple(messages))

    def snapshot_is_current(self, snapshot):
        members = self.members
        with self._lock:
            self.rollover()
            state = members.connection().execute(
                "SELECT local_day,generation FROM chat_buffers WHERE account=? AND room=?",
                (members.account, snapshot.room)).fetchone()
            return bool(state and (not members.daily_clear or state[0] == snapshot.day)
                        and state[1] == snapshot.generation)

    def conversation_snapshot(self, message, *, group_limit=25):
        """以当前消息为锚点，读取跨天的本人 10 条与全群近期上下文。"""
        members = self.members
        if message.chat_id not in members.groups:
            raise MemberError("这个群尚未启用聊天记录")
        with self._lock:
            self.rollover()
            day = self._day_bounds()[0]
            db = members.connection()
            anchor = self.message_id(message)
            state = db.execute("SELECT generation,context_floor FROM chat_buffers WHERE account=? AND room=?",
                               (members.account, message.chat_id)).fetchone()
            if anchor is None or anchor <= state[1]:
                raise SnapshotExpired("当前消息已被重置")
            previous = self.recent_rows(message.chat_id, limit=10, sender_id=message.sender_id,
                                        before_id=anchor, context=True)
            group = self.recent_rows(message.chat_id, limit=group_limit, before_id=anchor, context=True)
            # 真实收发顺序（含机器人自己的回复）用于多轮承接，30 分钟无新消息视为换话题。
            history = self.recent_rows(message.chat_id, limit=40, before_id=anchor, context=True,
                                       include_bot=True, continuity_seconds=1800)
            previous_ids = {row["id"] for row in previous}
            group = self.label_speakers([row for row in group if row["id"] not in previous_ids], message.chat_id)
            history = self.label_speakers(history, message.chat_id)
            member = members.find_by_wxid(message.sender_id, message.chat_id)
            return ConversationSnapshot(message.chat_id, day, state[0], member.name if member else
                                        members.safe_wechat_name(message.sender_id, message.chat_id) or "群友",
                                        tuple(previous), message.text, tuple(group), anchor, tuple(history))

    @contextmanager
    def delivery_guard(self, snapshot):
        # 仅原生发送期间持锁，随机等待和大模型调用不会阻塞 /清空。
        with self._lock:
            if not self.snapshot_is_current(snapshot):
                raise SnapshotExpired("聊天记录已清空或已跨天")
            yield

    def record(self, message):
        members = self.members
        if (not members.history_enabled or message.type != 1 or not message.is_group
                or message.chat_id not in members.groups):
            return False
        if message.data.get("self_wxid", members.account) != members.account:
            return False
        members.validate_member_id(message.chat_id, message.sender_id)
        if not isinstance(message.text, str) or not message.session_id:
            raise MemberError("群记录缺少正文或连接标识")
        native_name = message.data.get("sender_wechat_name")
        if native_name:
            try:
                members.remember_wechat_name(message.sender_id, native_name)
            except MemberError:
                pass
        at_names = message.data.get("at_member_names")
        if isinstance(at_names, dict) and at_names:
            members.sync_room_directory(message.chat_id, tuple(at_names.keys()), at_names)
        with self._lock, members.transaction() as db:
            self.rollover()
            db.execute("INSERT OR IGNORE INTO room_members(account,room,wxid) VALUES(?,?,?)",
                       (members.account, message.chat_id, message.sender_id))
            now = iso_utc(members.now())
            if message.data.get("bot_generated"):
                # 原生回显可能带 "@昵称 " 标签行或尾部空白；落库的是裸正文，几种形式都要比对。
                variants = {message.text, message.text.strip()}
                first, sep, rest = message.text.partition("\n")
                if sep and first.startswith("@") and " " in first:
                    variants.update((rest, rest.strip()))
                echoes = [value for value in variants if value]
                if echoes:
                    placeholders = ",".join("?" for _ in echoes)
                    cutoff = members.now() - timedelta(seconds=120)
                    echo = db.execute(f"""SELECT 1 FROM messages WHERE account=? AND room=? AND bot_generated=1
                        AND text IN ({placeholders}) AND replace(received_at,'+00:00','Z')>=? LIMIT 1""",
                        (members.account, message.chat_id, *echoes, iso_utc(cutoff))).fetchone()
                    if echo:
                        return False
            db.execute("""UPDATE room_members SET first_seen=COALESCE(first_seen,?),last_seen=?
                WHERE account=? AND room=? AND wxid=?""",
                       (now, now, members.account, message.chat_id, message.sender_id))
            result = db.execute("""INSERT OR IGNORE INTO messages
                (account,room,sender_id,text,received_at,session_id,sequence,is_self,bot_generated)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (members.account, message.chat_id, message.sender_id, message.text,
                 now, message.session_id, message.sequence,
                 int(message.is_self), int(bool(message.data.get("bot_generated")))))
            return result.rowcount > 0

    def record_bot_reply(self, room, text):
        """把成功发出的机器人回复按真实时间落库，供多轮对话承接自己刚才的话。"""
        members = self.members
        if not members.history_enabled or room not in members.groups:
            return False
        if not isinstance(text, str) or not text.strip():
            return False
        with self._lock:
            self.rollover()
            with members.transaction() as db:
                moment = members.now()
                now = iso_utc(moment)
                # 原生回显可能先于这里落库；已有回显时不再补一条裸正文。
                # 逐行取回再按去空白后的正文比对，避免尾部换行或 LIKE 通配符误判。
                echoes = db.execute("""SELECT text FROM messages WHERE account=? AND room=?
                    AND bot_generated=1 AND session_id<>'bot-reply'
                    AND replace(received_at,'+00:00','Z')>=?""",
                    (members.account, room, iso_utc(moment - timedelta(seconds=120)))).fetchall()
                wanted = text.strip()
                for row in echoes:
                    candidate = row["text"].strip()
                    first, sep, rest = candidate.partition("\n")
                    if sep and first.startswith("@") and " " in first:
                        candidate = rest.strip()
                    if candidate == wanted:
                        return False
                result = db.execute("""INSERT INTO messages
                    (account,room,sender_id,text,received_at,session_id,sequence,is_self,bot_generated)
                    VALUES(?,?,?,?,?,?,0,1,1)""",
                    (members.account, room, members.account, text.strip(), now, "bot-reply"))
                return result.rowcount > 0

    def message_id(self, message):
        members = self.members
        with self._lock:
            row = members.connection().execute("""SELECT id FROM messages WHERE account=? AND room=?
                AND sender_id=? AND session_id=? AND sequence=? AND text=? ORDER BY id DESC LIMIT 1""",
                (members.account, message.chat_id, message.sender_id, message.session_id,
                 message.sequence, message.text)).fetchone()
            return row[0] if row else None

    def latest_id(self, room):
        members = self.members
        with self._lock:
            return members.connection().execute(
                "SELECT COALESCE(MAX(id),0) FROM messages WHERE account=? AND room=?",
                (members.account, room)).fetchone()[0]

    def recent_rows(self, room, *, limit=200, sender_id=None, before_id=None, since=None, until=None,
                    context=False, include_bot=False, continuity_seconds=None):
        members = self.members
        if room not in members.groups or type(limit) is not int or not 1 <= limit <= 5000:
            raise MemberError("查询需要已启用的群，条数应为 1—5000")
        with self._lock:
            self.rollover()
            sql = "SELECT * FROM messages WHERE account=? AND room=?"
            if not include_bot:
                sql += " AND bot_generated=0"
            values = [members.account, room]
            if sender_id is not None:
                sql += " AND sender_id=?"
                values.append(sender_id)
            if before_id is not None:
                sql += " AND id<?"
                values.append(before_id)
            if since is not None:
                sql += " AND replace(received_at,'+00:00','Z')>=?"
                values.append(iso_utc(since))
            if until is not None:
                sql += " AND replace(received_at,'+00:00','Z')<?"
                values.append(iso_utc(until))
            if context:
                floor = members.connection().execute(
                    "SELECT context_floor FROM chat_buffers WHERE account=? AND room=?",
                    (members.account, room)).fetchone()[0]
                sql += " AND id>?"
                values.append(floor)
            rows, newest = [], None
            for row in members.connection().execute(sql + " ORDER BY id DESC", values):
                if continuity_seconds is not None:
                    try:
                        stamp = parse_utc(row["received_at"])
                    except (TypeError, ValueError):
                        stamp = None
                    if (newest is not None and stamp is not None
                            and (newest - stamp).total_seconds() > continuity_seconds):
                        break
                    if stamp is not None:
                        newest = stamp
                if effective_text(row["text"]):
                    rows.append(dict(row))
                    if len(rows) >= limit:
                        break
            return list(reversed(rows))

    def label_speakers(self, rows, room):
        members = self.members
        speakers, result = {}, []
        for row in rows:
            item = dict(row)
            number = speakers.setdefault(item["sender_id"], len(speakers) + 1)
            item.update(speaker=f"P{number}", speaker_name=members.display_name(item["sender_id"], room)
                        or f"未绑定成员{number}")
            result.append(item)
        return result

    def recent_snapshot(self, room, *, name=None, limit=200, today=False, before_id=None, context=False):
        members = self.members
        with self._lock:
            self.rollover()
            member = members.resolve(name, room) if name is not None else None
            if member and not member.wxid:
                raise MemberError("该成员尚未绑定微信 ID")
            day, start, _ = self._day_bounds()
            rows = self.recent_rows(room, limit=limit, sender_id=member.wxid if member else None,
                                    before_id=before_id, since=start if today else None, context=context)
            state = members.connection().execute("SELECT generation FROM chat_buffers WHERE account=? AND room=?",
                                                 (members.account, room)).fetchone()
            scope = "today" if today else "recent"
            if member:
                return ChatSnapshot(member, day, state[0], tuple(rows), scope, before_id or 0)
            return GroupChatSnapshot(room, day, state[0], tuple(self.label_speakers(rows, room)),
                                     scope, before_id or 0)

    def reset_context(self, room):
        members = self.members
        if room not in members.groups:
            raise MemberError("这个群尚未启用聊天记录")
        with self._lock:
            self.rollover()
            with members.transaction() as db:
                db.execute("UPDATE chat_buffers SET generation=generation+1,context_floor=? WHERE account=? AND room=?",
                           (self.latest_id(room), members.account, room))

    def history(self, name, room=None, *, limit=100, since=None):
        members = self.members
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise MemberError("历史查询 limit 应为 1–1000 的整数")
        if since is not None:
            if not isinstance(since, datetime) or since.tzinfo is None or since.utcoffset() is None:
                raise MemberError("since 应为带时区的 datetime")
            since = iso_utc(since)
        with self._lock:
            self.rollover()
            member = members.resolve(name, room)
            if member.wxid is None:
                raise MemberError(f"{member.name} 尚未绑定微信 ID")
            sql = "SELECT * FROM messages WHERE account=? AND room=? AND sender_id=?"
            values = [members.account, member.room, member.wxid]
            if since is not None:
                sql += " AND replace(received_at,'+00:00','Z')>=?"
                values.append(iso_utc(since))
            rows = members.connection().execute(sql + " ORDER BY id DESC LIMIT ?", [*values, limit]).fetchall()
            return [dict(row) for row in reversed(rows)]
