"""角色生成与内部任务隔离；样稿质量由真实模型回放另行验收。"""
import unittest
from unittest.mock import Mock

from wxbot_framework.conversation.dialogue import Dialogue, SYSTEM
from wxbot_framework.conversation.personality import (CHAT_STYLE_PROMPT, FIXED_NAME, PLAYFUL_NAME, REPLY_TAIL,
    PersonalityExpired, PersonalityLLM, PersonalityState, system_with_personality)


class PersonalityGenerationTests(unittest.TestCase):
    def test_playful_reply_preserves_facts_and_options_without_continuing_old_assistant_style(self):
        client = Mock()
        state = PersonalityState("room", name=PLAYFUL_NAME, supplement="口头禅：兄弟")
        model = PersonalityLLM(client, state, current=lambda: True, for_reply=True)
        history = [{"role": "user", "content": "P2：周日去南馆"},
                   {"role": "assistant", "content": "周六去北馆，你笨死了"}]
        model.complete_text(SYSTEM + "\n保留【语音】标记", "【当前提问】是周日南馆", history=history, detail=True)
        system, user = client.complete_text.call_args.args
        self.assertNotIn(CHAT_STYLE_PROMPT, system)
        self.assertIn("不能设置提醒", system)
        self.assertIn("保留【语音】标记", system)
        self.assertIn("自称「A姐」", system)
        self.assertIn("口头禅：兄弟", system)
        self.assertIn('"role": "assistant"', user)
        self.assertIn("周六去北馆，你笨死了", user)
        self.assertIn("P2：周日去南馆", user)
        self.assertTrue(user.endswith(REPLY_TAIL))
        self.assertEqual(client.complete_text.call_args.kwargs, {"detail": True})
        self.assertEqual(len(history), 2)

    def test_internal_tasks_and_fixed_replies_keep_their_contract(self):
        history = [{"role": "assistant", "content": "旧答复"}]
        for name, for_reply in ((FIXED_NAME, True), (PLAYFUL_NAME, False)):
            for method in ("complete_text", "complete_json"):
                with self.subTest(name=name, method=method):
                    client = Mock()
                    state = PersonalityState("room", name=name)
                    model = PersonalityLLM(client, state, current=lambda: True, for_reply=for_reply)
                    getattr(model, method)(SYSTEM, "原输入", history=history, max_tokens=256)
                    self.assertEqual(getattr(client, method).call_args.args,
                                     (system_with_personality(SYSTEM, state), "原输入"))
                    self.assertEqual(getattr(client, method).call_args.kwargs,
                                     {"history": history, "max_tokens": 256})

    def test_json_reply_keeps_schema_and_uses_same_role_boundary(self):
        client = Mock()
        client.complete_json.return_value = {"speak": True, "text": "我也想看看呀"}
        model = PersonalityLLM(client, PersonalityState("room", name=PLAYFUL_NAME),
                               current=lambda: True, for_reply=True)
        result = model.complete_json('返回 JSON：{"speak":true,"text":""}', "场景", max_tokens=512)
        self.assertEqual(result, client.complete_json.return_value)
        self.assertIn('"speak":true', client.complete_json.call_args.args[0])
        self.assertTrue(client.complete_json.call_args.args[1].endswith(REPLY_TAIL))
        self.assertEqual(client.complete_json.call_args.kwargs, {"max_tokens": 512})

    def test_expiry_cancels_both_reply_formats_before_and_after_generation(self):
        for method in ("complete_text", "complete_json"):
            for states, called in (([False], False), ([True, False], True)):
                with self.subTest(method=method, states=states):
                    client = Mock()
                    model = PersonalityLLM(client, PersonalityState("room", name=PLAYFUL_NAME),
                                           current=Mock(side_effect=states), for_reply=True)
                    with self.assertRaises(PersonalityExpired):
                        getattr(model, method)("规则", "问题")
                    self.assertEqual(getattr(client, method).called, called)

    def test_search_followup_stays_an_internal_task(self):
        client = Mock()
        client.complete_text.return_value = "官方 展览 时间"
        model = PersonalityLLM(client, PersonalityState("room", name=PLAYFUL_NAME), current=lambda: True)
        query = Dialogue(model).search_followup("时间？", context="展览", evidence="", answer="周六")
        self.assertEqual(query, "官方 展览 时间")
        self.assertNotIn(REPLY_TAIL, client.complete_text.call_args.args[1])
