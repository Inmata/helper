import random
import unittest
from contextlib import contextmanager

from wxbot_framework.conversation.utterance import (
    CHAT, DETAIL, QA, InterjectionGate, Interjected, SpeakResult, classify_tier, chat_parts, gap_seconds,
    interjection_probe, plan_utterance, speak, split_sentences)


class FakeMembers:
    """按调用次数返回预设的群消息序号和最近行；全部为虚构数据。"""

    def __init__(self, baseline=10, rows_by_call=()):
        self.baseline, self.rows_by_call, self.calls = baseline, list(rows_by_call), 0

    def latest_id(self, room):
        return self.baseline

    def recent_rows(self, room, **options):
        rows = self.rows_by_call[min(self.calls, len(self.rows_by_call) - 1)] if self.rows_by_call else []
        self.calls += 1
        return rows


class TierTests(unittest.TestCase):
    def test_detail_flag_wins(self):
        self.assertEqual(classify_tier("为什么会这样？", detail=True), DETAIL)

    def test_question_signals_are_qa(self):
        for query in ("今天几点开会", "这个怎么弄", "为什么没人说话", "你吃了吗", "是什么东西?", "哪个好"):
            with self.subTest(query=query):
                self.assertEqual(classify_tier(query), QA)

    def test_request_phrases_without_question_words_are_qa(self):
        for query in ("解释一下 TCP", "介绍下这个功能", "讲讲这个梗", "说说你的看法", "告诉我密码规则", "推荐点吃的",
                      "帮我想个名字", "对比一下两个方案", "科普下量子", "分析一下", "总结今天的聊天", "讲一下流程"):
            with self.subTest(query=query):
                self.assertEqual(classify_tier(query), QA)

    def test_banter_is_chat(self):
        for query in ("哈哈哈笑死", "晚安", "今天好累", ""):
            with self.subTest(query=query):
                self.assertEqual(classify_tier(query), CHAT)


class SplitTests(unittest.TestCase):
    def test_short_banter_splits_into_two_or_three(self):
        self.assertEqual(chat_parts("行吧。那我先睡了。"), ["行吧", "那我先睡了"])
        self.assertEqual(chat_parts("哈哈！真的假的？我不信。"), ["哈哈！", "真的假的？", "我不信"])

    def test_never_more_than_three_parts(self):
        self.assertIsNone(chat_parts("一。二。三。四。"))

    def test_single_sentence_or_too_long_is_not_split(self):
        self.assertIsNone(chat_parts("就这样吧"))
        self.assertIsNone(chat_parts("这是一句很长的话" * 3 + "。第二句。"))
        self.assertIsNone(chat_parts("甲" * 15 + "。" + "乙" * 15 + "。" + "丙" * 15 + "。"))  # 总长 >40

    def test_single_sentence_falls_back_to_comma_split(self):
        self.assertEqual(chat_parts("行吧，那我先睡了"), ["行吧", "那我先睡了"])
        self.assertIsNone(chat_parts("甲" * 21 + "，好"))  # 逗号段同样受每句 20 字限制
        self.assertIsNone(chat_parts("一，二，三，四"))
        # 已有句末标点时不再按逗号细分
        self.assertEqual(chat_parts("好，行。睡了。"), ["好，行", "睡了"])

    def test_sentence_longer_than_twenty_blocks_split(self):
        self.assertIsNone(chat_parts("甲" * 21 + "。好。"))

    def test_punctuation_runs_stay_with_sentence(self):
        self.assertEqual(split_sentences("真的吗？！好吧。"), ["真的吗？！", "好吧。"])

    def test_plan_chat_splits_and_only_first_carries_at(self):
        plan = plan_utterance("行吧。那我先睡了。", CHAT, byte_budget=1000)
        self.assertEqual(plan.parts, ("行吧", "那我先睡了"))
        self.assertFalse(plan.at_all)
        self.assertTrue(plan.paced)

    def test_plan_qa_never_splits_even_with_sentence_ends(self):
        plan = plan_utterance("可以。先点设置。再点开启。", QA, byte_budget=1000)
        self.assertEqual(plan.parts, ("可以。先点设置。再点开启。",))

    def test_plan_detail_segments_by_byte_budget_without_limit_on_count(self):
        text = "\n".join(f"第{index}行" + "字" * 60 for index in range(40))
        plan = plan_utterance(text, DETAIL, byte_budget=300)
        self.assertGreater(len(plan.parts), 3)
        self.assertTrue(all(len(part.encode("utf-8")) <= 300 for part in plan.parts))
        self.assertEqual("".join(plan.parts).replace("\n", ""), text.replace("\n", ""))
        self.assertTrue(plan.at_all)
        self.assertFalse(plan.paced)

    def test_oversized_non_detail_reply_is_segmented_not_truncated(self):
        text = "中" * 1200 + "[TAIL]"
        plan = plan_utterance(text, QA, byte_budget=1000)
        self.assertGreater(len(plan.parts), 1)
        self.assertEqual("".join(plan.parts), text)


class GapTests(unittest.TestCase):
    def test_gap_target_stays_within_one_to_three_seconds(self):
        rng = random.Random(7)
        for text in ("好", "哈" * 8, "哈" * 20, "哈" * 40):
            for _ in range(50):
                gap = gap_seconds(text, rng)
                self.assertGreaterEqual(gap, 1.0)
                self.assertLessEqual(gap, 3.0)

    def test_longer_text_waits_at_least_as_long(self):
        rng_short, rng_long = random.Random(1), random.Random(1)
        self.assertLessEqual(gap_seconds("短", rng_short), gap_seconds("长" * 18, rng_long) + 1.0)


class SpeakTests(unittest.TestCase):
    def collect(self):
        sent = []
        return sent, (lambda part: sent.append(("at", part)) or True), (lambda part: sent.append(("plain", part)) or True)

    def test_chat_split_first_at_then_plain_with_gaps(self):
        sent, send_at, send_plain = self.collect()
        waits = []
        result = speak("行吧。那我先睡了。", tier=CHAT, byte_budget=1000, send_at=send_at, send_plain=send_plain,
                       interjected=lambda: False, sleep=waits.append, rng=random.Random(3))
        self.assertEqual(sent, [("at", "行吧"), ("plain", "那我先睡了")])
        self.assertEqual(len(waits), 1)
        self.assertFalse(result.interrupted)

    def test_gap_runs_from_previous_return_to_next_call_within_one_to_three_seconds(self):
        now = [100.0]
        waits, events = [], []

        def sleep(seconds):
            waits.append(seconds)
            now[0] += seconds

        def send(part):
            events.append((part, now[0]))
            now[0] += 5.0  # 发送函数本身耗时（含发送层的打字节奏），不应计入条间隔
            return True

        for seed in range(30):
            waits.clear(), events.clear()
            speak("哈哈！真的假的？我不信。", tier=CHAT, byte_budget=1000, send_at=send, interjected=lambda: False,
                  sleep=sleep, rng=random.Random(seed), clock=lambda: now[0])
            self.assertEqual(len(waits), 2)
            for wait in waits:
                self.assertGreaterEqual(wait, 1.0)
                self.assertLessEqual(wait, 3.0)
            # 下一条被调用的时刻 = 上一条返回时刻 + 等待值。
            self.assertAlmostEqual(events[1][1] - (events[0][1] + 5.0), waits[0])

    def test_gap_only_waits_the_remaining_time(self):
        waits = []
        readings = iter([10.0, 10.4, 11.0])  # 首条返回时刻、计算剩余等待时的时刻、末条返回时刻

        class FixedRng:
            def uniform(self, low, high):
                return 0.0

        speak("行吧。那我先睡了。", tier=CHAT, byte_budget=1000, send_at=lambda part: True,
              interjected=lambda: False, sleep=waits.append, rng=FixedRng(), clock=lambda: next(readings))
        # 目标值 = max(1, 0.8 + 0.1 * 5) = 1.3，已过去 0.4 秒，只补足 0.9 秒。
        self.assertEqual(len(waits), 1)
        self.assertAlmostEqual(waits[0], 0.9)

    def test_no_wait_when_the_gap_has_already_passed(self):
        now = [0.0]
        waits = []

        def send(part):
            return True

        def clock():
            value = now[0]
            now[0] += 10.0  # 每次读时钟前进 10 秒，剩余时间为负
            return value

        speak("行吧。那我先睡了。", tier=CHAT, byte_budget=1000, send_at=send, interjected=lambda: False,
              sleep=waits.append, clock=clock)
        self.assertEqual(waits, [])

    def test_qa_is_a_single_message_with_no_wait(self):
        sent, send_at, send_plain = self.collect()
        waits = []
        speak("可以。先点设置。", tier=QA, byte_budget=1000, send_at=send_at, send_plain=send_plain,
              sleep=waits.append)
        self.assertEqual(sent, [("at", "可以。先点设置。")])
        self.assertEqual(waits, [])

    def test_detail_sends_every_segment_with_at(self):
        sent, send_at, send_plain = self.collect()
        text = "\n".join("行" + "字" * 80 for _ in range(10))
        speak(text, tier=DETAIL, byte_budget=300, send_at=send_at, send_plain=send_plain, sleep=lambda s: self.fail())
        self.assertGreater(len(sent), 1)
        self.assertTrue(all(kind == "at" for kind, _ in sent))

    def test_interjection_drops_remaining_parts(self):
        sent, send_at, send_plain = self.collect()
        flags = iter([False, True])
        result = speak("哈哈！真的假的？我不信。", tier=CHAT, byte_budget=1000, send_at=send_at,
                       send_plain=send_plain, interjected=lambda: next(flags), sleep=lambda s: None)
        self.assertEqual([part for _, part in sent], ["哈哈！", "真的假的？"])
        self.assertEqual(result.dropped, 1)
        self.assertTrue(result.interrupted)

    def test_interjection_before_second_part_drops_all_remaining(self):
        sent, send_at, send_plain = self.collect()
        result = speak("哈哈！真的假的？我不信。", tier=CHAT, byte_budget=1000, send_at=send_at,
                       send_plain=send_plain, interjected=lambda: True, sleep=lambda s: None)
        self.assertEqual([part for _, part in sent], ["哈哈！"])
        self.assertEqual(result.dropped, 2)

    def test_first_send_failure_stops_and_reports(self):
        calls = []
        result = speak("行吧。那我先睡了。", tier=CHAT, byte_budget=1000,
                       send_at=lambda part: calls.append(part) or False, sleep=lambda s: None)
        self.assertTrue(result.failed)
        self.assertEqual(calls, ["行吧"])
        self.assertEqual(result.sent, [])

    def test_guard_exception_propagates_and_already_sent_parts_are_not_resent(self):
        sent = []

        def send(part):
            if sent:
                raise RuntimeError("守卫拒绝")
            sent.append(part)
            return True

        with self.assertRaises(RuntimeError):
            speak("行吧。那我先睡了。", tier=CHAT, byte_budget=1000, send_at=send, interjected=lambda: False,
                  sleep=lambda s: None)
        self.assertEqual(sent, ["行吧"])

    def test_interjected_guard_inside_send_counts_as_dropped_not_failed(self):
        sent = []

        def send(part):
            if sent:
                raise Interjected
            sent.append(part)
            return True

        result = speak("哈哈！真的假的？我不信。", tier=CHAT, byte_budget=1000, send_at=send, interjected=lambda: False,
                       sleep=lambda s: None)
        self.assertEqual(sent, ["哈哈！"])
        self.assertEqual(result.dropped, 2)
        self.assertFalse(result.failed)
        self.assertTrue(result.interrupted)

    def test_gate_arms_after_first_part_and_is_checked_again_inside_the_guard(self):
        entered = []

        @contextmanager
        def base_guard():
            entered.append("enter")
            yield

        flag = [False]
        gate = InterjectionGate(lambda: flag[0])
        guard = gate.wrap(base_guard)
        calls = []

        def send(part):
            with guard():
                calls.append(part)
                if len(calls) == 1:
                    flag[0] = True  # 首条发出后才有人说话
                return True

        result = SpeakResult()
        speak("哈哈！真的假的？我不信。", tier=CHAT, byte_budget=1000, send_at=send, interjected=gate,
              sleep=lambda s: None, result=result)
        # 首条发出后，等待结束的预检查就发现了插话，第二条根本没有调用发送函数。
        self.assertEqual(calls, ["哈哈！"])
        self.assertEqual(result.dropped, 2)

    def test_gate_inside_guard_catches_interjection_arriving_after_the_precheck(self):
        gate = InterjectionGate(lambda: True)
        gate.arm()

        @contextmanager
        def base_guard():
            yield

        with self.assertRaises(Interjected):
            with gate.wrap(base_guard)():
                self.fail("守卫内不应继续发送")

    def test_gate_is_inert_until_armed_and_propagates_base_guard_errors(self):
        gate = InterjectionGate(lambda: True)
        self.assertFalse(gate())

        @contextmanager
        def base_guard():
            yield

        with gate.wrap(base_guard)():
            pass  # 未 arm 时首条不受影响

        @contextmanager
        def expired():
            raise RuntimeError("守卫拒绝")
            yield

        gate.arm()
        with self.assertRaises(RuntimeError):
            with gate.wrap(expired)():
                pass

    def test_guard_runs_for_every_segment(self):
        guarded = []
        speak("哈哈！真的假的？我不信。", tier=CHAT, byte_budget=1000,
              send_at=lambda part: guarded.append(part) or True, interjected=lambda: False, sleep=lambda s: None)
        self.assertEqual(len(guarded), 3)


class InterjectionProbeTests(unittest.TestCase):
    def test_new_member_message_counts(self):
        members = FakeMembers(10, [[{"id": 11, "bot_generated": 0, "is_self": 0}]])
        self.assertTrue(interjection_probe(members, "r@chatroom")())

    def test_old_messages_and_bot_echo_do_not_count(self):
        rows = [{"id": 9, "bot_generated": 0, "is_self": 0}, {"id": 12, "bot_generated": 1, "is_self": 1},
                {"id": 13, "bot_generated": 0, "is_self": 1}]
        self.assertFalse(interjection_probe(FakeMembers(10, [rows]), "r@chatroom")())

    def test_read_errors_mean_no_interjection(self):
        class Broken:
            def latest_id(self, room):
                return 1

            def recent_rows(self, room, **options):
                raise RuntimeError("库不可用")

        self.assertFalse(interjection_probe(Broken(), "r@chatroom")())

    def test_unreadable_baseline_means_no_interjection(self):
        class NoBaseline:
            def latest_id(self, room):
                raise RuntimeError("库不可用")

            def recent_rows(self, room, **options):
                return [{"id": 99, "bot_generated": 0, "is_self": 0}]

        self.assertFalse(interjection_probe(NoBaseline(), "r@chatroom")())

    def test_explicit_baseline_overrides_latest_id(self):
        members = FakeMembers(11, [[{"id": 11, "bot_generated": 0, "is_self": 0}]])
        self.assertFalse(interjection_probe(members, "r@chatroom")())  # 默认水位取 latest_id=11
        members = FakeMembers(11, [[{"id": 11, "bot_generated": 0, "is_self": 0}]])
        self.assertTrue(interjection_probe(members, "r@chatroom", baseline=10)())

    def test_magicmock_members_never_report_interjection(self):
        from unittest.mock import MagicMock
        self.assertFalse(interjection_probe(MagicMock(), "r@chatroom")())


if __name__ == "__main__":
    unittest.main()
