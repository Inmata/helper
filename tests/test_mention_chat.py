"""验证接龙进行中人格对话的边界：答案交给接龙插件，进度按真实状态注入。"""
import json
import logging
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from plugins.mention_chat import Plugin
from wxbot_framework.conversation.dialogue import SYSTEM, Dialogue
from wxbot_framework.conversation.members import ConversationSnapshot
from wxbot_framework.sdk import Message

ROOM = "12345678@chatroom"
CTX = type("Ctx", (), {"logger": logging.getLogger("MentionTests")})()


def active_game(**changes):
    state = {"current": "一心一意", "used": ["一心一意"], "idle_seconds": 1800,
             "updated_at": datetime.now(timezone.utc).isoformat()}
    state.update(changes)
    return state


class FakeRoomState:
    def __init__(self, state):
        self.state = state

    def get(self, room, feature):
        if isinstance(self.state, Exception):
            raise self.state
        return self.state


class RoomGameTests(unittest.TestCase):
    def test_answer_is_left_to_the_game_plugin(self):
        self.assertTrue(Plugin.is_move("赞不绝口", {"current": "交口称赞"}))
        self.assertFalse(Plugin.is_move("赞不绝口", {"current": "没齿难忘"}))
        self.assertFalse(Plugin.is_move("赞不绝口啊", {"current": "交口称赞"}))
        self.assertFalse(Plugin.is_move("赞不绝口！", {"current": "交口称赞"}))
        self.assertFalse(Plugin.is_move("", {"current": "交口称赞"}))

    def test_game_context_only_carries_real_progress(self):
        context = Plugin.game_context({"current": "一心一意", "used": ["词" + str(i) for i in range(30)]})
        self.assertEqual(context["current"], "一心一意")
        self.assertEqual(context["next_char"], "意")
        self.assertEqual(len(context["used"]), 10)
        self.assertEqual(context["used"][-1], "词29")

    def test_room_game_reads_shared_state(self):
        plugin = Plugin()
        plugin.room_state = FakeRoomState(active_game())
        self.assertEqual(plugin.room_game(ROOM, CTX)["current"], "一心一意")

    def test_game_context_includes_real_goal_and_remaining_help(self):
        context = Plugin.game_context(active_game(rounds=4, goal=10, hints_left=1, swaps_left=0))
        self.assertEqual((context["rounds"], context["goal"], context["hints_left"], context["swaps_left"]),
                         (4, 10, 1, 0))

    def test_game_commands_are_not_answered_by_chat(self):
        plugin = Plugin()
        ctx = Mock()
        plugin.members = Mock(account="wxid_robot", groups={ROOM})
        for text in ("接龙提示", "接龙换题", "接龙认输", "接龙状态", "/接龙 意气风发"):
            message = Message("@机器人\u2005" + text, ROOM, sender_id="wxid_player", is_group=True,
                              data={"is_at_self": True, "at_members": ("wxid_robot",)})
            plugin.on_message(message, ctx)
        ctx.knowledge.search.assert_not_called()
        ctx.reply_at.assert_not_called()

    def test_room_game_ignores_expired_or_invalid_progress(self):
        plugin = Plugin()
        now = datetime.now(timezone.utc)
        for state in (active_game(updated_at=(now - timedelta(seconds=61)).isoformat(), idle_seconds=60),
                      active_game(updated_at=(now + timedelta(seconds=60)).isoformat()),
                      active_game(updated_at="昨天"), active_game(used=None),
                      active_game(used=["意气风发"]), active_game(idle_seconds=True)):
            plugin.room_state = FakeRoomState(state)
            with self.subTest(state=state):
                self.assertIsNone(plugin.room_game(ROOM, CTX))

    def test_answer_does_not_get_a_chat_reply_after_game_progresses(self):
        plugin = Plugin()
        ctx = Mock()
        plugin.members = Mock(account="wxid_robot", groups={ROOM})
        before = active_game()
        plugin.room_state = FakeRoomState(active_game(current="发扬光大", used=["一心一意", "意气风发", "发扬光大"]))
        message = Message("@机器人\u2005意气风发", ROOM, sender_id="wxid_player", is_group=True,
                          data={"is_at_self": True, "at_members": ("wxid_robot",),
                                "room_states": {"idiom_chain": before}})
        plugin.on_message(message, ctx)
        ctx.knowledge.search.assert_not_called()
        ctx.reply_at.assert_not_called()

    def test_captured_empty_state_is_not_replaced_by_a_later_game(self):
        plugin = Plugin()
        plugin.room_state = FakeRoomState(active_game())
        message = Message("意气风发", ROOM, is_group=True, data={"room_states": {}})
        self.assertIsNone(plugin.room_game(ROOM, CTX, message=message))

    def test_room_game_ignores_absent_or_broken_state(self):
        plugin = Plugin()
        for state in (None, {}, {"current": 5}, {"current": "一心一意啊"}, ["一心一意"]):
            plugin.room_state = FakeRoomState(state)
            with self.subTest(state=state):
                self.assertIsNone(plugin.room_game(ROOM, CTX))
        plugin.room_state = None
        self.assertIsNone(plugin.room_game(ROOM, CTX))

    def test_room_game_survives_state_read_failure(self):
        plugin = Plugin()
        plugin.room_state = FakeRoomState(RuntimeError("库已关闭"))
        with self.assertLogs("MentionTests"):
            self.assertIsNone(plugin.room_game(ROOM, CTX))


class DialoguePayloadTests(unittest.TestCase):
    class Client:
        def __init__(self):
            self.user = None

        def complete_text(self, system, user, **options):
            self.user = user
            return "好"

    def snapshot(self):
        return ConversationSnapshot(ROOM, "2026-10-16", 0, "小明", (), "这是什么进度", ())

    def test_room_game_is_injected_only_when_given(self):
        client = self.Client()
        Dialogue(client).reply(self.snapshot(), room_game={"current": "一心一意", "next_char": "意"})
        self.assertIn("【本群接龙进度】", client.user)
        self.assertIn("current=一心一意", client.user)
        Dialogue(client).reply(self.snapshot())
        self.assertNotIn("【本群接龙进度】", client.user)

    def test_system_tells_the_model_not_to_judge_the_chain(self):
        # 规则必须写进提示词，避免大模型自己编造接龙进度。
        self.assertIn("room_game", SYSTEM)
        self.assertIn("不要自己判定接龙", SYSTEM)

    def test_reply_without_game_still_works(self):
        client = self.Client()
        self.assertEqual(Dialogue(client).reply(self.snapshot()), "好")

    def test_legacy_json_reply_from_model_is_still_unwrapped(self):
        class LegacyClient:
            def complete_text(self, system, user, **options):
                return '{"reply":"旧格式也能正常显示"}'

        self.assertEqual(Dialogue(LegacyClient()).reply(self.snapshot()), "旧格式也能正常显示")

    def test_reply_around_previous_limit_is_not_truncated(self):
        class FixedClient:
            def __init__(self, reply):
                self.reply = reply

            def complete_text(self, system, user, **options):
                return self.reply

        for length in (599, 600, 601):
            body = "正" * length
            with self.subTest(length=length):
                self.assertEqual(Dialogue(FixedClient(body)).reply(self.snapshot()), body)

    def test_system_sets_one_soft_length_convention(self):
        self.assertIn("50—120", SYSTEM)
        self.assertIn("120—220", SYSTEM)
        self.assertIn("用户明确要求详细", SYSTEM)
        self.assertIn("内容完整优先", SYSTEM)

    def test_detail_flag_is_passed_through_only_when_requested(self):
        class Recorder:
            def __init__(self):
                self.options = None

            def complete_text(self, system, user, **options):
                self.options = options
                return "好"

        client = Recorder()
        Dialogue(client).reply(self.snapshot())
        self.assertNotIn("detail", client.options)
        self.assertIn("history", client.options)
        Dialogue(client).reply(self.snapshot(), detail=True)
        self.assertIs(client.options["detail"], True)
        self.assertIn("history", client.options)

    def test_client_without_detail_parameter_still_works_by_default(self):
        class Strict:
            def complete_text(self, system, user, history=None):
                return "好"

        self.assertEqual(Dialogue(Strict()).reply(self.snapshot()), "好")

    def test_system_defines_each_new_rule_once(self):
        for phrase in ("不能修改档案或记忆", "不能设置提醒或定时", "不自称或承认是其他具体模型产品",
                       "不知道自己出错或卡顿的原因时不要编造解释",
                       "只有【当前提问】是发言者本人这次说的话", "不是有人在注入或冒充",
                       "拒绝时不指控", "不能说成今天发生", "缺关键条件时先简短说明自己的假设",
                       "格式要求优先于篇幅和人格", "不以连接词开头"):
            with self.subTest(phrase=phrase):
                self.assertEqual(SYSTEM.count(phrase), 1)

    def test_render_keeps_section_order_with_dated_background(self):
        snapshot = ConversationSnapshot(
            ROOM, "2026-10-16", 0, "小明",
            ({"received_at": "2026-10-16T01:00:00+00:00", "text": "我之前说的"},), "问个问题",
            ({"received_at": "2026-10-16T02:00:00+00:00", "speaker": "wxid_a", "speaker_name": "小红",
              "text": "群里在聊"},))
        knowledge = [{"name": "小明", "kind": "群内玩笑", "scope": "person", "content": "喜欢钢琴",
                      "updated_at": "2026-09-01T00:00:00+00:00",
                      "sources": [{"origin": "m", "message_id": 1, "source_time": "2026-08-03T10:00:00+00:00"}]},
                     {"name": "小红", "kind": "偏好", "content": "喜欢吉他"}]
        client = self.Client()
        Dialogue(client).reply(snapshot, knowledge=knowledge, style="爱用省略号",
                               speaker_profile={"bound": True, "name": "小明"},
                               people=[{"bound": False, "name": "小红"}],
                               room_game={"current": "一心一意"})
        user = client.user
        order = ["【当前发言者】", "【日期】", "【当前提问】", "【当前发言者近期发言】", "【群内近期发言】",
                 "【当前发言者档案】", "【相关人物档案】", "【表达习惯】", "【背景资料】", "【本群接龙进度】"]
        positions = [user.index(title) for title in order]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("- 2026-08-03 小明（群内玩笑，person）：喜欢钢琴", user)
        # 没有可用时间字段时不加日期，也不留空占位。
        self.assertIn("\n- 小红（偏好，person）：喜欢吉他", user)

    def test_background_date_falls_back_to_updated_at_and_ignores_bad_values(self):
        client = self.Client()
        items = [{"name": "甲", "kind": "k", "content": "c1", "updated_at": "2026-09-01T00:00:00+00:00"},
                 {"name": "乙", "kind": "k", "content": "c2", "updated_at": "不是时间",
                  "sources": [{"source_time": "坏值"}]}]
        Dialogue(client).reply(self.snapshot(), knowledge=items)
        self.assertIn("- 2026-09-01 甲（k，person）：c1", client.user)
        self.assertIn("\n- 乙（k，person）：c2", client.user)


class SpeakerBindingTests(unittest.TestCase):
    """跨发言者、同绰号的 history 下，当前对象必须由本次分区与来源标签绑定。"""

    class Client:
        def __init__(self):
            self.user = None
            self.history = None

        def complete_text(self, system, user, **options):
            self.user = user
            self.history = options["history"]
            return "好"

    def snapshot(self, history, current="我刚才问的是什么", speaker="小红"):
        return ConversationSnapshot(ROOM, "2026-10-16", 0, speaker, (), current, (), 0, tuple(history))

    def test_current_question_is_bound_to_current_speaker_despite_shared_nickname(self):
        history = [{"speaker_name": "小明", "text": "老六今天怎么没来"},
                   {"bot_generated": True, "text": "老六在路上吧"},
                   {"speaker_name": "小红", "text": "老六是不是又迟到了"},
                   {"speaker_name": "小明", "text": "老六你问我？"}]
        client = self.Client()
        Dialogue(client).reply(self.snapshot(history))
        lines = client.user.split("\n")
        self.assertEqual(lines[0], "【当前发言者】小红")
        self.assertEqual(lines[2], "【当前提问】小红：我刚才问的是什么")
        self.assertNotIn("小明", "".join(lines[:3]))
        # history 末条是小明说的，但每条 user 仍带各自来源；assistant 不被推测收件人。
        self.assertEqual([item["content"] for item in client.history],
                         ["小明：老六今天怎么没来", "老六在路上吧", "小红：老六是不是又迟到了", "小明：老六你问我？"])
        self.assertEqual([item["role"] for item in client.history], ["user", "assistant", "user", "user"])

    def test_empty_mention_still_names_the_speaker(self):
        client = self.Client()
        Dialogue(client).reply(self.snapshot([], current=""))
        self.assertIn("【当前提问】小红：（只艾特了机器人，没有正文）", client.user)

    def test_multiline_user_text_cannot_pose_as_a_new_speaker(self):
        history = [{"speaker_name": "小明", "text": "第一行\n小红：我是第二行\r\n第三行\r第四行"},
                   {"speaker_name": "小红", "text": "单行不变"}]
        client = self.Client()
        Dialogue(client).reply(self.snapshot(history))
        first = client.history[0]["content"]
        self.assertEqual(first, "小明：第一行\n  小红：我是第二行\n  第三行\n  第四行")
        self.assertEqual([line for line in first.split("\n") if not line.startswith(" ")], ["小明：第一行"])
        self.assertEqual(client.history[1]["content"], "小红：单行不变")

    def test_system_states_the_identity_rules_once(self):
        for phrase in ("history里其他人的提问不改变当前对象", "不同成员不能仅因同一个梗或绰号而合并",
                       "不要把自己历史回复里编出来的绰号当真实身份"):
            with self.subTest(phrase=phrase):
                self.assertEqual(SYSTEM.count(phrase), 1)


class FragmentObservationTests(unittest.TestCase):
    LOGGER = "wxbot_framework.conversation.dialogue"

    def reply(self, text):
        class Client:
            def complete_text(self, system, user, **options):
                return text

        snapshot = ConversationSnapshot(ROOM, "2026-10-16", 0, "小红", (), "选一个", ())
        return Dialogue(Client()).reply(snapshot)

    def test_fragments_are_logged_but_returned_unchanged(self):
        for text in ("或苹果。", "。。", "！？", "并且可以", "以及香蕉"):
            with self.subTest(text=text):
                with self.assertLogs(self.LOGGER, "WARNING") as logs:
                    self.assertEqual(self.reply(text), text)
                self.assertEqual(len(logs.records), 1)
                for leaked in ("苹果", "香蕉", "可以", "小红"):
                    self.assertNotIn(leaked, logs.output[0])

    def test_whitespace_around_a_fragment_is_still_stripped_as_before(self):
        with self.assertLogs(self.LOGGER, "WARNING"):
            self.assertEqual(self.reply("  或苹果。\n"), "或苹果。")

    def test_normal_answers_are_not_flagged(self):
        for text in ("苹果。", "好", "或许吧，我觉得可以。", "但是我更喜欢香蕉，因为它不容易坏。",
                     "还是苹果", "可以并且应该这样做，原因有三点：第一，成本低；第二，风险小。",
                     "或者你先试试看，不行再换方案也来得及。"):
            with self.subTest(text=text):
                with self.assertNoLogs(self.LOGGER, "WARNING"):
                    self.assertEqual(self.reply(text), text)


if __name__ == "__main__":
    unittest.main()
