"""真实成员库、图片持久记录、跨线程预算和到期收口。"""
import hashlib
import threading
import time
import unittest
from dataclasses import replace
from unittest.mock import Mock

import test_chat_summary as daily
from wxbot_framework.conversation.person_context import person_context
from wxbot_framework.conversation.reply_wait import ReplyWait
from wxbot_framework.conversation.mention_tools import _within
from wxbot_framework.integrations.reply_budget import CURRENT, ReplyBudget, ReplyExpired, remaining
from wxbot_framework.memory.knowledge import KnowledgeStore
from wxbot_framework.recent_images import RecentImages
from plugins.mention_chat.voice_reply import VoiceReply


class ReplyImprovements(unittest.TestCase):
    setUp = daily.DailySummaryTests.setUp
    tearDown = daily.DailySummaryTests.tearDown
    new_store = daily.DailySummaryTests.new_store
    message = daily.DailySummaryTests.message
    model = daily.DailySummaryTests.model

    def test_person_history_keeps_commands_and_excludes_future_and_other_room(self):
        knowledge = KnowledgeStore(self.store)
        knowledge.initialize()
        self.store.record(self.message('/生图 暖色水彩小狗，背景留白', sequence=1))
        current = self.message('测试成员最近画什么？', sequence=2, sender=daily.OTHER)
        self.store.record(current)
        self.store.record(self.message('/生图 以后改画机甲', sequence=3))
        self.store.record(self.message('/生图 另一个群的独立话题', sequence=4, room=daily.OTHER_ROOM))
        jev = Mock()
        jev.noul.side_effect = lambda state, questions, **kw: [0.99] * len(questions)
        rows = person_context(self.store, knowledge, jev, daily.ROOM, current.text,
                              before_id=self.store.message_id(current))
        self.assertTrue(any('暖色水彩小狗' in r.get('text', '') for r in rows))
        self.assertFalse(any('机甲' in r.get('text', '') or '独立话题' in r.get('text', '') for r in rows))
        self.assertTrue(all(r['id'] < self.store.message_id(current) for r in rows))

    def test_sent_picture_survives_reopen_and_explicit_quote_chooses_first(self):
        first, second = self.root / 'first.png', self.root / 'second.png'
        first.write_bytes(b'first-image-fixture')
        second.write_bytes(b'second-image-fixture')
        message = self.message('画两张', sequence=1)
        self.store.record(message)
        source = self.store.message_id(message)
        for path in (first, second):
            self.store.record_sent_image(daily.ROOM, path, hashlib.md5(path.read_bytes()).hexdigest(), source)
        index = RecentImages()
        index.restore_sent(self.new_store(), daily.ROOM)
        record = index.quoted(daily.ROOM, md5=hashlib.md5(first.read_bytes()).hexdigest())
        self.assertEqual(record.path, str(first.resolve()))
        self.assertEqual(index.recent(daily.ROOM, daily.CAT, before=time.monotonic(), within=120, from_others=True), ())
        self.assertIsNone(index.quoted(daily.OTHER_ROOM, md5=record.md5))
        self.store.reset_context(daily.ROOM)
        index.restore_sent(self.store, daily.ROOM)
        self.assertIsNone(index.quoted(daily.ROOM, md5=record.md5))

    def test_text_preference_persists_and_single_turn_voice_does_not_clear_it(self):
        voice = VoiceReply()
        ctx = Mock(members=self.store, room_state=self.store.room_state)
        message = self.message('以后只用文字', sequence=1)
        jev = Mock()
        jev.noul.return_value = [0.99, 0.01, 0.01]
        voice.update_preference(message, ctx, message.text, jev)
        self.assertTrue(voice.text_only(message, self.new_store().room_state))
        jev.noul.return_value = [0.01, 0.01, 0.99]
        self.assertTrue(voice.update_preference(message, ctx, '这条用语音说', jev))
        self.assertTrue(voice.text_only(message, ctx.room_state))

    def test_deadline_closes_old_worker_and_emits_only_one_status(self):
        message = self.message('查询资料', sequence=1)
        self.store.record(message)
        ctx = Mock()
        ctx.members = self.store
        ctx.personality.return_value = self.store.personality(daily.ROOM)
        ctx.response_guard = lambda snapshot, persona: self.store.delivery_guard(snapshot)
        sent = threading.Event()
        def reply(*args, **kwargs):
            with kwargs['delivery_guard']():
                sent.set()
            return True
        ctx.reply_at.side_effect = reply
        wait = ReplyWait()
        try:
            budget = wait.accept(message, ctx, 90)
            budget.deadline = time.monotonic() - 1
            self.assertTrue(sent.wait(2))
            wait.finish(budget, '重复结果')
            self.assertEqual(ctx.reply_at.call_count, 1)
            with self.assertRaises(ReplyExpired), budget.delivery():
                pass
        finally:
            wait.close()


class ReplyBudgetTests(unittest.TestCase):
    def test_nested_tool_thread_inherits_same_budget(self):
        now = time.monotonic()
        budget = ReplyBudget(now, 1)
        token = CURRENT.set(budget)
        try:
            timeout = _within(lambda: remaining(60, kind='llm'), 10)
            self.assertLessEqual(timeout, 1)
            self.assertEqual(budget.calls, {'llm': 1})
            budget.closed = True
            with self.assertRaises(ReplyExpired):
                remaining(60)
        finally:
            CURRENT.reset(token)

    def test_http_call_limit_counts_every_attempt(self):
        budget = ReplyBudget(time.monotonic())
        for _ in range(3):
            budget.remaining(10, kind='llm')
        with self.assertRaises(ReplyExpired):
            budget.remaining(10, kind='llm')
