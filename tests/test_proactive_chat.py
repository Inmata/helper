"""主动发言：决策接缝、预算闸门、Jev choice 解析与插件编排。"""
import io
import json
import logging
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from plugins.proactive_chat import Plugin, read_options
from wxbot_framework.conversation.planner import (REPLY, SKIP, WAIT, Action, JevSpeakPlanner, SpeakBudget,
                                                  SpeakContext, anonymize_text, build_speak_context,
                                                  describe_gap, generate_proactive, jev_state, mentions_bot)
from wxbot_framework.conversation.members import SnapshotExpired
from wxbot_framework.integrations.jev import JevClient, JevError, JevFormatError
from wxbot_framework.sdk import Message

ROOM = "12345678@chatroom"
ROBOT = "wxid_robot"


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value


def context(lines=((5, "P1", "机器人在吗"),), **options):
    values = {"bot_last_seconds": None, "mentioned": True, "local_time": "10:00", "persona": "人格", "streak": 0}
    values.update(options)
    return SpeakContext(tuple(lines), **values)


def fake_jev(**probabilities):
    jev = MagicMock()
    jev.choice.return_value = probabilities
    return jev


class ActionTests(unittest.TestCase):
    def test_action_type_is_validated_and_args_read_only(self):
        with self.assertRaises(ValueError):
            Action("send")
        action = Action(REPLY, {"probability": 0.9})
        with self.assertRaises(TypeError):
            action.args["probability"] = 0


class PlannerTests(unittest.TestCase):
    def test_reply_probability_sums_reply_options(self):
        planner = JevSpeakPlanner(fake_jev(reply_called=0.4, reply_banter=0.3, silent_irrelevant=0.3))
        action = planner.decide(context())
        self.assertEqual(action.type, REPLY)
        self.assertEqual(action.args["probability"], 0.7)

    def test_streak_raises_threshold(self):
        jev = fake_jev(reply_called=0.28, silent_irrelevant=0.72)
        planner = JevSpeakPlanner(jev)
        self.assertEqual(planner.decide(context(streak=0)).type, REPLY)
        self.assertEqual(planner.decide(context(streak=1)).type, WAIT)
        # streak=1 阈值 0.35：达到就 reply，差 0.1 以内才 wait。
        self.assertEqual(JevSpeakPlanner(fake_jev(reply_called=0.35, silent_irrelevant=0.65))
                         .decide(context(streak=1)).type, REPLY)
        self.assertEqual(JevSpeakPlanner(fake_jev(reply_called=0.34, silent_irrelevant=0.66))
                         .decide(context(streak=1)).type, WAIT)
        self.assertEqual(JevSpeakPlanner(fake_jev(reply_called=0.24, silent_irrelevant=0.76))
                         .decide(context(streak=1)).type, SKIP)

    def test_default_threshold_matches_measured_choice(self):
        # 阈值 0.25：10-29 真机 25 次判断 reply 概率 0.01–0.48，0.5 一次都没触发；0.25 下约两成判断会进入生成。
        planner = JevSpeakPlanner(fake_jev(reply_banter=0.26, silent_irrelevant=0.74))
        self.assertEqual(planner.decide(context()).type, REPLY)
        self.assertEqual(JevSpeakPlanner(fake_jev(reply_banter=0.18, silent_irrelevant=0.82)).decide(context()).type,
                         WAIT)
        # 复判缓冲区下限 0.15：再低就落在 Jev 判“几乎没关系”的区间，直接 skip，省一次复判调用。
        self.assertEqual(JevSpeakPlanner(fake_jev(reply_banter=0.15, silent_irrelevant=0.85)).decide(context()).type,
                         WAIT)
        self.assertEqual(JevSpeakPlanner(fake_jev(reply_banter=0.14, silent_irrelevant=0.86)).decide(context()).type,
                         SKIP)

    def test_low_probability_skips(self):
        action = JevSpeakPlanner(fake_jev(reply_called=0.02, silent_irrelevant=0.98)).decide(context())
        self.assertEqual(action.type, SKIP)
        self.assertEqual(action.args["reason"], "below-threshold")

    def test_jev_error_skips_without_retry(self):
        jev = MagicMock()
        jev.choice.side_effect = JevError("超时")
        action = JevSpeakPlanner(jev).decide(context())
        self.assertEqual((action.type, action.args["reason"]), (SKIP, "jev-error"))
        self.assertEqual(jev.choice.call_count, 1)

    def test_state_matches_evaluation_layout(self):
        state = jev_state(context([(30, "P1", "早"), (5, "P2", "机器人在吗")], bot_last_seconds=4000))
        self.assertEqual(state["前文（从旧到新）"], ["[30秒前] P1: 早"])
        self.assertEqual(state["刚刚这条"], "P2: 机器人在吗")
        self.assertEqual(state["最近6条的发言人"], ["P1"])
        self.assertEqual(state["机器人上次发言"], "1 小时前")

    def test_describe_gap(self):
        self.assertEqual([describe_gap(value) for value in (None, 5, 120)], ["很久没说过话", "5 秒前", "2 分钟前"])


class ContextBuildTests(unittest.TestCase):
    def test_anonymises_speakers_and_bot_names(self):
        now = datetime(2026, 10, 27, 2, 0, tzinfo=timezone.utc)
        rows = [{"sender_id": "wxid_a", "text": "@小助手 在吗 @张三", "received_at": now - timedelta(seconds=20)},
                {"sender_id": ROBOT, "text": "在", "received_at": now - timedelta(seconds=10), "bot_generated": 1},
                {"sender_id": "wxid_b", "text": "bot 好快", "received_at": now - timedelta(seconds=2)}]
        result = build_speak_context(rows, now=now, bot_names=("小助手",))
        self.assertEqual(result.lines, ((20, "P1", "@机器人 在吗 @群友"), (10, "机器人", "在"), (2, "P2", "bot 好快")))
        self.assertEqual(result.bot_last_seconds, 10)
        self.assertTrue(result.mentioned)

    def test_mention_regex_ignores_embedded_letters(self):
        self.assertFalse(mentions_bot("he said robot"))
        self.assertTrue(mentions_bot("问问 AI"))
        self.assertEqual(anonymize_text("@某人 你好"), "@群友 你好")


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.budget = SpeakBudget(limit=2, window_seconds=600, gap_seconds=60, max_streak=2,
                                  streak_reset_messages=2, clock=self.clock)

    def test_gap_quota_and_window(self):
        self.assertIsNone(self.budget.check(ROOM))
        self.budget.record(ROOM)
        self.assertEqual(self.budget.check(ROOM), "gap")
        self.clock.value += 61
        self.budget.human_message(ROOM)
        self.budget.human_message(ROOM)
        self.budget.record(ROOM)
        self.clock.value += 61
        self.assertEqual(self.budget.check(ROOM), "quota")
        self.clock.value += 600
        self.assertIsNone(self.budget.check(ROOM))

    def test_streak_resets_after_enough_human_messages(self):
        self.budget.limit = 10
        self.budget.record(ROOM)
        self.clock.value += 61
        self.budget.human_message(ROOM)
        self.budget.record(ROOM)
        self.clock.value += 61
        self.assertEqual(self.budget.streak(ROOM), 2)
        self.assertEqual(self.budget.check(ROOM), "streak")
        self.budget.human_message(ROOM)
        self.budget.human_message(ROOM)
        self.assertEqual(self.budget.streak(ROOM), 0)
        self.assertIsNone(self.budget.check(ROOM))

    def test_rooms_are_independent(self):
        self.budget.record(ROOM)
        self.assertIsNone(self.budget.check("other@chatroom"))

    def test_reserve_is_exclusive_and_release_records_only_when_sent(self):
        self.assertIsNone(self.budget.reserve(ROOM))
        self.assertEqual(self.budget.reserve(ROOM), "busy")
        self.budget.release(ROOM, sent=False)
        self.assertEqual(self.budget.count(ROOM), 0)
        self.assertIsNone(self.budget.reserve(ROOM))
        self.budget.release(ROOM, sent=True)
        self.assertEqual((self.budget.count(ROOM), self.budget.check(ROOM)), (1, "gap"))

    def test_mark_sent_counts_once_and_keeps_later_human_messages(self):
        self.budget.limit = 10
        self.assertIsNone(self.budget.reserve(ROOM))
        self.budget.mark_sent(ROOM)
        self.budget.mark_sent(ROOM)
        self.budget.human_message(ROOM)
        self.budget.human_message(ROOM)  # 首条发出后群友已说够两条（本测试的清零阈值）
        self.budget.release(ROOM, sent=True)
        self.assertEqual((self.budget.count(ROOM), self.budget.streak(ROOM)), (1, 0))

    def test_configure_keeps_counts(self):
        self.budget.record(ROOM)
        self.budget.configure(limit=1, window_seconds=600, gap_seconds=0, max_streak=5, streak_reset_messages=3)
        self.assertEqual(self.budget.check(ROOM), "quota")


class GenerateTests(unittest.TestCase):
    def model(self, result):
        model = MagicMock()
        model.complete_json.return_value = result
        return model

    def test_returns_text_when_speaking(self):
        self.assertEqual(generate_proactive(self.model({"speak": True, "text": " 我在呢 "}), [(1, "P1", "在吗")]),
                         "我在呢")

    def test_retry_still_similar_gives_up_instead_of_repeating(self):
        model = MagicMock()
        model.complete_json.side_effect = [{"speak": True, "text": "志远你今天又来裸奔了"},
                                           {"speak": True, "text": "志远你今天又来裸奔了"}]
        self.assertEqual(generate_proactive(model, [(1, "P1", "在吗")],
                                            recent_bot=("志远你今天又来裸奔了",)), "")
        self.assertEqual(model.complete_json.call_count, 2)

    def test_declines_and_rejects_bad_output(self):
        for result in ({"speak": False, "text": "不说"}, {"speak": True, "text": ""}, {"speak": True, "text": "@张三 你好"},
                       {"speak": True, "text": "长" * 61}, {"speak": "yes", "text": "嗯"}, ["bad"]):
            with self.subTest(result=result):
                self.assertEqual(generate_proactive(self.model(result), [(1, "P1", "在吗")]), "")


class JevChoiceTests(unittest.TestCase):
    def client(self):
        return JevClient({"base_url": "http://127.0.0.1:3425/v1", "api_key": "k"})

    def respond(self, payload):
        return patch("wxbot_framework.integrations.jev.urlopen",
                     return_value=io.BytesIO(json.dumps(payload).encode("utf-8")))

    def test_parses_declared_options_only(self):
        with self.respond({"answers": {"q0": {"probabilities": {"a": 0.7, "b": 0.3, "extra": 1}}}}) as call:
            result = self.client().choice({"x": 1}, "选一项", {"a": "甲", "b": "乙"})
        self.assertEqual(result, {"a": 0.7, "b": 0.3})
        body = json.loads(call.call_args.args[0].data)
        self.assertEqual(body["questions"]["q0"]["type"], "choice")

    def test_missing_option_or_bad_sum_is_format_error(self):
        for probabilities in ({"a": 1.0}, {"a": 0.9, "b": 0.9}, {"a": 0.2, "b": 0.2}):
            with self.subTest(probabilities=probabilities), \
                    self.respond({"answers": {"q0": {"probabilities": probabilities}}}):
                with self.assertRaises(JevFormatError):
                    self.client().choice({}, "选", {"a": "甲", "b": "乙"})

    def test_sum_tolerates_rounding(self):
        with self.respond({"answers": {"q0": {"probabilities": {"a": 0.5, "b": 0.49}}}}):
            self.assertEqual(self.client().choice({}, "选", {"a": "甲", "b": "乙"}), {"a": 0.5, "b": 0.49})

    def test_invalid_responses_raise_format_error(self):
        for answers in ({}, {"q0": {"probabilities": []}}, {"q0": {"probabilities": {"a": 2}}}):
            with self.subTest(answers=answers), self.respond({"answers": answers}):
                with self.assertRaises(JevFormatError):
                    self.client().choice({}, "选", {"a": "甲", "b": "乙"})

    def test_rejects_bad_arguments(self):
        with self.assertRaises(ValueError):
            self.client().choice({}, "选", {"a": "甲"})
        with self.assertRaises(ValueError):
            self.client().choice({}, "", {"a": "甲", "b": "乙"})


def make_message(text="今天吃什么", *, sender="wxid_a", **data):
    return Message(text, ROOM, sender_id=sender, is_group=True, data=data)


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.plugin = Plugin()
        self.plugin.active = True
        self.plugin.options = read_options({"start_hour": 0, "end_hour": 24})
        self.plugin.members = MagicMock(account=ROBOT, groups={ROOM})
        self.plugin.members.wechat_name.return_value = "小助手"
        self.plugin.members.display_name.return_value = "小助手"
        self.plugin.members.personality.return_value = MagicMock(name="人格", supplement="")
        self.plugin.members.message_id.return_value = 10  # 触发这一轮的消息已落库，序号 10
        self.plugin.room_state = None
        self.clock = Clock()
        self.plugin.clock = self.clock
        self.plugin.budget = SpeakBudget(clock=self.clock)
        self.plugin.planner = JevSpeakPlanner(fake_jev(reply_called=0.9, silent_irrelevant=0.1))
        self.plugin._pending = {}
        self.plugin._versions = {}
        import threading
        self.plugin._lock = threading.Lock()
        self.plugin._stop = threading.Event()
        now = datetime.now(timezone.utc)
        self.plugin.members.recent_rows.return_value = [
            {"sender_id": "wxid_a", "text": "机器人去哪了", "received_at": (now - timedelta(seconds=3)).isoformat()}]
        self.ctx = MagicMock()
        self.ctx.logger = logging.getLogger("ProactiveTests")
        self.ctx.reply.return_value = True
        self.model = MagicMock()
        self.model.complete_json.return_value = {"speak": True, "text": "在这呢"}
        self.ctx.llm_for.return_value = self.model

    def consider(self):
        with patch("plugins.proactive_chat.pick_speech_examples", return_value=[]):
            return self.plugin.consider(ROOM, make_message(), self.ctx)

    def test_reply_sends_once_and_counts_quota(self):
        self.assertTrue(self.consider())
        self.ctx.reply.assert_called_once()
        self.assertEqual(self.ctx.reply.call_args.args[1], "在这呢")
        self.assertEqual(self.plugin.budget.count(ROOM), 1)

    def test_gap_blocks_second_send_before_jev(self):
        self.consider()
        self.plugin.planner.jev.choice.reset_mock()
        self.assertFalse(self.consider())
        self.plugin.planner.jev.choice.assert_not_called()

    def test_skip_and_jev_failure_do_not_generate(self):
        self.plugin.planner.jev.choice.side_effect = JevError("down")
        self.assertFalse(self.consider())
        self.model.complete_json.assert_not_called()
        self.ctx.reply.assert_not_called()

    def test_nothing_to_say_does_not_send(self):
        self.model.complete_json.return_value = {"speak": False, "text": ""}
        self.assertFalse(self.consider())
        self.ctx.reply.assert_not_called()
        self.assertEqual(self.plugin.budget.count(ROOM), 0)

    def test_last_row_from_bot_does_not_judge(self):
        rows = self.plugin.members.recent_rows.return_value
        rows[-1]["bot_generated"] = 1
        self.assertFalse(self.consider())
        self.plugin.planner.jev.choice.assert_not_called()

    def test_newer_message_supersedes_pending_judgement(self):
        def interrupt(*args, **kwargs):
            self.plugin.on_message(make_message("又来一条"), self.ctx)
            return {"reply_called": 0.9, "silent_irrelevant": 0.1}
        self.plugin.planner.jev.choice.side_effect = interrupt
        self.assertFalse(self.consider())
        self.ctx.reply.assert_not_called()

    def test_mention_during_judgement_cancels_round(self):
        # _due 已取出待判断项；艾特时队列里没有这一项，仍要作废这一轮。
        self.plugin.on_message(make_message("一"), self.ctx)
        self.clock.value += 5
        [(room, message, version, recheck)] = self.plugin._due()
        self.assertFalse(recheck)

        def mention(*args, **kwargs):
            self.plugin.on_message(make_message("@小助手 你好", is_at_self=True), self.ctx)
            return {"speak": True, "text": "在这呢"}
        self.model.complete_json.side_effect = mention
        with patch("plugins.proactive_chat.pick_speech_examples", return_value=[]):
            self.assertFalse(self.plugin.consider(room, message, self.ctx, version=version))
        self.ctx.reply.assert_not_called()

    def guarded_reply(self, before_send=None):
        sent = []

        def reply(message, text, *, delivery_guard):
            with delivery_guard():
                if before_send:
                    before_send(len(sent))
                sent.append(text)
            return True
        self.ctx.reply.side_effect = reply
        return sent

    def test_message_just_before_native_send_is_refused_in_guard(self):
        sent = []

        def reply(message, text, *, delivery_guard):
            self.plugin.on_message(make_message("刚好插一句"), self.ctx)  # 最后一次检查之后、守卫之前到达
            with delivery_guard():
                sent.append(text)
            return True
        self.ctx.reply.side_effect = reply
        with self.assertRaises(SnapshotExpired):
            self.consider()
        self.assertEqual(sent, [])
        self.assertEqual(self.plugin.budget.count(ROOM), 0)

    def test_recorded_but_undispatched_message_is_refused_in_guard(self):
        # 消息已落库（id 大于判断时的水位），但插件还没收到分发、版本未变。
        self.plugin.members.latest_id.return_value = 10
        sent = []

        def reply(message, text, *, delivery_guard):
            self.plugin.members.recent_rows.return_value = [
                {"id": 11, "sender_id": "wxid_b", "text": "抢先一句", "received_at": datetime.now(timezone.utc)}]
            with delivery_guard():
                sent.append(text)
            return True
        self.ctx.reply.side_effect = reply
        with self.assertRaises(SnapshotExpired):
            self.consider()
        self.assertEqual(sent, [])

    def test_message_recorded_before_probe_but_undispatched_is_refused_before_jev(self):
        # 序号 11 的新消息在判断开始前就已落库（旧实现的水位会取到 11 而放行），触发消息序号是 10。
        self.plugin.members.latest_id.return_value = 11
        self.plugin.members.recent_rows.return_value = [
            {"id": 11, "sender_id": "wxid_b", "text": "抢先一句", "received_at": datetime.now(timezone.utc)}]
        self.assertFalse(self.consider())
        self.plugin.planner.jev.choice.assert_not_called()
        self.ctx.reply.assert_not_called()
        self.assertEqual(self.plugin.budget.count(ROOM), 0)

    def test_unrecorded_trigger_message_skips_round(self):
        self.plugin.members.message_id.return_value = None
        self.assertFalse(self.consider())
        self.plugin.planner.jev.choice.assert_not_called()
        self.ctx.reply.assert_not_called()

    def test_member_message_waiting_on_guard_lock_counts_after_mark_sent(self):
        # 群友消息落库与计入预算在聊天记录锁内；守卫内发出之后到达的消息排在锁后，不会被 mark_sent 清零。
        import threading
        lock = threading.RLock()
        self.plugin.budget.configure(limit=5, window_seconds=600, gap_seconds=60, max_streak=2,
                                     streak_reset_messages=1)

        @contextmanager
        def response_guard(snapshot, persona):
            with lock:
                yield
        self.ctx.response_guard = response_guard
        arrivals = []

        def reply(message, text, *, delivery_guard):
            with delivery_guard():
                thread = threading.Thread(target=lambda: self._record_human(lock))
                thread.start()
                arrivals.append(thread)
                thread.join(timeout=0.2)
                self.assertTrue(thread.is_alive())  # 守卫仍持锁，群友消息在锁外等待
            return True
        self.ctx.reply.side_effect = reply
        self.assertTrue(self.consider())
        arrivals[0].join(timeout=5)
        self.assertFalse(arrivals[0].is_alive())
        # mark_sent 先于群友消息：连续计数先 +1 再被这条消息清零（阈值 1），配额记 1 次。
        self.assertEqual((self.plugin.budget.count(ROOM), self.plugin.budget.streak(ROOM)), (1, 0))

    def _record_human(self, lock):
        with lock:
            self.plugin.budget.human_message(ROOM)

    def test_split_send_exception_after_first_part_still_counts(self):
        self.model.complete_json.return_value = {"speak": True, "text": "在这呢。刚才去吃饭了。"}

        def fail_second(count):
            if count == 1:
                raise SnapshotExpired("清空")
        sent = self.guarded_reply(fail_second)
        with patch("wxbot_framework.conversation.utterance.time.sleep"), self.assertRaises(SnapshotExpired):
            self.consider()
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.plugin.budget.count(ROOM), 1)
        self.assertEqual(self.plugin.budget.check(ROOM), "gap")

    def test_unload_during_generation_does_not_send(self):
        def unload(*args, **kwargs):
            self.plugin._stop.set()
            return {"speak": True, "text": "在这呢"}
        self.model.complete_json.side_effect = unload
        self.assertFalse(self.consider())
        self.ctx.reply.assert_not_called()

    def test_shared_budget_in_flight_blocks_second_speaker(self):
        self.assertIsNone(self.plugin.budget.reserve(ROOM))  # 定时主动发言正在本群发送
        self.assertFalse(self.consider())
        self.ctx.reply.assert_not_called()
        self.plugin.budget.release(ROOM, sent=False)
        self.assertTrue(self.consider())

    def test_debounce_waits_for_quiet_or_max_wait(self):
        self.plugin.on_message(make_message("一"), self.ctx)
        self.assertEqual(self.plugin._due(), [])
        self.clock.value += 2
        self.plugin.on_message(make_message("二"), self.ctx)
        self.clock.value += 2
        self.assertEqual(self.plugin._due(), [])
        self.clock.value += 1.5
        due = self.plugin._due()
        self.assertEqual([(room, message.text) for room, message, _, _ in due], [(ROOM, "二")])
        for _ in range(5):
            self.plugin.on_message(make_message("刷"), self.ctx)
            self.clock.value += 2
        self.assertEqual(len(self.plugin._due()), 1)  # 首条起满 8 秒即判断，不无限推迟。

    def test_mention_bot_and_commands_are_not_queued(self):
        self.plugin.on_message(make_message("一"), self.ctx)
        self.plugin.on_message(make_message("@小助手 你好", is_at_self=True), self.ctx)
        self.plugin.on_message(make_message("/help"), self.ctx)
        self.plugin.on_message(make_message("我说的", bot_generated=True), self.ctx)
        self.clock.value += 10
        self.assertEqual(self.plugin._due(), [])

    def test_wait_schedules_one_recheck_then_gives_up(self):
        self.plugin.options = read_options({"start_hour": 0, "end_hour": 24, "wait_recheck_seconds": 20})
        self.plugin.planner = JevSpeakPlanner(fake_jev(reply_called=0.18, silent_irrelevant=0.82))
        self.assertFalse(self.consider())
        self.ctx.reply.assert_not_called()
        self.assertEqual(self.plugin._due(), [])  # 复判时刻还没到
        self.clock.value += 21
        [(room, message, version, recheck)] = self.plugin._due()
        self.assertTrue(recheck)
        self.plugin.planner.jev.choice.reset_mock()
        self.assertFalse(self.plugin.consider(room, message, self.ctx, version=version, recheck=True))
        self.assertEqual(self.plugin.planner.jev.choice.call_count, 1)  # 复判又得 WAIT：不再排下一轮
        self.clock.value += 60
        self.assertEqual(self.plugin._due(), [])

    def test_new_message_cancels_pending_recheck(self):
        self.plugin.options = read_options({"start_hour": 0, "end_hour": 24, "wait_recheck_seconds": 20})
        self.plugin.planner = JevSpeakPlanner(fake_jev(reply_called=0.18, silent_irrelevant=0.82))
        self.assertFalse(self.consider())
        self.plugin.on_message(make_message("我又说一句"), self.ctx)
        self.clock.value += 21
        due = self.plugin._due()
        self.assertEqual([(message.text, recheck) for _, message, _, recheck in due], [("我又说一句", False)])

    def test_recheck_does_not_send_when_budget_refuses(self):
        self.plugin.options = read_options({"start_hour": 0, "end_hour": 24, "wait_recheck_seconds": 20})
        self.plugin.planner = JevSpeakPlanner(fake_jev(reply_called=0.18, silent_irrelevant=0.82))
        self.assertFalse(self.consider())
        self.plugin.budget.record(ROOM)  # 别的主动发言刚发过，间隔未满
        self.clock.value += 21
        [(room, message, version, _)] = self.plugin._due()
        self.plugin.planner.jev.choice.reset_mock()
        self.assertFalse(self.plugin.consider(room, message, self.ctx, version=version, recheck=True))
        self.plugin.planner.jev.choice.assert_not_called()

    def test_defaults_lower_threshold_and_open_the_whole_day(self):
        options = read_options({})
        self.assertEqual((options["threshold_percent"], options["start_hour"], options["end_hour"]), (25, 0, 24))
        self.assertEqual(options["wait_recheck_seconds"], 20)

    def test_recheck_seconds_is_validated(self):
        for value in (4, 121, "20"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                read_options({"wait_recheck_seconds": value})

    def test_disabled_by_default_starts_no_thread(self):
        plugin, ctx = Plugin(), MagicMock(config={})
        plugin.on_load(ctx)
        self.assertIsNone(plugin._thread)
        plugin.on_message(make_message(), ctx)
        plugin.on_unload(ctx)
        ctx.jev.choice.assert_not_called()

    def test_options_are_validated(self):
        for config in ({"limit": 0}, {"threshold_percent": "60"}, {"start_hour": 10, "end_hour": 9},
                       {"rooms": "x"}, {"quiet_seconds": 10, "max_wait_seconds": 5}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                read_options(config)


if __name__ == "__main__":
    unittest.main()
