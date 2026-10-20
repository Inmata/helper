"""学习批次的原子提交：任一步失败都不留半批知识与游标。"""
import sqlite3
import unittest
from unittest.mock import Mock, patch

import test_chat_summary as daily

from wxbot_framework.memory.knowledge import KnowledgeStore
from wxbot_framework.memory.worker import MemoryWorker

ROOM, CAT = daily.ROOM, daily.CAT


class BatchAtomicityTests(unittest.TestCase):
    setUp = daily.DailySummaryTests.setUp
    tearDown = daily.DailySummaryTests.tearDown
    message = daily.DailySummaryTests.message
    new_store = daily.DailySummaryTests.new_store
    model = daily.DailySummaryTests.model

    def kb(self):
        knowledge = KnowledgeStore(self.store)
        knowledge.initialize()
        return knowledge

    def record(self, text):
        message = self.message(text, sender=CAT, sequence=1)
        self.store.record(message)
        return self.store.message_id(message)

    def counts(self, kb):
        db = kb.connection()
        return (db.execute("SELECT COUNT(*) FROM knowledge_items").fetchone()[0],
                db.execute("SELECT COUNT(*) FROM knowledge_sources").fetchone()[0])

    def item(self, kb, message_id, slot="爱好"):
        entity = kb.member_entity(ROOM, CAT)
        source = {"id": message_id, "sender_id": CAT, "received_at": "2026-10-10"}
        return entity, slot, "本人陈述", "喜欢吉他", [], [source], entity["epoch"]

    def test_write_failure_rolls_back_items_sources_and_cursor(self):
        kb = self.kb()
        message_id = self.record("我喜欢吉他和爬山")
        model = Mock(complete_json=Mock(return_value={"entities": [], "memories": [
            {"subject_type": "speaker", "subject": "P1", "key": "爱好一", "kind": "本人陈述",
             "content": "喜欢吉他", "tags": [], "message_ids": [message_id]},
            {"subject_type": "speaker", "subject": "P1", "key": "爱好二", "kind": "本人陈述",
             "content": "喜欢爬山", "tags": [], "message_ids": [message_id]}]}))
        worker = MemoryWorker(kb, model)
        original, calls = kb.put, []

        def flaky(*args, **kwargs):
            calls.append(args)
            if len(calls) == 2:
                raise sqlite3.OperationalError("模拟写入故障")
            return original(*args, **kwargs)

        with patch.object(kb, "put", side_effect=flaky):
            with self.assertRaises(sqlite3.OperationalError):
                worker.process(ROOM, kb.batch(ROOM))
        self.assertEqual(len(calls), 2)
        self.assertEqual(kb.search(ROOM, ""), [])
        self.assertEqual(kb.cursor(ROOM), 0)
        self.assertEqual(self.counts(kb), (0, 0))

    def test_commit_inside_outer_transaction_is_rolled_back_together(self):
        kb = self.kb()
        message_id = self.record("我喜欢吉他")
        snapshot = kb.learning.snapshot(ROOM)
        with self.assertRaises(RuntimeError):
            with kb.database.transaction():
                self.assertTrue(kb.learning.commit(snapshot, items=[self.item(kb, message_id)],
                                                   cursor=message_id))
                raise RuntimeError("外层事务失败")
        self.assertEqual(kb.search(ROOM, ""), [])
        self.assertEqual(kb.cursor(ROOM), 0)
        self.assertEqual(self.counts(kb), (0, 0))

    def test_generation_change_discards_the_whole_batch(self):
        kb = self.kb()
        message_id = self.record("我喜欢吉他")
        snapshot = kb.learning.snapshot(ROOM)
        item = self.item(kb, message_id)
        self.store.clear(ROOM)
        self.assertFalse(kb.learning.commit(snapshot, items=[item], cursor=message_id))
        self.assertEqual(kb.search(ROOM, ""), [])
        self.assertEqual(kb.cursor(ROOM), 0)
        self.assertEqual(self.counts(kb), (0, 0))


if __name__ == "__main__":
    unittest.main()
