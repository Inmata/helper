"""验证接龙进行中人格对话的边界：答案交给接龙插件，进度按真实状态注入。"""
import json
import logging
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, Mock, patch

from plugins.mention_chat import Plugin
from wxbot_framework.conversation import utterance
from wxbot_framework.conversation.dialogue import SYSTEM, Dialogue
from wxbot_framework.conversation.members import ConversationSnapshot, SnapshotExpired
from wxbot_framework.conversation.personality import PersonalityState
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

    def test_system_sets_group_member_tone_instead_of_char_ranges(self):
        for old in ("50—120", "120—220"):
            self.assertNotIn(old, SYSTEM)
        self.assertIn("像群里的一个群友那样说话", SYSTEM)
        self.assertIn("不超过60个汉字", SYSTEM)
        self.assertIn("用户明确要求详细", SYSTEM)
        self.assertIn("内容完整优先", SYSTEM)
        self.assertIn("不要空行分段", SYSTEM)

    def test_system_lists_every_avoid_phrase_once(self):
        from wxbot_framework.conversation.dialogue import AVOID_PHRASES
        self.assertGreaterEqual(len(AVOID_PHRASES), 20)
        for phrase in AVOID_PHRASES:
            with self.subTest(phrase=phrase):
                self.assertEqual(SYSTEM.count(f"“{phrase}”"), 1)

    def test_examples_section_is_rendered_last_and_only_when_given(self):
        client = self.Client()
        Dialogue(client).reply(self.snapshot(), room_game={"current": "甲"}, examples=["虚构短句一", "虚构短句二"])
        user = client.user
        self.assertLess(user.index("【本群接龙进度】"), user.index("【群友说话的样子】"))
        self.assertTrue(user.endswith("- 虚构短句一\n- 虚构短句二"))
        Dialogue(client).reply(self.snapshot())
        self.assertNotIn("【群友说话的样子】", client.user)

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


class FakeMembers:
    """最小群成员目录：群消息序号基线为 10；``rows`` 是 recent_rows 返回的群友发言（虚构数据）。"""
    account, groups, history_enabled = "wxid_robot", {ROOM}, True

    def __init__(self):
        self.rows = [{"id": 9, "text": "早就睡了吧", "bot_generated": 0, "is_self": 0}]

    def conversation_snapshot(self, message):
        return ConversationSnapshot(ROOM, "2026-10-16", 0, "小明", (), message.text, ())

    def at_display_name(self, sender_id, chat_id):
        return "小明"

    def latest_id(self, room):
        return 10

    def recent_rows(self, room, **options):
        return list(self.rows)

    def interject(self):
        self.rows.append({"id": 11, "text": "有人插话", "bot_generated": 0, "is_self": 0})


class FakeCtx:
    """记录每次发送走的是 reply_at 还是 reply；发送函数像真实发送层一样在 delivery_guard 里完成。"""

    def __init__(self, members, reply):
        self.logger = logging.getLogger("MentionTests")
        self.members, self.reply_text = members, reply
        self.sent = []  # (方法名, 正文)
        self.before_guard = None  # 守卫之前的钩子，模拟发送层打字节奏等待期间进来的新消息
        self.guard_calls, self.expire_at_call, self.fail_at_send = 0, None, None
        self.completions = 0
        self.knowledge = MagicMock()
        self.knowledge.member_entity.return_value = {"id": 1}
        self.knowledge.speaker_profile.return_value = None
        self.knowledge.search.return_value = []
        self.knowledge.lookup_people.return_value = []
        self.knowledge.style.return_value = None
        ctx = self

        class Client:
            def complete_text(self, system, user, **options):
                ctx.completions += 1
                return ctx.reply_text

        self.client = Client()

    def personality(self, message):
        return PersonalityState(ROOM)

    def llm_for(self, message, *, personality=None):
        return self.client

    @contextmanager
    def response_guard(self, snapshot, personality):
        self.guard_calls += 1
        if self.expire_at_call is not None and self.guard_calls >= self.expire_at_call:
            raise SnapshotExpired("记录已清空")
        yield

    def _send(self, method, text, delivery_guard):
        if self.before_guard is not None:
            self.before_guard(len(self.sent))
        with delivery_guard():
            if self.fail_at_send is not None and len(self.sent) + 1 >= self.fail_at_send:
                return False
            self.sent.append((method, text))
            return True

    def reply_at(self, message, text, *, display_name=None, delivery_guard=None):
        return self._send("reply_at", text, delivery_guard)

    def reply(self, message, text, *, delivery_guard=None):
        return self._send("reply", text, delivery_guard)


def mention(text, received_at=1.0):
    return Message("@机器人 " + text, ROOM, sender_id="wxid_player", is_group=True, received_at=received_at,
                   data={"is_at_self": True, "at_members": ("wxid_robot",)})


class MentionDeliveryTests(unittest.TestCase):
    """艾特回复端到端：分档、拆分、插话丢弃、守卫和去重（不连网、不调用大模型）。"""

    def setUp(self):
        sleeper = patch.object(utterance.time, "sleep")
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)
        self.members = FakeMembers()

    def build(self, reply):
        ctx = FakeCtx(self.members, reply)
        plugin = Plugin()
        plugin.members, plugin.room_state = self.members, None
        return plugin, ctx

    def run_plugin(self, query, reply):
        plugin, ctx = self.build(reply)
        message = mention(query)
        plugin.on_message(message, ctx)
        return plugin, ctx, message

    def test_chat_split_uses_reply_at_first_then_plain_reply(self):
        _, ctx, _ = self.run_plugin("晚安啦", "行吧。那我先睡了。")
        self.assertEqual(ctx.sent, [("reply_at", "行吧"), ("reply", "那我先睡了")])
        self.assertEqual(ctx.guard_calls, 2)  # 每条单独过守卫
        self.assertEqual(self.sleep.call_count, 1)
        self.assertTrue(0.9 <= self.sleep.call_args.args[0] <= 3.0)  # 真实时钟只流逝了微秒级

    def test_interjection_during_gap_drops_the_rest_without_error(self):
        self.sleep.side_effect = lambda seconds: self.members.interject()
        _, ctx, _ = self.run_plugin("晚安啦", "哈哈！真的假的？我不信。")
        # 条间隔结束后的预检查发现插话，第二条的发送函数根本没有被调用。
        self.assertEqual(ctx.sent, [("reply_at", "哈哈！")])
        self.assertEqual(ctx.guard_calls, 1)

    def test_interjection_after_precheck_is_caught_inside_the_guard(self):
        plugin, ctx = self.build("哈哈！真的假的？我不信。")
        # 预检查之后、拿到守卫之前（发送层打字节奏等待）进来新消息：守卫内的再次检查拒发，不报错。
        ctx.before_guard = lambda sent: self.members.interject() if sent == 1 else None
        plugin.on_message(mention("晚安啦"), ctx)
        self.assertEqual(ctx.sent, [("reply_at", "哈哈！")])
        self.assertEqual(ctx.guard_calls, 2)

    def test_qa_is_not_split_and_sends_one_reply_at(self):
        _, ctx, _ = self.run_plugin("今天几点开会", "可以。先点设置。再点开启。")
        self.assertEqual(ctx.sent, [("reply_at", "可以。先点设置。再点开启。")])
        self.sleep.assert_not_called()

    def test_request_without_question_word_is_not_split(self):
        _, ctx, _ = self.run_plugin("解释一下 TCP", "行吧。那我先睡了。")
        self.assertEqual(ctx.sent, [("reply_at", "行吧。那我先睡了。")])

    def test_detail_sends_every_segment_with_reply_at(self):
        reply = "\n".join(f"第{index}段" + "字" * 120 for index in range(8))
        _, ctx, _ = self.run_plugin("详细说说晚安", reply)
        self.assertGreater(len(ctx.sent), 1)
        self.assertTrue(all(method == "reply_at" for method, _ in ctx.sent))
        self.assertEqual("".join(text for _, text in ctx.sent).replace("\n", ""), reply.replace("\n", ""))
        self.sleep.assert_not_called()

    def test_oversized_qa_reply_is_segmented_not_truncated(self):
        reply = "中" * 700 + "[END]"
        _, ctx, _ = self.run_plugin("今天几点开会", reply)
        self.assertGreater(len(ctx.sent), 1)
        self.assertEqual("".join(text for _, text in ctx.sent), reply)

    def test_partial_send_then_guard_expiry_keeps_dedup_record(self):
        plugin, ctx = self.build("行吧。那我先睡了。")
        ctx.expire_at_call = 2
        message = mention("晚安啦")
        plugin.on_message(message, ctx)
        self.assertEqual(ctx.sent, [("reply_at", "行吧")])
        # 同一提问再来一次（到达时刻不晚于首次结束）不再生成，也不重发。
        plugin.on_message(message, ctx)
        self.assertEqual(ctx.completions, 1)
        self.assertEqual(ctx.sent, [("reply_at", "行吧")])

    def test_partial_send_then_failure_keeps_dedup_record(self):
        plugin, ctx = self.build("行吧。那我先睡了。")
        ctx.fail_at_send = 2
        message = mention("晚安啦")
        with self.assertRaises(RuntimeError):
            plugin.on_message(message, ctx)
        self.assertEqual(ctx.sent, [("reply_at", "行吧")])
        plugin.on_message(message, ctx)
        self.assertEqual(ctx.completions, 1)

    def test_nothing_sent_releases_dedup_so_user_can_retry(self):
        plugin, ctx = self.build("行吧。那我先睡了。")
        ctx.expire_at_call = 1
        message = mention("晚安啦")
        plugin.on_message(message, ctx)
        self.assertEqual(ctx.sent, [])
        ctx.expire_at_call = None
        plugin.on_message(message, ctx)
        self.assertEqual(ctx.completions, 2)
        self.assertEqual(ctx.sent[0], ("reply_at", "行吧"))


if __name__ == "__main__":
    unittest.main()
