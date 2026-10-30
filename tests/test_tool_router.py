"""Jev 看图 / 搜索判断的离线验证：独立决策、阈值、失败关闭与每轮预算。"""
import unittest

from wxbot_framework.conversation.tool_router import (ACCEPTED, BUDGET_EXHAUSTED, NOT_APPLICABLE, REJECTED,
                                                      SEARCH, UNAVAILABLE, VISION, JevToolRouter, ToolTurn,
                                                      read_options)
from wxbot_framework.integrations.jev import JevError


class FakeJev:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def noul(self, state, questions):
        self.calls.append((state, questions))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class DecisionTests(unittest.TestCase):
    def test_default_thresholds_are_the_evaluated_values(self):
        router = JevToolRouter(FakeJev())
        self.assertEqual((router.vision_threshold, router.search_threshold), (0.6, 0.7))

    def test_threshold_is_configurable_per_tool(self):
        router = JevToolRouter(FakeJev([0.4], [0.39], [0.7]), vision_threshold=0.4, search_threshold=0.6)
        self.assertEqual(router.decide_vision(question="图里是什么", has_image=True).status, ACCEPTED)
        self.assertEqual(router.decide_vision(question="图里是什么", has_image=True).status, REJECTED)
        self.assertEqual(router.decide_search(question="今天有什么新闻").status, ACCEPTED)

    def test_decision_carries_probability_and_threshold(self):
        decision = JevToolRouter(FakeJev([0.62])).decide_vision(question="图里写了什么", has_image=True)
        self.assertEqual((decision.tool, decision.use, decision.probability, decision.threshold),
                         (VISION, True, 0.62, 0.6))
        self.assertEqual(decision.error, None)

    def test_vision_state_and_question_shape(self):
        jev = FakeJev([0.9])
        JevToolRouter(jev).decide_vision(question=" 这张截图报了什么错 ", has_image=True)
        state, questions = jev.calls[0]
        self.assertEqual(state, {"提问": "这张截图报了什么错"})
        self.assertEqual(len(questions), 1)
        self.assertIn("图片", questions[0]["true"])
        self.assertTrue(questions[0]["instructions"])

    def test_no_image_skips_jev(self):
        jev = FakeJev()
        decision = JevToolRouter(jev).decide_vision(question="在吗", has_image=False)
        self.assertEqual(decision.status, NOT_APPLICABLE)
        self.assertFalse(decision.use)
        self.assertEqual(jev.calls, [])

    def test_search_uses_vision_text_as_bounded_context(self):
        jev = FakeJev([0.8])
        decision = JevToolRouter(jev).decide_search(question="这个价格现在多少", vision_text="图" * 1000)
        state, _ = jev.calls[0]
        self.assertEqual(len(state["看图结果"]), 600)
        self.assertEqual(decision.status, ACCEPTED)

    def test_search_without_vision_has_no_vision_field(self):
        jev = FakeJev([0.6])
        JevToolRouter(jev).decide_search(question="今天有什么新闻")
        state, _ = jev.calls[0]
        self.assertEqual(state, {"提问": "今天有什么新闻"})

    def test_two_decisions_are_independent_when_vision_fails(self):
        jev = FakeJev(JevError("超时"), [0.9])
        router = JevToolRouter(jev)
        vision = router.decide_vision(question="图里是什么", has_image=True)
        search = router.decide_search(question="今天有什么新闻")
        self.assertEqual((vision.status, vision.use, vision.error), (UNAVAILABLE, False, "JevError"))
        self.assertEqual((search.status, search.use), (ACCEPTED, True))
        self.assertEqual(len(jev.calls), 2)

    def test_search_failure_after_successful_vision_is_isolated(self):
        jev = FakeJev([0.9])
        router = JevToolRouter(jev)
        vision = router.decide_vision(question="图里是什么", has_image=True)
        search = router.decide_search(question="今天有什么新闻")
        self.assertEqual(vision.status, ACCEPTED)
        self.assertEqual(search.status, UNAVAILABLE)
        self.assertEqual(search.error, "IndexError")
        self.assertFalse(search.use)

    def test_missing_jev_is_fail_closed_and_distinguishable(self):
        for decision in (JevToolRouter(None).decide_vision(question="图", has_image=True),
                         JevToolRouter(None).decide_search(question="新闻")):
            self.assertEqual((decision.status, decision.use, decision.error),
                             (UNAVAILABLE, False, "not-configured"))

    def test_invalid_probabilities_are_fail_closed(self):
        for value in ([[0.5]], ["0.5"], [float("nan")], [1.2], [True], [], 0.9, None):
            with self.subTest(value=value):
                decision = JevToolRouter(FakeJev(value)).decide_search(question="查询")
                self.assertEqual((decision.status, decision.use, decision.error),
                                 (UNAVAILABLE, False, "invalid-probability"))

    def test_question_and_flag_validation(self):
        router = JevToolRouter(FakeJev())
        for question in ("", "   ", None, 1, "x" * 2001):
            with self.subTest(question=question), self.assertRaises(ValueError):
                router.decide_search(question=question)
            with self.subTest(question=question), self.assertRaises(ValueError):
                router.decide_vision(question=question, has_image=True)
        for value in ("yes", 1, None):
            with self.subTest(has_image=value), self.assertRaises(ValueError):
                router.decide_vision(question="图", has_image=value)


class TurnTests(unittest.TestCase):
    def test_each_tool_is_decided_once_per_turn(self):
        jev = FakeJev([0.9], [0.8])
        turn = ToolTurn(JevToolRouter(jev), question="图里是什么？现在多少钱？", has_image=True)
        self.assertEqual(turn.consider_vision().status, ACCEPTED)
        again = turn.consider_vision()
        self.assertEqual((again.status, again.use), (BUDGET_EXHAUSTED, False))
        self.assertEqual(len(jev.calls), 1)
        turn.record_vision("图里是一只猫")
        self.assertEqual(turn.consider_search().status, ACCEPTED)
        self.assertEqual(turn.consider_search().status, BUDGET_EXHAUSTED)
        self.assertEqual(len(jev.calls), 2)
        turn.record_search("搜索结果")
        self.assertTrue(turn.vision_used)
        self.assertTrue(turn.search_used)

    def test_vision_result_feeds_the_search_decision(self):
        jev = FakeJev([0.9], [0.8])
        turn = ToolTurn(JevToolRouter(jev), question="这个多少钱？", has_image=True)
        turn.consider_vision()
        turn.record_vision("商品标签写着 199 元")
        turn.consider_search()
        state, _ = jev.calls[1]
        self.assertEqual(state["看图结果"], "商品标签写着 199 元")

    def test_search_is_available_without_an_image(self):
        jev = FakeJev([0.8])
        turn = ToolTurn(JevToolRouter(jev), question="今天有什么新闻", has_image=False)
        self.assertEqual(turn.consider_vision().status, NOT_APPLICABLE)
        self.assertEqual(turn.consider_search().status, ACCEPTED)
        self.assertNotIn("看图结果", jev.calls[0][0])

    def test_second_record_is_a_caller_error(self):
        turn = ToolTurn(JevToolRouter(FakeJev()), question="提问", has_image=False)
        turn.record_vision("第一次")
        with self.assertRaises(ValueError):
            turn.record_vision("第二次")
        turn.record_search("第一次")
        with self.assertRaises(ValueError):
            turn.record_search("第二次")

    def test_records_must_be_text(self):
        turn = ToolTurn(JevToolRouter(FakeJev()), question="提问", has_image=False)
        for value in (None, 1, ["x"]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                turn.record_vision(value)

    def test_turn_validation(self):
        for kwargs in ({"question": "", "has_image": False}, {"question": "提问", "has_image": 1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ToolTurn(JevToolRouter(FakeJev()), **kwargs)


class OptionTests(unittest.TestCase):
    def test_read_options_defaults_and_overrides(self):
        self.assertEqual(read_options(None), {"vision_threshold": 0.6, "search_threshold": 0.7})
        self.assertEqual(read_options({"vision_threshold": 0.7, "search_threshold": 0}),
                         {"vision_threshold": 0.7, "search_threshold": 0.0})

    def test_invalid_options(self):
        for options in ([], {"vision_threshold": True}, {"search_threshold": -0.1},
                        {"vision_threshold": 1.1}, {"search_threshold": float("nan")},
                        {"vision_threshold": "0.5"}, {"search_threshold": None}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                read_options(options)

    def test_constructor_validates_thresholds(self):
        for kwargs in ({"vision_threshold": True}, {"search_threshold": -1},
                       {"vision_threshold": 2}, {"search_threshold": float("inf")}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                JevToolRouter(FakeJev(), **kwargs)


if __name__ == "__main__":
    unittest.main()
