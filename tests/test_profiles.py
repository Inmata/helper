"""档案的真实查询、确认入口、来源与发送行为；外部模型用确定输出替身。"""
import json
import threading
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace

import test_chat_summary as daily
from plugins.knowledge import Plugin
from plugins.mention_chat import Plugin as MentionPlugin
from plugins.proactive_chat import Plugin as ProactivePlugin
from wxbot_framework.app.ingress import MessageIngress
from wxbot_framework.conversation.profiles import profile_messages, write_profile
from wxbot_framework.conversation.members import MemberError
from wxbot_framework.integrations.llm import LLMError


class FixtureClient:
    def __init__(self):
        self.calls = []
        self.candidates = []
        self.before_reply = lambda: None

    def complete_json(self, system, user, **options):
        data = json.loads(user.split("\n\n【本轮角色表达】")[0])
        self.calls.append((system, data))
        self.before_reply()
        if "candidates" in data:
            return {"candidate_ids": self.candidates}
        return {"overview": "这位群友平时说话简短，爱聊吉他。",
                "sections": [{"title": "那把一直惦记的吉他", "body": "群里的吉他手，常聊练琴的经历。",
                              "source_ids": [data["memories"][0]["id"]]}]}


class ProfileTests(unittest.TestCase):
    new_store = daily.DailySummaryTests.new_store
    message = daily.DailySummaryTests.message
    model = daily.DailySummaryTests.model

    def setUp(self):
        daily.DailySummaryTests.setUp(self)
        self.client = FixtureClient()
        self.ctx._llm = self.client
        self.plugin = Plugin()
        self.plugin.on_load(self.ctx)
        self.person = self.knowledge.entity(daily.ROOM, "陈俊山", aliases=["陈哥"], pinned=True)
        self.knowledge.add_memory(daily.ROOM, self.person["id"], "吉他", "本人陈述", "群里的吉他手", daily.CAT)
        self.client.candidates = [self.person["id"]]

    def tearDown(self):
        self.plugin.on_unload(self.ctx)
        daily.DailySummaryTests.tearDown(self)

    def drain(self):
        deadline = time.monotonic() + 3
        while self.plugin.has_pending_callbacks() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertFalse(self.plugin.has_pending_callbacks())

    def send(self, text, **options):
        msg = self.message(text, **options)
        self.store.record(msg)
        msg = self.store.profile_queries.prepare(msg)
        self.plugin.on_message(msg, self.ctx)
        self.drain()
        return msg

    def text(self):
        return "\n".join(text for _, text in self.backend.sent)

    def test_exact_name_generates_overview_and_distinct_sections(self):
        self.send("/档案 陈哥", sender=daily.OTHER)
        self.assertEqual(len(self.client.calls), 2)
        self.assertIn("人物概览", self.text())
        self.assertIn("1. 那把一直惦记的吉他", self.text())
        self.assertNotIn("已绑定", self.text())
        self.assertNotIn("source_ids", self.text())
        self.assertTrue(all(len(text.encode("utf-8")) <= 1023 for _, text in self.backend.sent))

    def test_unknown_nickname_confirms_then_queries_without_changing_identity(self):
        before = self.knowledge.profile_candidates(daily.ROOM)
        self.send("/档案 陈大山")
        self.assertIn("你说的是陈俊山吗", self.text())
        self.assertEqual(len(self.client.calls), 1)
        accepted = self.send("对")
        self.assertEqual(accepted.data["profile_confirmation"]["entity_id"], self.person["id"])
        self.assertIn("人物概览", self.text())
        self.assertEqual(self.knowledge.profile_candidates(daily.ROOM), before)

    def test_other_sender_and_room_cannot_confirm(self):
        self.send("/档案 陈大山")
        self.assertNotIn("profile_confirmation", self.send("对", sender=daily.OTHER).data)
        self.assertNotIn("profile_confirmation", self.send("对", room=daily.OTHER_ROOM).data)
        self.assertEqual(len(self.client.calls), 1)
        self.send("是的")
        self.assertEqual(len(self.client.calls), 3)

    def test_negative_expired_reset_and_topic_change_do_not_query(self):
        for action in ("negative", "expired", "reset", "topic", "session", "personality"):
            with self.subTest(action=action):
                self.send("/档案 陈大山")
                count = len(self.client.calls)
                if action == "negative":
                    self.send("不是")
                elif action == "expired":
                    clock = self.store.profile_queries.clock
                    self.store.profile_queries.clock = lambda: clock() + 301
                    self.send("对")
                    self.store.profile_queries.clock = clock
                elif action == "reset":
                    self.store.reset_context(daily.ROOM)
                    self.send("对")
                elif action == "session":
                    msg = replace(self.message("对"), session_id="another")
                    self.assertNotIn("profile_confirmation", self.store.profile_queries.prepare(msg).data)
                    self.store.profile_queries.clear()
                elif action == "personality":
                    self.store.set_personality(daily.ROOM, "俏皮")
                    self.send("对")
                    self.store.set_personality(daily.ROOM, "损友")
                else:
                    self.send("今晚吃什么")
                    self.send("对")
                self.assertEqual(len(self.client.calls), count)

    def test_multiple_candidates_select_by_number(self):
        second = self.knowledge.entity(daily.ROOM, "陈小山", pinned=True)
        self.client.candidates.append(second["id"])
        self.send("/档案 小山")
        self.assertIn("2. 陈小山", self.text())
        self.send("2")
        self.assertIn("陈小山", self.backend.sent[-1][1])
        self.assertIn("没有足够资料", self.backend.sent[-1][1])

    def test_empty_or_foreign_candidate_never_creates_person(self):
        before = self.knowledge.profile_candidates(daily.ROOM)
        self.client.candidates = []
        self.send("/档案 完全未知")
        self.assertIn("补一个名字", self.text())
        foreign = self.knowledge.entity(daily.OTHER_ROOM, "外群人物", pinned=True)
        self.client.candidates = [foreign["id"]]
        self.send("/档案 完全未知")
        self.assertIn("没整理好", self.backend.sent[-1][1])
        self.assertEqual(self.knowledge.profile_candidates(daily.ROOM), before)

    def test_profile_reads_more_than_eight_items_and_preserves_source_types(self):
        for index in range(12):
            self.knowledge.add_memory(daily.ROOM, self.person["id"], f"故事{index}", "群内玩笑",
                                      f"群友用第{index}件事调侃他", daily.CAT)
        material = self.knowledge.profile_material(daily.ROOM, self.person["id"])
        self.assertEqual(len(material["memories"]), 13)
        self.assertTrue(any(item["kind"] == "群内玩笑" for item in material["memories"]))
        self.assertIn("first_source_at", material["memories"][0])
        self.knowledge.quarantine_person(daily.ROOM, self.person["id"], daily.CAT)
        self.client.calls.clear()
        with self.assertRaises(MemberError):
            self.knowledge.profile_material(daily.ROOM, self.person["id"])

    def test_bad_model_sources_and_fields_are_rejected(self):
        material = self.knowledge.profile_material(daily.ROOM, self.person["id"])
        for response in ({"overview": "简介", "sections": [{"title": "标题", "body": "正文", "source_ids": [999999]}]},
                         {"overview": "", "sections": []}, {"overview": "简介", "sections": ["正文"]}):
            with self.subTest(response=response), self.assertRaises(LLMError):
                write_profile(SimpleNamespace(complete_json=lambda *a, **k: response), material)

    def test_wechat_splitting_keeps_heading_with_body(self):
        blocks = ["人物概览\n" + "内容" * 145, "1. 练琴的经历\n" + "故事" * 110,
                  "2. 一段很长的经历\n" + "长故事" * 300]
        parts = list(profile_messages(blocks))
        self.assertTrue(all(len(part.encode("utf-8")) <= 1023 for part in parts))
        self.assertEqual(parts[0], blocks[0])
        self.assertEqual(parts[1], blocks[1])
        self.assertIn("长故事", parts[2])
        self.assertEqual("".join(parts[2:]), blocks[2])

    def test_only_source_reviewed_draft_is_delivered(self):
        material = self.knowledge.profile_material(daily.ROOM, self.person["id"])
        drafts = iter([{"overview": "未经核对的初稿", "sections": []},
                       {"overview": "核对来源后的概览", "sections": []}])
        calls = []

        def complete(system, user, **options):
            calls.append(json.loads(user))
            return next(drafts)

        blocks = write_profile(SimpleNamespace(complete_json=complete), material)
        self.assertIn("核对来源后的概览", blocks[0])
        self.assertNotIn("未经核对的初稿", blocks[0])
        self.assertEqual(calls[1]["draft"]["overview"], "未经核对的初稿")
        self.assertEqual(calls[1]["memories"], material["memories"])

    def test_model_wait_releases_database_and_reset_cancels_reply(self):
        started, release = threading.Event(), threading.Event()
        self.client.before_reply = lambda: (started.set(), release.wait(3))
        msg = self.message("/档案 陈哥")
        self.store.record(msg)
        self.plugin.on_message(msg, self.ctx)
        self.assertTrue(started.wait(1))
        try:
            with self.store.lock:
                self.store.reset_context(daily.ROOM)
        finally:
            release.set()
        self.drain()
        self.assertEqual(self.backend.sent, [])

    def test_ingress_marks_confirmation_before_any_plugin_and_chat_skips_it(self):
        self.send("/档案 陈大山")
        captured = []
        plugins = SimpleNamespace(capture=lambda msg: msg, is_active=lambda name: False,
                                  dispatch=lambda msg: captured.append(msg))
        app = SimpleNamespace(config={}, members=self.store, plugins=plugins)
        MessageIngress(app).submit(self.message("对"))
        msg = captured[0]
        self.assertIn("profile_confirmation", msg.data)
        mention = MentionPlugin()
        mention.members = self.store
        mention.on_message(msg, self.ctx)
        proactive = ProactivePlugin()
        proactive.active = True
        proactive.members = self.store
        proactive.options = {"rooms": []}
        proactive.on_message(msg, self.ctx)
        self.plugin.on_message(msg, self.ctx)
        self.drain()
        self.assertIn("人物概览", self.text())


if __name__ == "__main__":
    unittest.main()
