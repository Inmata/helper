"""真实成员库、图片持久记录、跨线程预算和到期收口。"""
import hashlib
import json
import threading
import time
import unittest
from contextlib import nullcontext
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock, patch

import test_chat_summary as daily
from wxbot_framework.conversation.person_context import person_context
from wxbot_framework.conversation.reply_wait import ReplyWait
from wxbot_framework.conversation.status_replies import StatusReplies
from wxbot_framework.conversation.turns import CORRECTION_INTENT
from wxbot_framework.conversation.mention_tools import _within
from wxbot_framework.integrations.reply_budget import CURRENT, ReplyBudget, ReplyDraft, ReplyExpired, remaining
from wxbot_framework.memory.knowledge import KnowledgeStore
from wxbot_framework.recent_images import RecentImages
from plugins.mention_chat.voice_reply import VoiceReply
from plugins.mention_chat import Plugin as MentionPlugin


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

    def test_restoring_bot_images_preserves_all_48_group_images_for_quotes_and_recent_lookup(self):
        message = self.message('画图', sequence=1)
        self.store.record(message)
        source = self.store.message_id(message)
        digests = []
        for number in range(64):
            self.now += timedelta(seconds=1)
            path = self.root / f'generated-{number}.png'
            path.write_bytes(str(number).encode())
            digests.append(hashlib.md5(path.read_bytes()).hexdigest())
            self.store.record_sent_image(daily.ROOM, path, digests[-1], source)

        index = RecentImages()
        index.restore_sent(self.store, daily.ROOM)
        for number in range(49):
            index.note(dict(chat_id=daily.ROOM, sender_id=daily.CAT, native_message_id=str(100 + number),
                            image_status='ready', image_path=str(self.root / f'group-{number}.png')))
        index.restore_sent(self.store, daily.ROOM)
        self.assertIsNotNone(index.quoted(daily.ROOM, svrid='148'))
        self.assertIsNone(index.quoted(daily.ROOM, svrid='100'))
        recent = index.recent(daily.ROOM, daily.OTHER, before=time.monotonic(), within=120, from_others=True)
        self.assertEqual([item.native_message_id for item in recent], [str(i) for i in range(101, 149)])
        self.assertIsNone(index.quoted(daily.ROOM, md5=digests[-17]))
        for digest in digests[-16:]:
            self.assertIsNotNone(index.quoted(daily.ROOM, md5=digest))
        # 微信补回服务器编号，同一张出图仍只占一格；下次按摘要恢复也保留编号。
        last = index.quoted(daily.ROOM, md5=digests[-1])
        index.sent(daily.ROOM, self.store.account, last.path, md5=last.md5, svrid='900')
        quote = dict(ref_type='3', svrid='900', image_md5=last.md5, chatusr=self.store.account)
        index.restore_sent(self.store, daily.ROOM, quote=quote, source_id=source)
        self.assertEqual(index.quoted(daily.ROOM, svrid='900').path, last.path)
        self.assertIsNotNone(index.quoted(daily.ROOM, md5=digests[-16]))

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
        status = StatusReplies(ctx.logger)
        wait = ReplyWait(status)
        try:
            budget = wait.accept(message, ctx, 90)
            budget.stage = "看图"
            budget.deadline = time.monotonic() - 1
            self.assertTrue(sent.wait(2))
            wait.finish(budget, '重复结果')
            self.assertEqual(ctx.reply_at.call_count, 1)
            self.assertEqual(ctx.reply_at.call_args.args[1], "我等那张图的识别结果等太久了，没来得及回你。")
            ctx.llm_for.assert_not_called()
            ctx.jev.noul.assert_not_called()
            with self.assertRaises(ReplyExpired), budget.delivery():
                pass
        finally:
            wait.close()
            status.close()

    def test_self_correction_reads_own_scoped_records_and_revises_the_denial(self):
        original_time = self.now
        self.now -= timedelta(days=15)
        self.store.record_bot_reply(daily.ROOM, "太早：我说过要选小林")
        self.now += timedelta(days=8)
        self.store.record_bot_reply(daily.ROOM, "清空前：我说过要选小林")
        self.store.reset_context(daily.ROOM)
        self.now += timedelta(days=1)
        original = "非让我选的话，那我选小林，嫁过去就能蹭饭。"
        self.store.record_bot_reply(daily.ROOM, original, voice_id="18123")
        self.store.record_bot_reply(daily.OTHER_ROOM, "别群：我说过要选小林")
        self.new_store(account="wxid_second_bot").record_bot_reply(daily.ROOM, "别账号：我说过要选小林")
        self.store.record(self.message("群友复述：我说过要选小林", sequence=1))
        self.now = original_time
        message = self.message("@机器人\u2005你之前说结婚就选小林", sequence=2,
                               data={"is_at_self": True, "at_members": (daily.ROBOT,)})
        self.store.record(message)
        self.store.record_bot_reply(daily.ROOM, "未来：我说过要选小林")
        self.assertEqual([row["text"] for row in self.store.own_replies(self.store.conversation_snapshot(message))], [original])

        plugin = MentionPlugin()
        plugin.members, plugin.room_state = self.store, self.store.room_state
        self.backend.send_at_text = lambda room, text, ids: self.backend.send_text(room, text)
        denied = "我哪说过这种话，你别瞎编。"
        corrected = "我那会儿确实说过选小林，刚才否认说得太满了。"
        provider = self.ctx._llm.complete_text = Mock(side_effect=[denied, corrected])
        self.ctx._jev = Mock()
        verified = []

        def judge(state, questions, **kw):
            if "拟发送的回答" in state:
                verified.append(state["依据"]["本轮确认事实"])
                return [0.01, 0.01, 0.01, 0.99 if state["拟发送的回答"] == denied else 0.01, 0.01, 0.01, 0.01]
            if "候选原话" in state:
                own = [row["原话"] for row in state["候选原话"] if row["发言人"] == "机器人"]
                self.assertEqual(own, [original])
                return [0.99 if row["原话"] == original else 0.01 for row in state["候选原话"]]
            return [0.99 if q == CORRECTION_INTENT else 0.01 for q in questions]

        self.ctx._jev.noul.side_effect = judge
        plugin.on_message(message, self.ctx)
        self.assertIn(original, provider.call_args.args[1])
        self.assertEqual(len(verified), 2)
        self.assertEqual(verified[0], verified[1])
        self.assertEqual(len(self.backend.sent), 1)
        self.assertTrue(self.backend.sent[0][1].endswith(corrected))
        self.store.reset_context(daily.ROOM)

        missing = self.message("@机器人\u2005你之前说过要选小林", sequence=3,
                               data={**message.data, "quote": {"fromusr": daily.ROBOT, "text": original,
                                                               "svrid": "18123", "ref_type": "34"}})
        self.store.record(missing)
        provider.side_effect = ["这段记录里我没找着那句，能引用一下吗？"]
        with patch.object(self.store, "context_text", side_effect=AssertionError("语音引用必须先经过纠错范围过滤")):
            plugin.on_message(missing, self.ctx)
        self.assertNotIn(original, provider.call_args.args[1])
        self.assertEqual(verified[-1]["机器人原话"], [])
        self.assertTrue(self.backend.sent[-1][1].endswith("这段记录里我没找着那句，能引用一下吗？"))

    def test_correction_keeps_requesters_actual_words_and_bots_later_retelling(self):
        self.store.record(self.message("不如我农村户口", sequence=1))
        self.store.record_bot_reply(daily.ROOM, "他自称农村户口。")
        current = self.message("我啥时候是农村户口", sequence=2)
        self.store.record(current)
        self.backend.send_at_text = lambda room, text, ids: self.backend.send_text(room, text)
        plugin = MentionPlugin()
        plugin.members, plugin.room_state = self.store, self.store.room_state
        self.ctx._jev = Mock()
        self.ctx._llm.complete_text = Mock(return_value="你之前说过‘不如我农村户口’，我后来按那句转述了。")

        def judge(state, questions, **options):
            if "候选原话" in state:
                return [0.99] * len(questions)
            if "拟发送的回答" in state:
                facts = state["依据"]["本轮确认事实"]
                self.assertEqual(facts["群友原话"][0]["原话"], "不如我农村户口")
                self.assertTrue(facts["群友原话"][0]["本人"])
                self.assertEqual(facts["机器人原话"][0]["原话"], "他自称农村户口。")
            return [0.99 if question == CORRECTION_INTENT else 0.01 for question in questions]

        self.ctx._jev.noul.side_effect = judge
        for _ in plugin._reply_once(current, self.ctx, current.text, None):
            pass
        self.assertIn("不如我农村户口", self.ctx._llm.complete_text.call_args.args[1])
        self.assertIn("按那句转述了", self.backend.sent[-1][1])

    def test_own_reply_lookback_uses_the_message_time_without_context_reset(self):
        self.store.record_bot_reply(daily.ROOM, "早于窗口")
        self.now += timedelta(seconds=1)
        self.store.record_bot_reply(daily.ROOM, "恰好在窗口内")
        self.now += timedelta(days=14)
        current = self.message("你以前是不是说过", sequence=4)
        self.store.record(current)
        self.now += timedelta(days=1)
        snapshot = self.store.conversation_snapshot(current)
        self.assertEqual([r["text"] for r in self.store.own_replies(snapshot)], ["恰好在窗口内"])

    def test_old_status_claims_are_rejected_even_when_general_check_passes(self):
        for candidate, facts in (
            ("八成靠门票钱回血。", {"当前阶段": "组织回答"}),
            ("刚才没查准。", {"结果": "本轮没有可发送的答复，具体原因未确认"}),
            ("刚才卡壳没唱出来，重试我给你唱。", {"结果": "模型连接失败或超时，没有拿到回答"}),
        ):
            with self.subTest(candidate=candidate):
                message = self.message("测试状态", sequence=1)
                ctx = Mock()
                provider = ctx.llm_for.return_value.complete_text
                correct = "我这边还没拿到结果。"
                provider.side_effect = [candidate, correct]
                checked = []

                def judge(state, questions, **kwargs):
                    checked.append(state["拟发送回复"])
                    if checked[-1] == correct:
                        return [0.98, 0.01]
                    if "唱" in candidate:
                        return [0.74, 0.98]
                    return [0.24, 0.9]

                ctx.jev.noul.side_effect = judge
                delivered = threading.Event()
                ctx.reply_at.side_effect = lambda *args, **kw: delivered.set()
                status = StatusReplies(ctx.logger)
                try:
                    status.submit(message, ctx, facts, personality=self.store.personality(daily.ROOM), guard=nullcontext)
                    self.assertTrue(delivered.wait(2))
                    self.assertEqual(checked, [candidate, correct])
                    self.assertEqual(ctx.reply_at.call_count, 1)
                    self.assertEqual(ctx.reply_at.call_args.args[1], correct)
                finally:
                    status.close()

    def test_rank_correction_rejects_the_invented_motive(self):
        original = "1600 分妥妥的高手分段啦！"
        self.store.record_bot_reply(daily.ROOM, original)
        current = self.message("全球前十万就是高手是吧", sequence=7,
                               data={"quote": {"fromusr": daily.ROBOT, "ref_type": "1", "text": original}})
        self.store.record(current)
        self.backend.send_at_text = lambda room, text, ids: self.backend.send_text(room, text)
        plugin = MentionPlugin()
        plugin.members, plugin.room_state = self.store, self.store.room_state
        excuse = "被你抓包了，我刚才是想给你捧场。"
        correction = "我刚才说得太满了，没核实榜单，光凭 1600 分我判断不了水平。"
        provider = self.ctx._llm.complete_text = Mock(side_effect=[excuse, correction])
        self.ctx._jev = Mock()

        def judge(state, questions, **kw):
            if "拟发送的回答" in state:
                self.assertEqual(len(questions), 7)
                self.assertEqual(state["依据"]["本轮确认事实"]["机器人原话"][0]["原话"], original)
                return [0.02, 0.02, 0.02, 0.4, 0.99 if state["拟发送的回答"] == excuse else 0.01, 0.01, 0.01]
            return [0.99 if q == CORRECTION_INTENT else 0.01 for q in questions]

        self.ctx._jev.noul.side_effect = judge
        plugin.on_message(current, self.ctx)
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(len(self.backend.sent), 1)
        self.assertTrue(self.backend.sent[0][1].endswith(correction))


class ReplyBudgetTests(unittest.TestCase):
    def test_deadline_draft_checks_pending_additions_and_revision(self):
        budget = ReplyBudget(time.monotonic())
        pending = threading.Event()
        draft = ReplyDraft(budget.revision, "当前草稿", pending.is_set)
        budget.ready = draft
        self.assertTrue(budget.draft_is_current(draft))
        pending.set()
        self.assertFalse(budget.draft_is_current(draft))
        pending.clear()
        budget.revise()
        self.assertIsNone(budget.ready)
        self.assertFalse(budget.draft_is_current(draft))
        with self.assertRaises(ReplyExpired), budget.delivery(draft.revision):
            pass

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
