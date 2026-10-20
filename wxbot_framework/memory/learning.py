"""增量学习的读写边界：先取版本快照，锁外调用模型，再整批原子提交。

事务由用例控制：下层写方法只执行 SQL、不自行提交，异常沿调用栈抛出，
因此一个非法来源或写入故障不会留下半批知识或推进游标。
"""
from __future__ import annotations

import json

from ..commands import effective_text


class LearningBatch:
    """一次学习批次的游标、来源与提交边界；由 ``MemoryWorker`` 使用。"""

    def __init__(self, knowledge):
        self.knowledge = knowledge

    @property
    def account(self):
        return self.knowledge.account

    def generation(self, room):
        return self.knowledge.connection().execute(
            "SELECT generation FROM chat_buffers WHERE account=? AND room=?",
            (self.account, room)).fetchone()[0]

    def snapshot(self, room):
        """锁内取 generation 与人物 epoch，供锁外模型调用前后判断有效性。"""
        knowledge = self.knowledge
        with knowledge.lock:
            return {"room": room, "generation": self.generation(room),
                    "epochs": knowledge.epoch_snapshot(room)}

    def current(self, snapshot):
        """清空或跨天后 generation 变化，整批结果作废。"""
        with self.knowledge.lock:
            return self.generation(snapshot["room"]) == snapshot["generation"]

    def batch(self, room, limit=50):
        """按游标取下一批原文；有效条数达到 limit 即停，跳过控制指令。"""
        with self.knowledge.lock:
            result, effective = [], 0
            for row in self.knowledge.connection().execute("""SELECT * FROM messages
                WHERE account=? AND room=? AND id>? ORDER BY id LIMIT ?""",
                (self.account, room, self.cursor(room), max(1000, limit))):
                result.append(dict(row))
                effective += int(not row["bot_generated"] and effective_text(row["text"]))
                if effective >= limit:
                    break
            return result

    def cursor(self, room):
        with self.knowledge.lock:
            row = self.knowledge.connection().execute(
                "SELECT cursor_id FROM memory_cursors WHERE account=? AND room=?",
                (self.account, room)).fetchone()
            return row[0] if row else 0

    def commit(self, snapshot, *, mentions=(), items=(), cursor=None, valid=None):
        """把 mentions、实际接纳的知识和游标放进同一外层事务。

        版本变化或调用方判定失效时整批丢弃；任何异常整批回滚，不推进游标。
        """
        room = snapshot["room"]
        knowledge = self.knowledge
        with knowledge.lock, knowledge.database.transaction() as db:
            if valid is not None and not valid():
                return False
            if self.generation(room) != snapshot["generation"]:
                return False
            for entity_id, message_id, speaker_id in mentions:
                db.execute("INSERT OR IGNORE INTO knowledge_mentions VALUES(?,?,?)",
                           (entity_id, message_id, speaker_id))
            for entity, slot, kind, content, tags, sources, expected_epoch in items:
                knowledge.put(room, entity["id"], slot, kind, content, tags=tags,
                              sources=sources, expected_epoch=expected_epoch)
                if not entity["wxid"]:
                    for source in sources:
                        db.execute("INSERT OR IGNORE INTO knowledge_mentions VALUES(?,?,?)",
                                   (entity["id"], source["id"], source["sender_id"]))
            if cursor is not None:
                db.execute("""INSERT INTO memory_cursors(account,room,cursor_id,updated_at) VALUES(?,?,?,?)
                    ON CONFLICT(account,room) DO UPDATE SET
                    cursor_id=MAX(memory_cursors.cursor_id,excluded.cursor_id),
                    updated_at=excluded.updated_at,last_error=NULL""",
                    (self.account, room, cursor, knowledge.now()))
            return True

    def save_style(self, room, entity, rows, *, generation, description):
        """表达习惯与样本水位一起提交；generation 变化时整条丢弃。"""
        knowledge = self.knowledge
        with knowledge.lock, knowledge.database.transaction() as db:
            if self.generation(room) != generation:
                return None
            item_id = knowledge.put(room, entity["id"], "表达习惯", "语言习惯", description,
                                    sources=rows, expected_epoch=entity["epoch"])
            if item_id is None:
                return None
            db.execute("INSERT OR REPLACE INTO member_styles VALUES(?,?,?,?,?,?)",
                       (entity["id"], rows[-1]["id"], len(rows),
                        sum(len(row["text"]) for row in rows) / len(rows),
                        json.dumps([row["id"] for row in rows]), knowledge.now()))
            return item_id

    def record_error(self, room, text):
        with self.knowledge.lock, self.knowledge.database.transaction() as db:
            db.execute("""INSERT INTO memory_cursors(account,room,last_error) VALUES(?,?,?)
                ON CONFLICT(account,room) DO UPDATE SET last_error=excluded.last_error""",
                (self.account, room, text[:160]))

    def clear_error(self, room):
        with self.knowledge.lock, self.knowledge.database.transaction() as db:
            db.execute("UPDATE memory_cursors SET last_error=NULL WHERE account=? AND room=?",
                       (self.account, room))
