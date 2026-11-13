import unittest
from tests import test_mention_tools
from tests.test_mention_tools import FakeJev, FakeMagpie
from tests.test_mention_chat import mention
from wxbot_framework.conversation.context_tools import MentionContext
from wxbot_framework.conversation.members import ConversationSnapshot
from wxbot_framework.conversation.reply_review import reply_evidence, review_reply
from wxbot_framework.conversation.tool_router import REJECTED, UNAVAILABLE


class ReviewJev(FakeJev):
    def __init__(self, answers):
        super().__init__()
        self.answers = iter(answers)
        self.reviews = []

    def noul(self, state, questions, *, timeout=None):
        if len(questions) != 3:
            return super().noul(state, questions, timeout=timeout)
        self.reviews.append((state, questions, timeout))
        result = next(self.answers)
        if isinstance(result, Exception):
            raise result
        return result


class ReplyReviewTests(unittest.TestCase):
    def test_evidence_retains_speakers_dates_bot_roles_and_memory_status(self):
        own = {"id": 1, "received_at": "2026-11-13T12:00:00Z", "text": "我买了鸡蛋"}
        other = {"id": 2, "received_at": "2026-11-13T12:01:00Z", "speaker_name": "小红", "speaker": "P2",
                 "text": "你买的多少钱"}
        bot = {"id": 3, "received_at": "2026-11-13T12:02:00Z", "speaker_name": "机器人",
               "bot_generated": 1, "text": "小红买了鸡蛋"}
        snapshot = ConversationSnapshot("1@chatroom", "2026-11-13", 0, "小明", (own,), "谁买了鸡蛋",
                                        (other,), history=({**own, "speaker": "P1", "speaker_name": "小明"}, other, bot),
                                        speaker_tag="P1", participants=(("P1", "小明", ()), ("P2", "小红", ("红哥",))))
        memory = {"name": "小红", "kind": "群内玩笑", "content": "以前买蛋摔了一跤", "updated_at": "2025-01-01"}
        context = MentionContext([memory], None, {"name": "小明", "bound": True}, [], [])
        evidence = reply_evidence(snapshot, context, quote={"name": "机器人", "text": bot["text"]}, quote_is_bot=True)
        self.assertEqual(len(evidence["聊天原文"]), 3)
        self.assertEqual(evidence["聊天原文"][0]["编号"], "P1")
        self.assertEqual(evidence["聊天原文"][1]["发言者"], "小红")
        self.assertEqual(evidence["聊天原文"][2]["角色"], "机器人")
        self.assertEqual(evidence["历史记忆"], [memory])
        self.assertEqual(evidence["本次引用"]["角色"], "机器人")

    def test_three_judgments_share_one_bounded_request(self):
        jev = ReviewJev([[0.01, 0.98, 0.9]])
        result = review_reply(jev, "这书评分多少", "评分9.8", {"聊天原文": []})
        self.assertEqual(result.status, REJECTED)
        self.assertTrue(result.needs_search)
        self.assertEqual(len(jev.reviews), 1)
        self.assertEqual(jev.reviews[0][2], 2)

    def test_service_failure_is_distinguishable(self):
        self.assertEqual(review_reply(None, "你好", "你好", {}).status, UNAVAILABLE)
        result = review_reply(ReviewJev([TimeoutError()]), "你好", "你好", {})
        self.assertEqual((result.status, result.error), (UNAVAILABLE, "TimeoutError"))

    def test_local_error_does_not_trigger_search_for_an_unrelated_public_need(self):
        result = review_reply(ReviewJev([[0.98, 0.01, 0.99]]), "这价格多少，小红刚才说啥", "小红刚才报价了", {})
        self.assertEqual(result.status, REJECTED)
        self.assertFalse(result.needs_search)


class OrdinaryReplyTests(unittest.TestCase):
    build = test_mention_tools.MentionChatToolTests.build

    def test_wrong_local_attribution_is_rewritten_before_sending_without_search(self):
        jev = ReviewJev([[0.98, 0.01, 0.01], [0.03, 0.01, 0.01]])
        plugin, ctx = self.build(["小红刚才回了俩问号。", "你又想让我点名呀。"], jev=jev)
        plugin.on_message(mention("群里谁最容易急眼"), ctx)
        self.assertEqual(ctx.sent, [("reply_at", "你又想让我点名呀。")])
        self.assertEqual(ctx.magpie.queries, [])
        self.assertIn("待修订候选", ctx.last_user)
        self.assertIn("小红刚才回了俩问号", ctx.last_user)

    def test_extra_public_consensus_is_removed_without_search(self):
        plugin, ctx = self.build(["很多人觉得这书战力崩了。", "你觉得最拉的是哪一段？"],
                                 jev=ReviewJev([[0.01, 0.98, 0.08], [0.01, 0.05, 0.03]]))
        plugin.on_message(mention("这书后半段是不是拉完了"), ctx)
        self.assertEqual(ctx.magpie.queries, [])
        self.assertEqual(ctx.sent, [("reply_at", "你觉得最拉的是哪一段？")])

    def test_missing_essential_public_fact_triggers_search_and_existing_verification(self):
        jev = ReviewJev([[0.01, 0.98, 0.98]])
        plugin, ctx = self.build(["这书评分9.8。", "这本书 官方评分 最新数据", "这个站显示是7.2分。"],
                                 jev=jev, magpie=FakeMagpie(search="官网本书评分7.2分。"))
        plugin.on_message(mention("这本书评分多少"), ctx)
        self.assertEqual(ctx.magpie.queries, ["这本书 官方评分 最新数据"])
        self.assertEqual(ctx.sent, [("reply_at", "这个站显示是7.2分。")])
        self.assertIn("【联网搜索结果】", ctx.last_user)
        self.assertEqual(ctx.completions, 3)

    def test_failed_search_is_not_repeated_and_reply_can_ask_for_missing_material(self):
        jev = ReviewJev([[0.01, 0.98, 0.98], [0.01, 0.04, 0.05]])
        plugin, ctx = self.build(["评分9.8。", "这本书 评分", "你说的是哪个评分网站？"],
                                 jev=jev, magpie=FakeMagpie(error=TimeoutError()))
        plugin.on_message(mention("这本书评分多少"), ctx)
        self.assertEqual(len(ctx.magpie.queries), 1)
        self.assertEqual(ctx.sent, [("reply_at", "你说的是哪个评分网站？")])

    def test_repeatedly_unsupported_reply_is_never_sent(self):
        jev = ReviewJev([[0.98, 0.01, 0.01]] * 3)
        plugin, ctx = self.build("小红刚才回了俩问号。", jev=jev)
        with self.assertLogs("MentionTests", level="WARNING") as logs:
            plugin.on_message(mention("谁刚才急眼了"), ctx)
        self.assertIn("两次修订后仍缺少依据", logs.output[0])
        self.assertEqual(ctx.sent, [])
        self.assertEqual(ctx.completions, 3)

    def test_unavailable_review_keeps_normal_reply_and_does_not_start_a_search(self):
        plugin, ctx = self.build("你好呀。", jev=ReviewJev([TimeoutError()]))
        plugin.on_message(mention("你好"), ctx)
        self.assertEqual(ctx.sent, [("reply_at", "你好呀。")])
        self.assertEqual(ctx.magpie.queries, [])


if __name__ == "__main__":
    unittest.main()
