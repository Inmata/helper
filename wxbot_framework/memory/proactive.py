"""主动发言候选的持久化：历史状态、配额、冷却与最近主题。

调度与生成流程留在 ``hourly.py``；这里只负责读写候选记录，
写入共用知识库的同一连接与事务。
"""
from __future__ import annotations

from datetime import timedelta

from ..conversation.members import BEIJING
from ..timeutil import iso_utc, parse_utc


class ProactiveCandidates:
    """``proactive_candidates`` 表的公开读写接口。"""

    def __init__(self, knowledge, clock=None):
        self.knowledge = knowledge
        # 时间戳沿用调用方（调度器）的时钟，测试与离线重放可注入同一时间源。
        self.clock = clock or knowledge.now

    @property
    def account(self):
        return self.knowledge.account

    def miss_stale(self, run_id):
        """上一运行尚未执行的候选没有补发价值，保留记录并标记 missed。"""
        now = iso_utc(self.clock())
        with self.knowledge.lock, self.knowledge.database.transaction() as db:
            db.execute("""UPDATE proactive_candidates SET status='missed',updated_at=?
                WHERE account=? AND run_id<>? AND status='scheduled'""", (now, self.account, run_id))

    def schedule(self, room, run_id, slot, anchor, generation):
        timestamp = iso_utc(slot)
        with self.knowledge.lock, self.knowledge.database.transaction() as db:
            row = db.execute("""INSERT INTO proactive_candidates
                (account,room,run_id,due_at,status,anchor_id,context_generation,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (self.account, room, run_id, timestamp, "scheduled", anchor, generation,
                 timestamp, timestamp))
            return row.lastrowid

    def update(self, candidate_id, status, *, topic="", message="", error=""):
        timestamp = iso_utc(self.clock())
        with self.knowledge.lock, self.knowledge.database.transaction() as db:
            db.execute("""UPDATE proactive_candidates SET status=?,topic=?,message=?,error=?,
                updated_at=?,sent_at=CASE WHEN ? IN ('sent','unknown') THEN ? ELSE sent_at END
                WHERE id=?""",
                (status, topic[:120], message[:600], error[:200], timestamp, status,
                 timestamp, candidate_id))

    def record_activity(self, candidate_id, activity, speakers):
        with self.knowledge.lock, self.knowledge.database.transaction() as db:
            db.execute("UPDATE proactive_candidates SET activity_count=?,speaker_count=? WHERE id=?",
                       (activity, speakers, candidate_id))

    def status_of(self, candidate_id):
        with self.knowledge.lock:
            row = self.knowledge.connection().execute(
                "SELECT status FROM proactive_candidates WHERE id=?", (candidate_id,)).fetchone()
            return row[0] if row else None

    def usage(self, room, slot, *, cooldown_minutes):
        """当天已发条数，以及距上次成功发言是否已超过冷却。"""
        start = slot.astimezone(BEIJING).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        with self.knowledge.lock:
            db = self.knowledge.connection()
            count = db.execute("""SELECT COUNT(*) FROM proactive_candidates
                WHERE account=? AND room=?
                AND replace(due_at,'+00:00','Z')>=? AND replace(due_at,'+00:00','Z')<?
                AND status IN ('sent','unknown')""",
                (self.account, room, iso_utc(start), iso_utc(end))).fetchone()[0]
            latest = db.execute("""SELECT due_at FROM proactive_candidates
                WHERE account=? AND room=? AND status IN ('sent','unknown')
                ORDER BY replace(due_at,'+00:00','Z') DESC LIMIT 1""",
                (self.account, room)).fetchone()
        latest = latest["due_at"] if latest else None
        cooldown_ok = not latest or slot - parse_utc(latest) >= timedelta(minutes=cooldown_minutes)
        return count, cooldown_ok

    def recent_topics(self, room, limit=8):
        with self.knowledge.lock:
            return [row[0] for row in self.knowledge.connection().execute("""SELECT topic
                FROM proactive_candidates WHERE account=? AND room=? AND topic<>''
                ORDER BY id DESC LIMIT ?""", (self.account, room, limit))]
