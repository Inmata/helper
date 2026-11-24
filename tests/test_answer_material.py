"""真实历史、工具选择、公开状态核对和发送登记的回归。"""
import json
import threading
import unittest
from unittest.mock import Mock, patch

import test_image_generation as images_test
from wxbot_framework.conversation.mention_tools import MentionTools, ToolResult, read_tool_options
from wxbot_framework.conversation.reply_wait import ReplyWait
from wxbot_framework.integrations.reply_budget import CURRENT, ReplyBudget, ReplyDraft, remaining


ROOM = images_test.ROOM


class ChatEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = images_test.ImageGenerationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.ctx = self.fixture.ctx
        self.plugin = self.fixture.plugin

    def _reply(self, message, question):
        for _ in self.plugin._reply_once(message, self.ctx, question, None):
            pass

    def test_short_disagreement_checks_updated_public_status_and_blocks_stale_draft(self):
        self.plugin.tools = read_tool_options({})
        self.fixture.message("听说某公开公司的 CEO 已经卸任？")
        self.fixture.members.record_bot_reply(ROOM, "没走，他还是 CEO。")
        message = self.fixture.message("走了好几天了")
        answers = ["他没走，仍然是 CEO。", "公司11月12日的公告确认他已卸任，财务主管暂代。"]

        class Judge:
            def noul(self, state, questions, **options):
                if "这轮要回答的问题" in state:
                    return [0.2, 0.1, 0.9]
                if "本轮公开资料" in state:
                    return [0.94 if "已卸任" in state["拟回答"] else 0.03]
                return [0.02] * len(questions)

        class Model:
            def complete_json(self, system, user, **options):
                payload = json.loads(user)
                if not payload["取材记录"]:
                    return images_test.lookup("search_public", query="公开公司 CEO 近日任免 公告")
                source = payload["取材记录"][0][0]["结果"]["资料"]["来源编号"]
                return {"依据": [source]}

            def complete_text(self, system, user, **options):
                return answers.pop(0)

        self.ctx._llm = Model()
        self.ctx._jev = Judge()
        with patch.object(MentionTools, "search", return_value=ToolResult("search", True,
                "公司官方新闻稿，2026-11-12，CEO已卸任，财务主管暂代。")) as search:
            self._reply(message, message.text)
        self.assertEqual(search.call_count, 1)
        self.assertEqual(answers, [])
        actual = "\n".join(text for _, text in self.fixture.backend.texts)
        self.assertIn("已卸任", actual)
        self.assertNotIn("他没走", actual)

    def test_disagreement_with_no_confirming_public_source_does_not_assert_a_status(self):
        class Model:
            def complete_json(self, system, user, **options):
                return {"依据": []}

        self.ctx._llm = Model()
        message = self.fixture.message("走了好几天了")
        with patch("plugins.mention_chat.material_needs", return_value=(True, False)):
            self._reply(message, message.text)
        self.assertEqual(self.fixture.backend.texts, [])

    def test_nonempty_old_search_results_do_not_confirm_current_status(self):
        self.plugin.tools = read_tool_options({})
        message = self.fixture.message("那个人是不是已经换工作了？")

        class Model:
            def complete_json(self, system, user, **options):
                trace = json.loads(user)["取材记录"]
                if not trace:
                    return images_test.lookup("search_public", query="公众人物 新任职")
                return {"依据": [trace[0][0]["结果"]["资料"]["来源编号"]]}

            def complete_text(self, system, user, **options):
                return "他仍在原岗位。"

        class Judge:
            def noul(self, state, questions, **options):
                return [0.02] * len(questions)

        self.ctx._llm = Model()
        self.ctx._jev = Judge()
        with (patch("plugins.mention_chat.material_needs", return_value=(True, False)),
              patch.object(MentionTools, "search", return_value=ToolResult("search", True, "2021 年的旧任职简介"))):
            self._reply(message, message.text)
        self.assertEqual(self.fixture.backend.texts, [])

    def test_chat_answer_can_search_older_firsthand_dialogue(self):
        self.fixture.members.register("阿青", ROOM, "wxid_qing")
        self.fixture.message("路线定了，走北门，西门取消", sender="wxid_qing")
        for index in range(14):
            self.fixture.message(f"午饭讨论 {index}", sender="wxid_other")
        message = self.fixture.message("阿青最后拍板的入口在哪？")

        class Model:
            def complete_json(self, system, user, **options):
                payload = json.loads(user)
                if not payload["取材记录"]:
                    return images_test.lookup("search_messages", keywords=["路线", "西门"], speaker="member")
                found = payload["取材记录"][0][0]["结果"]["消息"][0]
                return {"依据": [found["来源编号"]]}

            def complete_text(self, system, user, **options):
                return "阿青最后定的是北门。" if "走北门，西门取消" in user else "我没找到。"

        self.ctx._llm = Model()
        with patch("plugins.mention_chat.material_needs", return_value=(False, True)):
            self._reply(message, message.text)
        self.assertIn("阿青最后定的是北门。", self.fixture.backend.texts[-1][1])

    def test_preparation_timeout_leaves_firsthand_material_and_sixty_seconds_for_text(self):
        self.fixture.message("路线定了，走北门。", sender="wxid_other")
        for number in range(13):
            self.fixture.message(f"闲聊 {number}", sender="wxid_other")
        message = self.fixture.message("之前路线定在哪？")
        now = [0]
        budget = ReplyBudget(0, 90, clock=lambda: now[0])
        model = self.ctx._llm = Mock()

        def plan(system, user, **options):
            self.assertEqual(remaining(45, kind="llm"), 20)
            if not json.loads(user)["取材记录"]:
                return images_test.lookup("search_messages", keywords=["路线"])
            now[0] = 20
            raise TimeoutError()

        def answer(system, user, **options):
            self.assertEqual(remaining(60, kind="llm"), 60)
            self.assertIn("走北门", user)
            return "走北门。"

        model.complete_json.side_effect = plan
        model.complete_text.side_effect = answer
        token = CURRENT.set(budget)
        try:
            with patch("plugins.mention_chat.material_needs", return_value=(False, True)):
                self._reply(message, message.text)
        finally:
            CURRENT.reset(token)
        self.assertEqual(model.complete_text.call_count, 1)
        self.assertIn("走北门。", self.fixture.backend.texts[-1][1])

    def test_quoted_image_reaches_text_without_a_material_round_or_reply_review(self):
        self.plugin.tools = read_tool_options({})
        self.fixture.poster_fixture()
        self.ctx._llm = Mock()
        self.ctx._llm.complete_json.side_effect = AssertionError("短句引用图不需要先取材")
        self.ctx._llm.complete_text.return_value = "这张确实更像头像。"

        class Judge:
            def noul(self, state, questions, **options):
                assert "拟发送的回答" not in state
                return [0.01] * len(questions)

        self.ctx._jev = Judge()
        message = self.fixture.message("不如这个", data={"quote": {"ref_type": "3", "svrid": "912"}})
        self._reply(message, message.text)
        self.assertEqual([call["name"] for call in self.fixture.server.calls], ["analyze_image"])
        model = self.ctx._llm
        model.complete_json.assert_not_called()
        model.complete_text.assert_called_once()
        self.assertIn("左侧旧职业为软件开发", model.complete_text.call_args.args[1])
        self.assertIn("这张确实更像头像。", self.fixture.backend.texts[-1][1])

    def test_deadline_draft_registers_actual_send_and_can_be_followed_up(self):
        request = self.fixture.message("哪条路线？")
        turns = self.fixture.members.turns
        turn = turns.open(request, request.text, 0)
        waiter = ReplyWait(self.plugin._status)
        self.addCleanup(waiter.close)
        budget = waiter.accept(request, self.ctx, 90)
        budget.ready = ReplyDraft(0, "走北门。", None, lambda part: turns.sent(turn, part, allow_closed=True))
        turns.close(turn)
        waiter.finish(budget, "超时")
        next_message = self.fixture.message("那几点集合？")
        self.assertEqual(turns.continuation(next_message).text, "走北门。")
        row = self.fixture.members.connection().execute(
            "SELECT source_id,status,output FROM reply_receipts").fetchone()
        self.assertEqual(tuple(row), (self.fixture.members.message_id(request), "submitted", "走北门。"))

    def test_clear_waits_until_submitted_text_is_registered(self):
        request = self.fixture.message("先答这个问题")
        members = self.fixture.members
        snapshot = members.conversation_snapshot(request)
        entered, release, clear_finished = (threading.Event() for _ in range(3))
        original = self.fixture.backend.send_text

        def send(room, part):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("测试没有放行发送")
            return original(room, part)

        def clear():
            members.clear(ROOM)
            clear_finished.set()

        with patch.object(self.fixture.backend, "send_text", side_effect=send):
            worker = threading.Thread(target=lambda: self.ctx.reply(request, "实际发出的旧答复",
                delivery_guard=lambda: self.ctx.response_guard(snapshot, self.ctx.personality(request))))
            worker.start()
            self.assertTrue(entered.wait(1))
            reset = threading.Thread(target=clear)
            reset.start()
            self.assertFalse(clear_finished.wait(0.05))
            release.set()
            worker.join(2)
            reset.join(2)
        self.assertTrue(clear_finished.is_set())
        self.assertNotIn("实际发出的旧答复", [row["text"] for row in members.recent_rows(ROOM, context=True, include_bot=True)])
        stored = members.connection().execute("SELECT status,output FROM reply_receipts").fetchone()
        self.assertEqual(tuple(stored), ("submitted", "实际发出的旧答复"))

    def test_unconfirmed_send_never_enters_the_transcript_as_own_words(self):
        request = self.fixture.message("能回吗？")
        with patch.object(self.fixture.backend, "send_text", return_value=False):
            self.assertFalse(self.ctx.reply(request, "这段并没有收到发送确认"))
        members = self.fixture.members
        row = members.connection().execute("SELECT status,output FROM reply_receipts").fetchone()
        self.assertEqual(tuple(row), ("unconfirmed", ""))
        self.assertFalse(any(row["bot_generated"] for row in members.recent_rows(ROOM, include_bot=True)))

    def test_deadline_does_not_resend_a_draft_after_unconfirmed_submission(self):
        request = self.fixture.message("先回答")
        waiter = ReplyWait(self.plugin._status)
        self.addCleanup(waiter.close)
        budget = waiter.accept(request, self.ctx, 90)
        budget.ready = ReplyDraft(0, "实际只提交一次的正文", None)
        with patch.object(self.fixture.backend, "send_at_text", return_value=False) as send:
            self.assertFalse(self.ctx.reply_at(request, budget.ready.text, delivery_guard=budget.delivery))
            waiter.finish(budget, "超时")
            self.assertEqual(send.call_count, 1)
        stored = self.fixture.members.connection().execute(
            "SELECT status FROM reply_receipts WHERE source_id=? AND status='unconfirmed'",
            (self.fixture.members.message_id(request),)).fetchall()
        self.assertEqual(len(stored), 1)
