"""群人格选择、真实艾特、上下文及并发取消的验证。"""
import json
import sys
import threading
import unittest
from datetime import timedelta
from unittest.mock import Mock, patch

import test_chat_summary as daily
from plugins.mention_chat import Plugin as ChatPlugin
from plugins.mention_chat.slow_replies import SlowReplies
from plugins.mention_chat.voice_reply import READ_PAUSE_FEATURE, READ_REVIEW_THRESHOLD, REPLY_FEATURE
from plugins.personality import Plugin as PersonalityPlugin
from wxbot_framework.conversation.members import MemberError, PersonaSuperseded
from wxbot_framework.conversation.mention_tools import ToolOutcome, ToolResult
from wxbot_framework.integrations.jev import JevError
from wxbot_framework.messaging.mentions import (at_source, learned_aliases, parse_at_members, parse_quote,
                                                reset_learned_aliases, strip_at_labels, typed_at)
from wxbot_framework.conversation.dialogue import SYSTEM as CHAT_SYSTEM
from wxbot_framework.conversation.personality import (FIXED_NAME, FIXED_PROMPT, PLAYFUL_NAME, PLAYFUL_PROMPT,
                                                      CHAT_STYLE_PROMPT, REPLY_TAIL, VOICE_FEATURE, PersonalityExpired, PersonalityState,
                                                      prepare_switch, game_reply)
from wxbot_framework.app.runtime import BotApplication
from wxbot_framework.commands import COMMANDS, help_text
from wxbot_framework.sdk import Message
from wxbot_framework.messaging.message import VoiceSubmission

ROOM, CAT, ROBOT, OTHER = daily.ROOM, daily.CAT, daily.ROBOT, daily.OTHER


class FixedPersonalityTests(unittest.TestCase):
    setUp = daily.DailySummaryTests.setUp
    tearDown = daily.DailySummaryTests.tearDown
    new_store = daily.DailySummaryTests.new_store
    message = daily.DailySummaryTests.message
    model = daily.DailySummaryTests.model
    snapshot = daily.DailySummaryTests.snapshot

    def chat(self, text="你怎么看？", *, sequence=100, sender=CAT, at_members=(ROBOT,), **kwargs):
        msg = self.message("@机器人\u2005" + text, sequence=sequence, sender=sender,
                           data={"at_members": at_members, "is_at_self": True, **kwargs})
        self.store.record(msg)
        return msg

    def setup_chat(self, provider=None):
        self.backend.send_at_text = lambda room, text, ids: self.backend.sent.append((room, text, ids)) or True
        self.ctx._llm = Mock(complete_text=provider or Mock(return_value="这是自然具体的回答。"))
        plugin = ChatPlugin()
        plugin.on_load(self.ctx)
        plugin._slow.close()
        plugin._slow = None
        self.addCleanup(plugin.on_unload, self.ctx)
        return plugin

    def test_personality_commands_view_and_switch_with_admin_permission(self):
        plugin = PersonalityPlugin()
        plugin.on_load(self.ctx)
        initial = self.store.personality(ROOM)
        for command in ("/人格", "/切换人格"):
            plugin.on_message(prepare_switch(self.message(command, sender=OTHER), self.store), self.ctx)
            self.assertIn("当前人格：熟人损友", self.backend.sent[-1][1])
            self.assertIn("俏皮", self.backend.sent[-1][1])
            self.assertEqual(self.store.personality(ROOM), initial)
        plugin.on_message(prepare_switch(self.message("/人格 俏皮", sender=OTHER), self.store), self.ctx)
        self.assertIn("只有管理员", self.backend.sent[-1][1])
        self.assertEqual(self.store.personality(ROOM), initial)
        for command, name in (("/人格 俏皮", PLAYFUL_NAME), ("/人格 损友", FIXED_NAME),
                              ("/切换人格 俏皮损友", PLAYFUL_NAME), ("/人格 熟人损友", FIXED_NAME)):
            plugin.on_message(prepare_switch(self.message("@机器人\u2005" + command), self.store), self.ctx)
            self.assertIn("已切换为" + name, self.backend.sent[-1][1])
            self.assertEqual(self.store.personality(ROOM).name, name)
        before = self.store.personality(ROOM)
        plugin.on_message(prepare_switch(self.message("/人格 嘴臭"), self.store), self.ctx)
        self.assertIn("可选人格", self.backend.sent[-1][1])
        self.assertEqual(self.store.personality(ROOM), before)
        self.assertIn("/人格", COMMANDS)
        self.assertIn("/人格：", help_text(False))
        self.assertNotIn("/人格 <", help_text(False))
        self.assertIn("/人格 <损友|俏皮>", help_text(True))

    def test_personality_command_scope_does_not_change_state(self):
        before = self.store.personality(ROOM)
        for message in (self.message("/人格 俏皮", is_self=True),
                        self.message("/人格 俏皮", data={"bot_generated": True}),
                        self.message("/人格 俏皮", data={"dispatch_to_plugins": False}),
                        self.message("/人格 俏皮", room="999@chatroom"),
                        self.message("/人格化 俏皮"), Message("/人格 俏皮", CAT, sender_id=CAT)):
            self.assertIs(prepare_switch(message, self.store), message)
        self.assertEqual(self.store.personality(ROOM), before)

    def test_personality_persists_per_room_and_account_and_keeps_supplements(self):
        self.store.save_persona(ROOM, {"text": "爱好：游戏"})
        self.store.set_personality(ROOM, "俏皮")
        saved = self.store.personality(ROOM)
        reopened = self.new_store()
        self.assertEqual(reopened.personality(ROOM), saved)
        self.assertEqual(reopened.personality(daily.OTHER_ROOM).name, FIXED_NAME)
        self.assertEqual(self.new_store(account="wxid_second").personality(ROOM).name, FIXED_NAME)
        self.store.save_persona(ROOM, {"text": "爱好：火锅"})
        self.assertEqual(self.store.personality(ROOM).name, PLAYFUL_NAME)
        self.store.rollback_persona(ROOM)
        self.assertEqual(self.store.personality(ROOM).supplement, "爱好：游戏")
        self.store.activate_persona(ROOM, 0)
        self.assertEqual(self.store.personality(ROOM).name, PLAYFUL_NAME)
        self.assertEqual(len(self.store.persona_versions(ROOM)), 2)
        with self.assertRaisesRegex(MemberError, "不在配置中"):
            self.store.set_personality("999@chatroom", "俏皮")
        with self.assertRaises(MemberError):
            self.store.set_personality(ROOM, "嘴臭")

    def test_legacy_personality_falls_back_to_original_style(self):
        with self.store.transaction() as db:
            db.execute("INSERT INTO room_personalities(account,room,name,generation,revision) VALUES(?,?,?,0,1)",
                       (ROBOT, ROOM, "嘴臭"))
        self.assertEqual(self.store.personality(ROOM).name, FIXED_NAME)

    def test_personality_round_trip_cancels_reply_and_old_derivation(self):
        before = self.store.personality(ROOM)
        self.store.set_personality(ROOM, "俏皮")
        self.store.set_personality(ROOM, "损友")
        self.assertFalse(self.store.personality_is_current(before))
        with self.assertRaises(PersonalityExpired), self.store.personality_delivery_guard(before):
            pass
        with self.assertRaises(PersonaSuperseded):
            self.store.save_persona(ROOM, {"text": "过时补充"}, expected_revision=before.revision)
        self.assertEqual(self.store.persona_versions(ROOM), [])

    def test_playful_personality_reaches_text_and_voice_generation(self):
        plugin, provider = self.setup_fixed_voice("【文字】再夸两句，我爱听。")
        self.store.set_personality(ROOM, "俏皮")
        self.store.record_bot_reply(ROOM, "你还好意思说，真笨。")
        plugin.on_message(self.chat("你真聪明，打字回我", sequence=100), self.ctx)
        self.assertTrue(provider.call_args.args[0].startswith(PLAYFUL_PROMPT))
        self.assertNotIn(CHAT_STYLE_PROMPT, provider.call_args.args[0])
        self.assertTrue(provider.call_args.args[1].endswith(REPLY_TAIL))
        self.assertIn("你还好意思说，真笨。", provider.call_args.args[1])
        self.assertNotIn("history", provider.call_args.kwargs)
        self.assertIn("再夸两句", "".join(row[1] for row in self.backend.sent))
        plugin.on_message(self.message("/语音回复 测试成员"), self.ctx)
        provider.return_value = "再夸两句，我爱听。"
        plugin.on_message(self.chat("你今天心情不错呀", sequence=101), self.ctx)
        self.assertTrue(provider.call_args.args[0].startswith(PLAYFUL_PROMPT))
        self.assertTrue(provider.call_args.args[1].endswith(REPLY_TAIL))
        plugin.voice_reply._local.synthesize.assert_called_once_with("星瞳", "再夸两句，我爱听。")

    def test_personality_switch_during_generation_discards_old_reply(self):
        def change(*args, **kwargs):
            prepare_switch(self.message("/人格 俏皮"), self.store)
            return "这条已经过时"
        plugin = self.setup_chat(change)
        plugin.on_message(self.chat(), self.ctx)
        self.assertEqual(self.backend.sent, [])

    def test_runtime_switches_personality_before_plugin_dispatch(self):
        app = object.__new__(BotApplication)
        app.config, app.members, app.plugins = {"chat_allowlist": []}, self.store, Mock()
        app.plugins.capture.side_effect = lambda message: message
        before = self.store.personality(ROOM)
        app._emit(self.message("/人格 俏皮"))
        state = app.plugins.dispatch.call_args.args[0].data["personality_switch"]
        self.assertEqual(state.name, PLAYFUL_NAME)
        self.assertFalse(self.store.personality_is_current(before))

    def test_fixed_style_wraps_function_constraints_without_truncation(self):
        provider = Mock(return_value={"reply": "回答"})
        self.ctx._llm = Mock(complete_json=provider)
        self.ctx.llm_for(self.message("问题")).complete_json("必须遵守JSON字段", "输入")
        system = provider.call_args.args[0]
        self.assertIn("熟人损友", system)
        self.assertIn("必须遵守JSON字段", system)
        self.assertNotIn("每次回复控制在 20", system)

    def test_voice_command_updates_self_name_only_for_admin(self):
        provider = Mock(return_value="我在呢。")
        plugin = self.setup_chat(provider)
        self.ctx._room_state = self.store.room_state
        self.store.owner_wxid = CAT
        plugin.voice_reply._voices = ("星瞳", "炫神")
        plugin.on_message(self.message("/音色 炫神", sender=OTHER), self.ctx)
        self.assertIn("只有管理员", self.backend.sent[-1][1])
        plugin.on_message(self.message("/音色", sender=OTHER), self.ctx)
        self.assertIn("星瞳，自称A姐", self.backend.sent[-1][1])
        plugin.on_message(self.chat("你叫什么？", sequence=100), self.ctx)
        self.assertIn("自称「A姐」", provider.call_args.args[0])
        plugin.on_message(self.message("/音色 炫神", sender=CAT), self.ctx)
        plugin.on_message(self.chat("那你现在叫什么？", sequence=101), self.ctx)
        self.assertIn("自称「A哥」", provider.call_args.args[0])
        self.assertEqual(self.store.personality(ROOM).voice, "炫神")

    def test_length_guidance_is_defined_once_and_deferred_by_personality(self):
        for old in ("120—220", "50—120"):
            self.assertNotIn(old, CHAT_SYSTEM)
            self.assertNotIn(old, FIXED_PROMPT)
        self.assertIn("用户明确要求详细", CHAT_SYSTEM)
        self.assertIn("分多段", CHAT_SYSTEM)
        self.assertIn("篇幅按功能要求决定", FIXED_PROMPT)

    def test_sensitive_event_and_repeated_joke_rules_are_defined_once(self):
        for phrase in ("日常拌嘴默认按玩笑接", "确认真实后后续回复也不再拿它玩梗",
                       "自己近期回复里用过的梗和开头词不再重复", "对方明确表示不舒服或要求停止时收住",
                       "政治与时事争议不下确定结论", "不用加粗等Markdown标记"):
            with self.subTest(phrase=phrase):
                self.assertEqual(FIXED_PROMPT.count(phrase), 1)
                self.assertNotIn(phrase, CHAT_SYSTEM)
        # 原有“不拿真实创伤攻击”被扩展而不是另起一条。
        self.assertEqual(FIXED_PROMPT.count("现实创伤"), 1)
        self.assertEqual(FIXED_PROMPT.count("机械复读梗"), 1)
        self.assertEqual(FIXED_PROMPT.count("Markdown"), 1)

    def test_voice_banter_uses_relaxed_prompt_and_keeps_original_words(self):
        provider = Mock()
        plugin = self.setup_chat(provider)
        self.ctx.capabilities = frozenset({"voice.send"})
        self.backend.send_voice = Mock()
        plugin.voice_reply._voices = ("星瞳",)
        plugin.voice_reply._local = Mock()
        plugin.voice_reply._local.synthesize.return_value = b"test-audio"
        self.store.record_bot_reply(ROOM, "骂人的话一句都不带念的哈")
        for sequence, original in enumerate(("你这个扎啤，真是笑死我了。", "卧槽，这波操作真牛逼。"), 100):
            with self.subTest(original=original):
                self.backend.send_voice.return_value = VoiceSubmission(f"test-voice-{sequence}")
                provider.reset_mock()
                provider.return_value = "【语音】" + original
                question = "用语音念：" + original
                message = self.chat(question, sequence=sequence)
                for _ in plugin._reply_once(message, self.ctx, question, None):
                    pass
                self.assertEqual(provider.call_count, 1)
                system = provider.call_args.args[0]
                self.assertIn("日常拌嘴默认按玩笑接", system)
                self.assertIn("轻度互损、粗口、谐音梗和略低俗的玩笑可以照念", system)
                self.assertIn("历史里拒读过别的句子也不延续成一律禁念的规则", system)
                self.assertIn("对方明确表示不舒服或要求停止时收住", system)
                plugin.voice_reply._local.synthesize.assert_called_with("星瞳", original)
                self.backend.send_voice.assert_called_with(ROOM, b"test-audio")
                saved = self.store.recent_rows(ROOM, limit=1, include_bot=True)[0]
                self.assertEqual((saved["text"], saved["context_kind"]), (original, "voice"))
        self.assertEqual(self.backend.sent, [])

    def setup_fixed_voice(self, reply="普通回答。"):
        provider = Mock(return_value=reply)
        self.ctx._room_state = self.store.room_state
        plugin = self.setup_chat(provider)
        self.ctx.capabilities = frozenset({"voice.send"})
        self.backend.send_voice = Mock(side_effect=lambda *args: VoiceSubmission(
            f"voice-{self.backend.send_voice.call_count}"))
        plugin.voice_reply._voices = ("星瞳",)
        plugin.voice_reply._local = Mock()
        plugin.voice_reply._local.synthesize.return_value = b"audio"
        return plugin, provider

    def test_automatic_voice_accepts_eighty_characters_and_resumes_after_thirty_seconds(self):
        text = "今天真热闹，" * 13 + "哈。"
        self.assertEqual(len(text), 80)
        plugin, provider = self.setup_fixed_voice(text)
        with patch("plugins.mention_chat.voice_reply.random", return_value=0):
            for sequence, now, length, voice_count in ((100, 1000, 80, 1), (101, 1029, 80, 1),
                                                      (102, 1030, 80, 2), (103, 1060, 81, 2)):
                with self.subTest(now=now, length=length):
                    provider.return_value = text + ("哈" if length == 81 else "")
                    with patch("plugins.mention_chat.voice_reply.monotonic", return_value=now):
                        plugin.on_message(self.chat("今晚真热闹", sequence=sequence), self.ctx)
                    self.assertEqual(self.backend.send_voice.call_count, voice_count)
        plugin.voice_reply._local.synthesize.assert_called_with("星瞳", text)
        self.assertEqual([item[1] for item in self.backend.sent],
                         ["@测试成员\u2005\n" + text, "@测试成员\u2005\n" + text + "哈"])

    def test_read_voice_command_preserves_literal_text_and_reviews_after_sending(self):
        plugin, provider = self.setup_fixed_voice()
        provider.side_effect = AssertionError("原文朗读不应调用聊天模型")
        self.ctx._jev = Mock()
        self.ctx._jev.noul.return_value = [0.01]
        self.ctx._magpie = Mock()
        plugin.voice_reply._voices = ("星瞳", "炫神")
        plugin.on_message(self.message("/音色 炫神"), self.ctx)
        self.backend.sent.clear()
        plugin.voice_reply._next[ROOM] = float("inf")
        original = "【文字】卧槽，这波操作真牛逼。/音色 @小明，照原话念！"
        for sequence, prefix in enumerate(("/语音 ", "@机器人\u2005/语音 "), 100):
            plugin.on_message(self.message(prefix + original, sender=OTHER, sequence=sequence), self.ctx)
            plugin.voice_reply._local.synthesize.assert_called_with("炫神", original)
        self.assertEqual(self.backend.send_voice.call_count, 2)
        self.assertEqual(self.backend.sent, [])
        self.assertIsNone(self.store.room_state.get(ROOM, REPLY_FEATURE))
        saved = self.store.recent_rows(ROOM, limit=1, include_bot=True)[0]
        self.assertEqual((saved["text"], saved["context_kind"]), (original, "voice"))
        provider.assert_not_called()
        self.assertEqual(self.ctx._jev.noul.call_count, 2)
        self.assertEqual(self.ctx._magpie.mock_calls, [])

    def test_read_voice_review_waits_for_all_parts_then_pauses_group_and_exempts_admin(self):
        plugin, provider = self.setup_fixed_voice()
        self.ctx._jev = Mock()
        text = "甲" * 80 + "乙" * 80 + "丙" * 5

        def judge(state, questions, **options):
            self.assertEqual(self.backend.send_voice.call_count, 3)
            self.assertEqual(state["text"].replace(" ", ""), text)
            self.assertEqual(options["timeout"], 10)
            self.assertIsNone(self.store.room_state.get(ROOM, READ_PAUSE_FEATURE))
            return [READ_REVIEW_THRESHOLD]

        self.ctx._jev.noul.side_effect = judge
        with patch("plugins.mention_chat.voice_reply.time", return_value=1000):
            plugin.on_message(self.message("/语音 " + text, sender=OTHER), self.ctx)
            self.assertEqual(self.store.room_state.get(ROOM, READ_PAUSE_FEATURE), {"until": 4600})
            self.assertIn("暂停一小时", self.backend.sent[-1][1])
            for sender in (OTHER, "wxid_another"):
                plugin.on_message(self.message("/语音 还想念", sender=sender), self.ctx)
                self.assertIn("暂停中", self.backend.sent[-1][1])
            self.assertEqual(self.backend.send_voice.call_count, 3)
            plugin.on_message(self.message("/语音 管理员照常使用", sender=CAT), self.ctx)
            self.assertEqual(self.backend.send_voice.call_count, 4)
            self.ctx._jev.noul.assert_called_once()
            self.assertEqual(self.store.room_state.get(ROOM, READ_PAUSE_FEATURE), {"until": 4600})
        provider.assert_not_called()

    def test_read_voice_pause_survives_reopening_is_room_scoped_and_expires(self):
        plugin, _ = self.setup_fixed_voice()
        self.ctx._jev = Mock()
        self.ctx._jev.noul.return_value = [0.01]
        self.store.room_state.put(ROOM, READ_PAUSE_FEATURE, {"until": 4600})
        reopened = self.new_store()
        self.ctx._room_state = reopened.room_state
        with patch("plugins.mention_chat.voice_reply.time", return_value=4599):
            plugin.on_message(self.message("/语音 被暂停", sender=OTHER), self.ctx)
            self.backend.send_voice.assert_not_called()
            self.assertIn("1 分钟", self.backend.sent[-1][1])
            plugin.on_message(self.message("/语音 其他群照常", sender=OTHER, room=daily.OTHER_ROOM), self.ctx)
            self.backend.send_voice.assert_called_once_with(daily.OTHER_ROOM, b"audio")
        with patch("plugins.mention_chat.voice_reply.time", return_value=4600):
            plugin.on_message(self.message("/语音 到期恢复", sender=OTHER), self.ctx)
            self.assertEqual(self.backend.send_voice.call_count, 2)
        self.assertIsNone(self.new_store(account="wxid_second").room_state.get(ROOM, READ_PAUSE_FEATURE))

    def test_admin_read_voice_does_not_trigger_review_or_group_pause(self):
        plugin, _ = self.setup_fixed_voice()
        self.ctx._jev = Mock()
        self.ctx._jev.noul.return_value = [1.0]
        plugin.on_message(self.message("/语音 管理员原文", sender=CAT), self.ctx)
        self.backend.send_voice.assert_called_once()
        self.ctx._jev.noul.assert_not_called()
        self.assertIsNone(self.store.room_state.get(ROOM, READ_PAUSE_FEATURE))

    def test_read_voice_failed_or_unknown_first_send_is_not_reviewed(self):
        plugin, _ = self.setup_fixed_voice()
        self.ctx._jev = Mock()
        plugin.voice_reply._local.synthesize.return_value = None
        plugin.on_message(self.message("/语音 合成失败", sender=OTHER), self.ctx)
        self.backend.send_voice.assert_not_called()
        self.ctx._jev.noul.assert_not_called()
        plugin.voice_reply._local.synthesize.return_value = b"audio"
        self.backend.send_voice.side_effect = None
        self.backend.send_voice.return_value = False
        plugin.on_message(self.message("/语音 回执不明", sender=OTHER), self.ctx)
        self.ctx._jev.noul.assert_not_called()

    def test_read_voice_partial_delivery_reviews_only_confirmed_voice(self):
        plugin, _ = self.setup_fixed_voice()
        self.ctx._jev = Mock()
        self.ctx._jev.noul.return_value = [0.01]
        self.backend.send_voice.side_effect = [VoiceSubmission("first"), False]
        plugin.on_message(self.message("/语音 " + "甲" * 80 + "乙" * 160, sender=OTHER), self.ctx)
        self.assertEqual(self.ctx._jev.noul.call_args.args[0], {"text": "甲" * 80})
        self.assertEqual(self.backend.send_voice.call_count, 2)
        self.assertEqual(self.backend.sent, [])

    def test_read_voice_review_failure_or_uncertain_score_keeps_command_available(self):
        plugin, _ = self.setup_fixed_voice()
        self.ctx._jev = Mock()
        self.ctx._jev.noul.side_effect = [JevError("超时"), [READ_REVIEW_THRESHOLD - 0.01], [0.01]]
        for text in ("判断失败", "边界不清楚", "继续正常使用"):
            plugin.on_message(self.message("/语音 " + text, sender=OTHER), self.ctx)
            self.assertIsNone(self.store.room_state.get(ROOM, READ_PAUSE_FEATURE))
        self.assertEqual(self.backend.send_voice.call_count, 3)

    def test_read_voice_group_pause_cancels_voice_already_synthesizing(self):
        plugin, _ = self.setup_fixed_voice()
        self.ctx._jev = Mock()

        def synthesize(*args):
            self.store.room_state.put(ROOM, READ_PAUSE_FEATURE, {"until": 4600})
            return b"audio"

        plugin.voice_reply._local.synthesize.side_effect = synthesize
        with patch("plugins.mention_chat.voice_reply.time", return_value=1000):
            plugin.on_message(self.message("/语音 排队期间被暂停", sender=OTHER), self.ctx)
        self.backend.send_voice.assert_not_called()
        self.ctx._jev.noul.assert_not_called()

    def test_read_voice_pause_leaves_ordinary_voice_replies_available(self):
        plugin, provider = self.setup_fixed_voice("【语音】晚安。")
        self.ctx._jev = Mock()
        self.ctx._jev.noul.return_value = [0.01]
        self.store.room_state.put(ROOM, READ_PAUSE_FEATURE, {"until": 4600})
        with patch("plugins.mention_chat.voice_reply.time", return_value=1000):
            plugin.on_message(self.chat("用语音念句晚安", sender=OTHER), self.ctx)
        self.backend.send_voice.assert_called_once()
        self.assertEqual(self.store.room_state.get(ROOM, READ_PAUSE_FEATURE), {"until": 4600})
        provider.assert_called_once()
        for call in self.ctx._jev.noul.call_args_list:
            self.assertNotIn("text", call.args[0])

    def test_read_voice_command_reads_long_multiline_text_in_order(self):
        plugin, provider = self.setup_fixed_voice()
        text = "甲" * 80 + "\n" + "乙" * 80 + "丙" * 5
        plugin.on_message(self.message("/语音\n" + text, sender=OTHER), self.ctx)
        spoken = [call.args[1] for call in plugin.voice_reply._local.synthesize.call_args_list]
        self.assertEqual(spoken, ["甲" * 80, "乙" * 80, "丙" * 5])
        self.assertEqual(self.backend.send_voice.call_count, 3)
        provider.assert_not_called()

    def test_read_voice_command_usage_capability_and_message_scope(self):
        plugin, provider = self.setup_fixed_voice()
        for text in ("/语音", "/语音   \n"):
            plugin.on_message(self.message(text, sender=OTHER), self.ctx)
            self.assertIn("用法：/语音", self.backend.sent[-1][1])
        self.assertIn("/语音", COMMANDS)
        self.assertIn("/语音 原文", help_text(False))
        before = len(self.backend.sent)
        for message in (self.message("/语音测试 原文", sender=OTHER),
                        self.message("/语音 原文", is_self=True),
                        self.message("/语音 原文", room="999@chatroom"),
                        self.message("/语音 原文", data={"bot_generated": True}),
                        Message("/语音 原文", OTHER, sender_id=OTHER)):
            plugin.on_message(message, self.ctx)
        self.assertEqual(len(self.backend.sent), before)
        self.ctx.capabilities = frozenset()
        plugin.on_message(self.message("/语音 原文", sender=OTHER), self.ctx)
        self.assertIn("不支持发送语音", self.backend.sent[-1][1])
        plugin.voice_reply._local.synthesize.assert_not_called()
        provider.assert_not_called()

    def test_read_voice_command_failure_returns_only_unsent_text_with_byte_splitting(self):
        plugin, provider = self.setup_fixed_voice()
        plugin.voice_reply._local.synthesize.side_effect = [b"audio", OSError("合成失败")]
        plugin.on_message(self.message("/语音 " + "甲" * 80 + "乙" * 400, sender=OTHER), self.ctx)
        self.backend.send_voice.assert_called_once_with(ROOM, b"audio")
        texts = [sent[1] for sent in self.backend.sent]
        self.assertGreater(len(texts), 1)
        self.assertTrue(all(len(text.encode("utf-8")) <= 1023 for text in texts))
        self.assertEqual("".join(texts).replace(" ", ""), "乙" * 400)
        provider.assert_not_called()

    def test_read_voice_command_unknown_delivery_stops_without_resend(self):
        plugin, provider = self.setup_fixed_voice()
        self.backend.send_voice.side_effect = [VoiceSubmission("first"), False]
        plugin.on_message(self.message("/语音 " + "甲" * 240, sender=OTHER), self.ctx)
        self.assertEqual(self.backend.send_voice.call_count, 2)
        self.assertEqual(self.backend.sent, [])
        provider.assert_not_called()

    def test_read_voice_command_cancels_when_voice_changes_during_synthesis(self):
        plugin, provider = self.setup_fixed_voice()

        def synthesize(*args):
            self.store.room_state.put(ROOM, VOICE_FEATURE, {"voice": "炫神"})
            return b"audio"

        plugin.voice_reply._local.synthesize.side_effect = synthesize
        plugin.on_message(self.message("/语音 原文", sender=OTHER), self.ctx)
        self.backend.send_voice.assert_not_called()
        self.assertEqual(self.backend.sent, [])
        provider.assert_not_called()

    def test_read_voice_command_uses_member_queue_without_blocking_other_members(self):
        plugin, provider = self.setup_fixed_voice("普通文字。")
        plugin._slow = SlowReplies(self.ctx.logger)
        started, release, fast, finished = (threading.Event() for _ in range(4))
        spoken = []

        def synthesize(voice, text):
            spoken.append(text)
            if text == "第一条":
                started.set()
                release.wait(5)
            return b"audio"

        def send_voice(*args):
            if len(spoken) == 2:
                finished.set()
            return VoiceSubmission(f"voice-{len(spoken)}")

        send_text = self.backend.send_at_text

        def send_at(*args):
            result = send_text(*args)
            fast.set()
            return result

        plugin.voice_reply._local.synthesize.side_effect = synthesize
        self.backend.send_voice.side_effect = send_voice
        self.backend.send_at_text = send_at
        try:
            plugin.on_message(self.message("/语音 第一条", sender=OTHER), self.ctx)
            self.assertTrue(started.wait(2), "原文语音合成没有开始")
            plugin.on_message(self.message("/语音 第二条", sender=OTHER), self.ctx)
            plugin.on_message(self.chat("详细说说今天的安排"), self.ctx)
            self.assertTrue(fast.wait(2), "其他成员的文字回复被语音阻塞")
            self.assertEqual(spoken, ["第一条"])
            release.set()
            self.assertTrue(finished.wait(2), "同一成员的后续语音没有恢复")
            self.assertEqual(spoken, ["第一条", "第二条"])
            provider.assert_called_once()
        finally:
            release.set()
            plugin._slow.close()

    def test_fixed_voice_command_targets_permissions_restart_and_disable(self):
        plugin, provider = self.setup_fixed_voice()
        plugin.on_message(self.message("/语音回复 测试成员", sender=OTHER), self.ctx)
        self.assertIn("只有管理员", self.backend.sent[-1][1])
        self.assertIsNone(self.store.room_state.get(ROOM, REPLY_FEATURE))
        plugin.on_message(self.message("/语音回复 测试成员"), self.ctx)
        self.assertTrue(plugin.voice_reply.fixed(self.message(""), self.new_store().room_state))
        self.assertFalse(plugin.voice_reply.fixed(self.message("", room=daily.OTHER_ROOM), self.store.room_state))
        self.assertFalse(plugin.voice_reply.fixed(self.message("", sender=OTHER), self.store.room_state))
        self.assertFalse(plugin.voice_reply.fixed(self.message(""), self.new_store("wxid_second").room_state))
        self.store.remember_wechat_name(CAT, "新的微信昵称")
        plugin.on_message(self.message("/语音回复", sender=OTHER), self.ctx)
        self.assertIn("测试成员", self.backend.sent[-1][1])
        plugin.on_message(self.chat("在吗"), self.ctx)
        self.backend.send_voice.assert_called_once_with(ROOM, b"audio")
        provider.assert_called_once()
        plugin.on_message(self.message("/语音回复 新的微信昵称 关闭"), self.ctx)
        self.assertIsNone(self.store.room_state.get(ROOM, REPLY_FEATURE))
        plugin.on_message(self.chat("还在吗", sequence=101), self.ctx)
        self.assertEqual(self.backend.sent[-1][2], (CAT,))
        self.assertEqual(self.backend.send_voice.call_count, 1)

    def test_fixed_voice_resolves_real_mention_member_code_and_rejects_ambiguity(self):
        plugin, _ = self.setup_fixed_voice()
        self.store.sync_room_directory(ROOM, (CAT, OTHER), {CAT: "同名", OTHER: "同名"})
        plugin.on_message(self.message("/语音回复 同名"), self.ctx)
        self.assertIn("多位群成员", self.backend.sent[-1][1])
        plugin.on_message(self.message("/语音回复 @群友", data={"at_members": (CAT, OTHER)}), self.ctx)
        self.assertIn("一次只能", self.backend.sent[-1][1])
        self.assertIsNone(self.store.room_state.get(ROOM, REPLY_FEATURE))
        plugin.on_message(self.message("/语音回复 @同名\u2005", data={"at_members": (OTHER,)}), self.ctx)
        self.assertTrue(plugin.voice_reply.fixed(self.message("", sender=OTHER), self.store.room_state))
        code = next(item["code"] for item in self.store.room_member_directory(ROOM) if item["wxid"] == OTHER)
        plugin.on_message(self.message(f"/语音回复 {code} 关闭"), self.ctx)
        self.assertIsNone(self.store.room_state.get(ROOM, REPLY_FEATURE))

    def test_fixed_voice_bypasses_text_marker_and_automatic_voice_cooldown(self):
        plugin, provider = self.setup_fixed_voice("【文字】我在呢。")
        plugin.on_message(self.message("/语音回复 测试成员"), self.ctx)
        plugin.voice_reply._next[ROOM] = float("inf")
        for sequence in (100, 101):
            with patch("plugins.mention_chat.voice_reply.random", return_value=0.99), \
                    patch("plugins.mention_chat.voice_reply.monotonic", return_value=sequence * 10):
                plugin.on_message(self.chat("打字说你在吗", sequence=sequence), self.ctx)
        self.assertEqual(self.backend.send_voice.call_count, 2)
        self.assertIn("当前发言者已开启固定语音回复", provider.call_args.args[0])
        plugin.voice_reply._local.synthesize.assert_called_with("星瞳", "我在呢。")

    def test_fixed_voice_replies_in_text_within_ten_seconds_for_mentions_and_quotes(self):
        plugin, provider = self.setup_fixed_voice()
        plugin.on_message(self.message("/语音回复 测试成员"), self.ctx)
        for sequence, now, answer, quote in (
                (100, 1000, "先整理文件。", False),
                (101, 1005, "桌面可以保持原样。", False),
                (102, 1009.9, "那就换个项目。", True),
                (103, 1010, "做个素材搜索吧。", True)):
            provider.return_value = answer
            if quote:
                message = self.message("继续说说", sequence=sequence,
                    data={"quote": {"fromusr": ROBOT, "ref_type": "1", "text": "前一条回复"}})
                self.store.record(message)
            else:
                message = self.chat("帮我想想项目", sequence=sequence)
            with patch("plugins.mention_chat.voice_reply.monotonic", return_value=now):
                plugin.on_message(message, self.ctx)
        self.assertEqual(provider.call_count, 4)
        self.assertEqual(self.backend.send_voice.call_count, 2)
        self.assertEqual([call.args[1] for call in plugin.voice_reply._local.synthesize.call_args_list],
                         ["先整理文件。", "做个素材搜索吧。"])
        self.assertEqual([sent[1].split("\n", 1)[-1] for sent in self.backend.sent[1:]],
                         ["桌面可以保持原样。", "那就换个项目。"])
        with patch("plugins.mention_chat.voice_reply.monotonic", return_value=1011):
            plugin.on_message(self.message("/语音 按原文念", sequence=104), self.ctx)
        self.assertEqual(self.backend.send_voice.call_count, 3)

    def test_fixed_voice_interval_is_per_member_and_failed_synthesis_does_not_start_it(self):
        plugin, provider = self.setup_fixed_voice()
        self.store.room_state.put(ROOM, REPLY_FEATURE, {CAT: True, OTHER: True})
        self.store.room_state.put(daily.OTHER_ROOM, REPLY_FEATURE, {CAT: True})
        with patch("plugins.mention_chat.voice_reply.monotonic", return_value=1000):
            provider.return_value = "先吃饭吧。"
            plugin.on_message(self.chat("在吗", sequence=100), self.ctx)
            provider.return_value = "晚点聊游戏。"
            plugin.on_message(self.chat("在吗", sequence=101, sender=OTHER), self.ctx)
            provider.return_value = "这边讨论天气。"
            message = self.message("@机器人\u2005在吗", sequence=102, room=daily.OTHER_ROOM,
                data={"is_at_self": True, "at_members": (ROBOT,)})
            self.store.record(message)
            plugin.on_message(message, self.ctx)
        self.assertEqual(self.backend.send_voice.call_count, 3)
        with patch("plugins.mention_chat.voice_reply.monotonic", return_value=1010):
            plugin.voice_reply._local.synthesize.side_effect = OSError("合成失败")
            provider.return_value = "失败也先把文字发出。"
            plugin.on_message(self.chat("继续", sequence=103), self.ctx)
            with patch("plugins.mention_chat.voice_reply.LocalVoice") as local:
                local.return_value.synthesize.return_value = b"audio"
                provider.return_value = "这次可以正常说话。"
                plugin.on_message(self.chat("再试一次", sequence=104), self.ctx)
        self.assertEqual(self.backend.send_voice.call_count, 4)
        self.assertIn("失败也先把文字发出。", self.backend.sent[-1][1])

    def test_fixed_voice_reads_full_multiline_detail_in_chunks_and_records_context(self):
        text = "第一部分讲清楚事情的来龙去脉。" * 7 + "\n" + "第二部分补充具体做法。" * 5
        plugin, _ = self.setup_fixed_voice(text)
        plugin.on_message(self.message("/语音回复 测试成员"), self.ctx)
        plugin.on_message(self.chat("详细解释一下"), self.ctx)
        spoken = [call.args[1] for call in plugin.voice_reply._local.synthesize.call_args_list]
        self.assertGreater(len(spoken), 1)
        self.assertTrue(all(0 < len(part) <= 80 and "\n" not in part for part in spoken))
        self.assertEqual("".join(spoken).replace(" ", ""), text.replace("\n", ""))
        rows = self.store.recent_rows(ROOM, limit=20, include_bot=True, sender_id=ROBOT)
        voice_rows = [row for row in rows if row["context_kind"] == "voice"]
        self.assertEqual(len(voice_rows), len(spoken))

    def test_fixed_voice_covers_tools_clarification_missing_quote_and_catch_up(self):
        plugin, provider = self.setup_fixed_voice("看到一只猫。")
        provider.side_effect = ["看到一只猫。", "这次搜索页面没打开。", "请引用那张图片。",
                                "引用语音没拿到转写。", "刚才讨论了一只猫。"]
        plugin.on_message(self.message("/语音回复 测试成员"), self.ctx)
        outcomes = (ToolOutcome(results=[ToolResult("vision", True, "一只猫")]),
                    ToolOutcome(results=[ToolResult("search", False, note="搜索超时")]),
                    ToolOutcome(ask="请引用那张图片。"))
        for sequence, outcome in enumerate(outcomes, 100):
            def steps(*args, **kwargs):
                yield from ()
                return outcome
            with patch.object(plugin, "_run_tools", side_effect=steps), \
                    patch("plugins.mention_chat.voice_reply.monotonic", return_value=sequence * 10):
                plugin.on_message(self.chat("这个有什么含义", sequence=sequence), self.ctx)
        with patch("plugins.mention_chat.voice_reply.monotonic", return_value=1030):
            plugin.on_message(self.chat("这句什么意思", sequence=103,
                                        quote={"ref_type": "34", "svrid": "missing", "fromusr": OTHER}), self.ctx)
        with patch("plugins.mention_chat.voice_reply.monotonic", return_value=1040):
            plugin.on_message(self.chat("刚才最后定了啥", sequence=104), self.ctx)
        self.assertEqual(self.backend.send_voice.call_count, 5)
        spoken = [call.args[1] for call in plugin.voice_reply._local.synthesize.call_args_list]
        self.assertIn("请引用那张图片。", spoken)
        self.assertTrue(any("转写" in part for part in spoken))

    def test_fixed_voice_synthesis_failure_only_falls_back_unsent_remainder(self):
        text = "甲" * 80 + "乙" * 80 + "丙" * 10
        plugin, _ = self.setup_fixed_voice(text)
        plugin.on_message(self.message("/语音回复 测试成员"), self.ctx)
        plugin.voice_reply._local.synthesize.side_effect = [b"audio", OSError("合成失败")]
        plugin.on_message(self.chat("详细解释", sequence=100), self.ctx)
        self.backend.send_voice.assert_called_once()
        self.assertEqual(self.backend.sent[-1][1].split("\n", 1)[-1], "乙" * 80 + " " + "丙" * 10)

    def test_fixed_voice_unknown_delivery_stops_without_text_resend(self):
        plugin, _ = self.setup_fixed_voice("甲" * 160)
        plugin.on_message(self.message("/语音回复 测试成员"), self.ctx)
        self.backend.send_voice.side_effect = None
        self.backend.send_voice.return_value = False
        plugin.on_message(self.chat("详细解释", sequence=100), self.ctx)
        self.backend.send_voice.assert_called_once()
        self.assertEqual(len(self.backend.sent), 1)  # 只有命令回执。

    def test_fixed_voice_still_uses_delivery_guard_for_each_chunk(self):
        plugin, _ = self.setup_fixed_voice("甲" * 80 + "乙" * 80)
        plugin.on_message(self.message("/语音回复 测试成员"), self.ctx)
        def invalidate(voice, text):
            if text.startswith("乙"):
                self.store.reset_context(ROOM)
            return b"audio"
        plugin.voice_reply._local.synthesize.side_effect = invalidate
        plugin.on_message(self.chat("详细解释"), self.ctx)
        self.backend.send_voice.assert_called_once()

    def test_fixed_voice_keeps_cold_reply_frequency_and_reads_the_reply(self):
        plugin, _ = self.setup_fixed_voice("有话直说。")
        plugin.on_message(self.message("/语音回复 测试成员"), self.ctx)
        plugin.on_message(self.message("/冷处理 @测试成员 30m", data={"at_members": (CAT,)}), self.ctx)
        plugin.on_message(self.chat("在吗"), self.ctx)
        plugin.on_message(self.chat("还在吗", sequence=101), self.ctx)
        self.backend.send_voice.assert_called_once()
        plugin.voice_reply._local.synthesize.assert_called_once_with("星瞳", "有话直说。")

    def test_group_and_own_context_are_separate_and_keep_real_at(self):
        self.store.record(self.message("本人前文", sequence=1))
        self.store.record(self.message("别人聊吉他", sender=OTHER, sequence=2))
        provider = Mock(return_value="刚才大家聊吉他，你也想试试吗？")
        plugin = self.setup_chat(provider)
        plugin.on_message(self.chat("吉他适合新手吗？"), self.ctx)
        sent = provider.call_args.args[1]
        self.assertIn("【当前发言者近期发言】", sent)
        self.assertIn("本人前文", sent)
        self.assertIn("【群内近期发言】", sent)
        self.assertIn("别人聊吉他", sent)
        self.assertNotIn(CAT, sent)
        self.assertNotIn(OTHER, sent)
        self.assertEqual(self.backend.sent[-1][2], (CAT,))
        self.assertTrue(self.backend.sent[-1][1].startswith("@测试成员\u2005"))

    def test_recent_context_excludes_later_messages_commands_and_robot(self):
        for number in range(12):
            self.store.record(self.message(f"前文{number}", sequence=number + 1))
        self.store.record(self.message("/档案"))
        self.store.record(self.message("机器人输出", data={"bot_generated": True}))
        msg = self.chat()
        self.store.record(self.message("后来才收到", sequence=101))
        self.assertEqual([row["text"] for row in self.store.conversation_snapshot(msg).messages],
                         [f"前文{number}" for number in range(2, 12)])

    def test_robot_reply_is_saved_and_reused_as_assistant_context(self):
        provider = Mock(return_value="B 是这样来的。")
        plugin = self.setup_chat(provider)
        plugin.on_message(self.chat("A 是什么？", sequence=10), self.ctx)
        stored = self.store.recent_rows(ROOM, include_bot=True)
        self.assertIn("B 是这样来的。", [row["text"] for row in stored])
        plugin.on_message(self.chat("那你为什么觉得 B 是这样？", sequence=11), self.ctx)
        history = provider.call_args.kwargs["history"]
        self.assertEqual([row["role"] for row in history], ["user", "assistant"])
        self.assertIn("A 是什么", history[0]["content"])
        self.assertEqual(history[1]["content"], "[机器人发言] B 是这样来的。")

    def test_cross_day_does_not_cancel_reply(self):
        def change(*args, **kwargs):
            self.now += timedelta(seconds=1)
            return "跨天仍能继续这段对话。"
        plugin = self.setup_chat(change)
        plugin.on_message(self.chat(), self.ctx)
        self.assertIn("跨天仍能", self.backend.sent[-1][1])

    def test_context_reset_during_model_request_discards_result(self):
        def change(*args, **kwargs):
            self.store.reset_context(ROOM)
            return "这条已经过时"
        plugin = self.setup_chat(change)
        plugin.on_message(self.chat(), self.ctx)
        self.assertEqual(self.backend.sent, [])

    def test_mention_of_other_person_and_command_do_not_call_model(self):
        provider = Mock(return_value="回答")
        plugin = self.setup_chat(provider)
        for msg in (self.message("@别人 你怎么看？", data={"at_members": (OTHER,), "is_at_self": True}),
                    self.chat("/绑定 新名", sequence=101), self.chat(sequence=102, bot_generated=True),
                    self.message("@别人 只有文字"), Message("私聊", CAT)):
            plugin.on_message(msg, self.ctx)
        provider.assert_not_called()
        self.assertEqual(self.backend.sent, [])

    def test_typed_at_without_structural_mention_is_answered(self):
        # 手打或复制粘贴的 @名字 没有微信的结构化艾特，也要当作在叫机器人。
        provider = Mock(return_value="在呢")
        plugin = self.setup_chat(provider)
        plugin.aliases = ("有个ai",)
        message = self.message("@有个ai 是不是出 bug 了")
        self.store.record(message)
        plugin.on_message(message, self.ctx)
        provider.assert_called_once()
        self.assertTrue(self.backend.sent[-1][1].startswith("@"))

    def test_quote_of_bot_message_is_answered_with_quoted_text(self):
        provider = Mock(return_value="刚才说的是晚饭")
        plugin = self.setup_chat(provider)
        message = self.message("这个呢", data={"quote": {"name": "机器人", "text": "今晚吃啥",
                                                         "chatusr": ROBOT, "fromusr": ROOM}})
        self.store.record(message)
        plugin.on_message(message, self.ctx)
        provider.assert_called_once()
        self.assertIn("【引用】机器人：今晚吃啥", provider.call_args.args[1])

    def test_unbound_member_gets_real_id_and_binding_reminder_once(self):
        plugin = self.setup_chat()
        plugin.on_message(self.chat(sender="wxid_unbound", sender_wechat_name="路人甲"), self.ctx)
        self.assertEqual(self.backend.sent[-1][2], ("wxid_unbound",))
        self.assertTrue(self.backend.sent[-1][1].startswith("@路人甲\u2005"))
        self.assertNotIn("/绑定", self.backend.sent[-1][1])

    def test_bound_person_uses_wechat_name_for_at_and_person_name_for_llm_context(self):
        provider = Mock(return_value="你是示例人物乙（乙哥），长语音担当。")
        plugin = self.setup_chat(provider)
        kb = self.ctx.knowledge
        person = kb.entity(ROOM, "示例人物乙", aliases=["乙哥", "小乙"], pinned=True)
        kb.add_memory(ROOM, person["id"], "长语音担当", "群内玩笑", "乙哥的经典形象是超长语音。", CAT)
        self.store.record(self.message("测试发言", sender=OTHER, sequence=1,
                                       data={"sender_wechat_name": "示例微信昵称", "self_wxid": ROBOT}))
        kb.link(ROOM, OTHER, "示例人物乙", CAT)
        plugin.on_message(self.chat("我是谁", sender=OTHER, sequence=2, sender_wechat_name="示例微信昵称"), self.ctx)
        sent = provider.call_args.args[1]
        self.assertIn("【当前发言者】[P1] 示例人物乙", sent)
        self.assertIn("【当前发言者档案】已绑定", sent)
        self.assertIn("name=示例人物乙", sent)
        self.assertIn("乙哥", sent)
        self.assertIn("wechat_name=示例微信昵称", sent)
        self.assertIn("【背景资料】", sent)
        self.assertTrue(self.backend.sent[-1][1].startswith("@示例微信昵称\u2005\n"))

    def test_long_reply_respects_byte_limit_and_is_not_twenty_characters(self):
        plugin = self.setup_chat(Mock(return_value="中文🌸" * 190))
        plugin.on_message(self.chat(), self.ctx)
        self.assertGreater(len(self.backend.sent), 1)
        self.assertTrue(all(len(row[1].encode("utf-8")) <= 1023 for row in self.backend.sent))
        self.assertTrue(all(row[2] == (CAT,) for row in self.backend.sent))

    def test_long_reply_is_split_and_reassembled_without_loss(self):
        body = "中" * 1200 + "[TAIL]"
        provider = Mock(return_value=body)
        plugin = self.setup_chat(provider)
        plugin.on_message(self.chat(), self.ctx)
        provider.assert_called_once()
        self.assertGreater(len(self.backend.sent), 2)
        bodies = []
        for room, text, ids in self.backend.sent:
            self.assertEqual(room, ROOM)
            self.assertEqual(ids, (CAT,))
            self.assertLessEqual(len(text.encode("utf-8")), 1023)
            prefix, part = text.split("\n", 1)
            self.assertEqual(prefix, "@测试成员 ")
            bodies.append(part)
        self.assertEqual("".join(bodies), body)
        self.assertTrue(bodies[-1].endswith("[TAIL]"))

    def test_multibyte_multiline_reply_round_trips_with_long_display_name(self):
        long_name = "很长的成员昵称" * 5
        self.store.register(long_name, ROOM, CAT)
        body = "第一段中文🌸\n第二段 emoji 😀🎉\n" + "长" * 500 + "\n[TAIL]"
        plugin = self.setup_chat(Mock(return_value=body))
        plugin.on_message(self.chat(), self.ctx)
        self.assertGreater(len(self.backend.sent), 2)
        bodies = []
        for room, text, ids in self.backend.sent:
            self.assertEqual(room, ROOM)
            self.assertEqual(ids, (CAT,))
            self.assertLessEqual(len(text.encode("utf-8")), 1023)
            prefix, part = text.split("\n", 1)
            self.assertEqual(prefix, "@" + long_name + " ")
            bodies.append(part)
        self.assertEqual("".join(bodies), body)

    def test_middle_send_failure_stops_without_resending_earlier_parts(self):
        body = "".join(chr(0x4E00 + index) for index in range(1200))
        plugin = self.setup_chat(Mock(return_value=body))
        attempts = []

        def send_at_text(room, text, ids):
            attempts.append(text)
            if len(attempts) == 2:
                return False
            self.backend.sent.append((room, text, ids))
            return True

        self.backend.send_at_text = send_at_text
        with self.assertRaises(RuntimeError):
            plugin.on_message(self.chat(), self.ctx)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(self.backend.sent), 1)
        self.assertEqual(len(set(attempts)), 2)

    def test_context_expiry_midway_stops_later_parts(self):
        plugin = self.setup_chat(Mock(return_value="中" * 1200))
        attempts = []

        def send_at_text(room, text, ids):
            attempts.append(text)
            self.backend.sent.append((room, text, ids))
            self.store.reset_context(ROOM)
            return True

        self.backend.send_at_text = send_at_text
        plugin.on_message(self.chat(), self.ctx)
        self.assertEqual(len(attempts), 1)

    def test_detail_request_calls_the_main_model_once_with_full_history(self):
        self.store.record(self.message("前面聊了甲", sequence=1))
        self.store.record(self.message("机器人说过乙", sequence=2, data={"bot_generated": True}))
        provider = Mock(return_value="详细回答。")
        plugin = self.setup_chat(provider)
        ask = "请详细展开学吉他的步骤，继续展开也可以"
        plugin.on_message(self.chat(ask, sequence=3), self.ctx)
        provider.assert_called_once()
        sent = provider.call_args.args[1]
        self.assertIn("详细展开", sent)
        self.assertIn("继续展开", sent)
        history = provider.call_args.kwargs["history"]
        self.assertEqual([row["role"] for row in history], ["user", "assistant"])
        self.assertIn("前面聊了甲", history[0]["content"])
        self.assertEqual(history[1]["content"], "[机器人发言] 机器人说过乙")

    def test_invalid_model_reply_preserves_context(self):
        plugin = self.setup_chat(Mock(return_value=""))
        plugin.on_message(self.chat(), self.ctx)
        self.assertEqual(self.backend.sent, [])
        self.assertTrue(self.store.history("测试成员", ROOM))

    def test_game_reply_preserves_next_word_and_rule(self):
        reply = game_reply(FIXED_NAME, True, "", "一心一意", "意气风发", "发扬光大")
        self.assertIn("发扬光大", reply)
        self.assertIn("以“大”", reply)

    def test_runtime_prepares_reset_before_dispatch(self):
        app = object.__new__(BotApplication)
        app.config, app.members, app.plugins = {"chat_allowlist": []}, self.store, Mock()
        app.plugins.capture.side_effect = lambda message: message
        self.store.record(self.message("旧消息"))
        snapshot = self.store.recent_snapshot(ROOM)
        app._emit(self.message("/清空"))
        self.assertTrue(app.plugins.dispatch.call_args.args[0].data["context_reset"])
        self.assertFalse(self.store.snapshot_is_current(snapshot))


class NativeAtReceiveTests(unittest.TestCase):
    def test_cdata_and_multiple_members_are_parsed_without_guessing_names(self):
        self.assertEqual(parse_at_members("<msgsource><atuserlist><![CDATA[wxid_a,amy123,wxid_a]]></atuserlist></msgsource>"),
                         ("wxid_a", "amy123"))
        self.assertEqual(parse_at_members("<msgsource><atuserlist>filehelper,坏名字,wxid_b</atuserlist></msgsource>"), ("wxid_b",))
        for source in ("", "@机器人", "<msgsource>", "<!DOCTYPE a [<!ENTITY x 'abc'>]><a>&x;</a>", "x" * 4096):
            self.assertEqual(parse_at_members(source), ())

    def test_visible_labels_are_removed_on_unicode_at_boundary(self):
        self.assertEqual(strip_at_labels("@机器 人\u2005@小明\u2005请看看这句话"), "请看看这句话")
        self.assertEqual(strip_at_labels("普通 @example.com 文本"), "普通 @example.com 文本")

    def test_native_metadata_marks_only_actual_robot_group_at(self):
        native = daily.ROOT / "native"
        if str(native) not in sys.path:
            sys.path.insert(0, str(native))
        from text_service import NativeTextService
        from filehelper_service import ReceivedTextMessage
        service = NativeTextService(pid=123)
        service.router.set_account(ROBOT)
        msg = ReceivedTextMessage(type=1, sender=ROOM.encode(), receiver=ROBOT.encode(),
                                  text=(CAT + ":\n@机器人\u2005你好").encode(),
                                  mention_source=f"<msgsource><atuserlist>{ROBOT}</atuserlist></msgsource>".encode())
        self.assertTrue(service._map_message(msg)["is_at_self"])
        msg.mention_source = f"<msgsource><atuserlist>{OTHER}</atuserlist></msgsource>".encode()
        self.assertFalse(service._map_message(msg)["is_at_self"])
        msg.mention_source = b""
        self.assertFalse(service._map_message(msg)["is_at_self"])
        msg.sender, msg.text = CAT.encode(), "普通私聊".encode()
        msg.mention_source = f"<msgsource><atuserlist>{ROBOT}</atuserlist></msgsource>".encode()
        self.assertFalse(service._map_message(msg)["is_at_self"])

    def test_quote_message_is_mapped_to_its_reply_text(self):
        native = daily.ROOT / "native"
        if str(native) not in sys.path:
            sys.path.insert(0, str(native))
        from text_service import NativeTextService
        from filehelper_service import ReceivedTextMessage
        service = NativeTextService(pid=123)
        service.router.set_account(ROBOT)
        xml = ("<msg><appmsg><title>这个呢</title><type>57</type><refermsg><type>1</type>"
               f"<chatusr>{ROBOT}</chatusr><displayname>机器人</displayname>"
               "<content>今晚吃啥</content></refermsg></appmsg></msg>")
        msg = ReceivedTextMessage(type=49, sender=ROOM.encode(), receiver=ROBOT.encode(),
                                  text=(CAT + ":\n" + xml).encode())
        data = service._map_message(msg)
        self.assertEqual(data["msg"], "这个呢")
        self.assertEqual(data["quote"]["text"], "今晚吃啥")
        self.assertEqual(data["quote"]["chatusr"], ROBOT)


class AddressingTests(unittest.TestCase):
    """手打 @ 名字、引用机器人、以及从真实艾特学习别名。"""

    def setUp(self):
        reset_learned_aliases()

    def tearDown(self):
        reset_learned_aliases()

    members = type("Members", (), {"account": ROBOT, "wechat_name": lambda self, wxid: "机器人",
                                   "display_name": lambda self, wxid, room: None})()

    def message(self, text, **data):
        return Message(text, ROOM, sender_id=CAT, is_group=True, data=data)

    def test_typed_at_matches_configured_alias(self):
        self.assertEqual(typed_at("@有个ai 是不是出 bug 了", ("有个ai",)), "有个ai")
        self.assertIsNone(typed_at("@别人 是不是出 bug 了", ("有个ai",)))
        self.assertEqual(typed_at("@有个ai", ("有个ai",)), "有个ai")  # 只有艾特没有正文

    def test_typed_at_accepts_punctuation_and_names_with_spaces(self):
        self.assertEqual(typed_at("@有个ai，看看这个", ("有个ai",)), "有个ai")
        self.assertEqual(typed_at("问@有个ai。", ("有个ai",)), "有个ai")
        self.assertEqual(typed_at("@有 个ai 在吗", ("有 个ai",)), "有 个ai")

    def test_typed_at_ignores_email_and_longer_names(self):
        self.assertIsNone(typed_at("邮箱 a@bot.com 收一下", ("bot.com",)))
        self.assertIsNone(typed_at("@有个ai2 在吗", ("有个ai",)))
        self.assertIsNone(typed_at("@有个aiabc", ("有个ai",)))

    def test_typed_at_is_recognised_without_structural_mention(self):
        self.assertEqual(at_source(self.message("@机器人 在吗"), self.members), "typed")
        for alias in ("A姐", "A哥"):
            self.assertEqual(at_source(self.message(f"@{alias} 在吗"), self.members), "typed")
        self.assertIsNone(at_source(self.message("@A姐 在吗", at_members=(OTHER,)), self.members))

    def test_structural_at_teaches_the_visible_label(self):
        message = self.message("@有个ai 你看", is_at_self=True, at_members=(ROBOT,))
        self.assertEqual(at_source(message, self.members), "structural")
        self.assertIn("有个ai", learned_aliases())
        # 之后别人手打同一个名字也认。
        self.assertEqual(at_source(self.message("@有个ai 你看"), self.members), "typed")

    def test_two_labels_are_not_learned(self):
        message = self.message("@有个ai @小明 你看", is_at_self=True, at_members=(ROBOT,))
        at_source(message, self.members)
        self.assertNotIn("有个ai", learned_aliases())

    def test_learned_label_does_not_leak_to_another_room(self):
        other_room = "other_room@chatroom"
        message = self.message("@有个ai 你看", is_at_self=True, at_members=(ROBOT,))
        at_source(message, self.members)
        self.assertIn("有个ai", learned_aliases(ROOM))
        self.assertEqual(learned_aliases(other_room), ())
        other = Message("@有个ai 你看", other_room, sender_id=CAT, is_group=True, data={})
        self.assertIsNone(at_source(other, self.members))

    def test_quote_of_bot_counts_as_addressing(self):
        message = self.message("这个呢", quote={"name": "机器人", "text": "今晚吃啥", "chatusr": ROBOT})
        self.assertEqual(at_source(message, self.members), "quote")

    def test_quote_of_someone_else_is_not_addressing(self):
        message = self.message("这个呢", quote={"name": "小明", "text": "今晚吃啥", "chatusr": OTHER})
        self.assertIsNone(at_source(message, self.members))

    def test_quote_parser_rejects_unsafe_or_non_quote_xml(self):
        good = "<msg><appmsg><title>好</title><type>57</type><refermsg><type>1</type><content>原文</content></refermsg></appmsg></msg>"
        self.assertEqual(parse_quote(good)["text"], "原文")
        for source in ("", "不是 XML", "<!DOCTYPE a [<!ENTITY x 'abc'>]><a>&x;</a>", "<msg></msg>",
                       "<msg><appmsg><type>5</type><title>链接</title></appmsg></msg>", "x" * 9000):
            with self.subTest(source=source[:20]):
                self.assertIsNone(parse_quote(source))


if __name__ == "__main__":
    unittest.main()
