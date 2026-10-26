"""验证同一提问生成中的重复艾特不另行生成，以及详细请求的预算传递。"""
import logging
import threading
import unittest
from unittest.mock import MagicMock, patch

from plugins.mention_chat import Plugin, wants_detail
from wxbot_framework.conversation.members import ConversationSnapshot
from wxbot_framework.integrations.llm import LLMError
from wxbot_framework.sdk import Message

ROOM = "12345678@chatroom"
OTHER_ROOM = "87654321@chatroom"
SENDER = "wxid_speaker"
OTHER_SENDER = "wxid_other"


class FakeClient:
    def __init__(self):
        self.calls = []

    def complete_text(self, system, user, *, history=(), max_tokens=None, detail=False):
        self.calls.append({"max_tokens": max_tokens, "detail": detail})
        return "回复正文"


class FakeDialogue:
    """按约定的 Dialogue.reply 接口：detail 为真才以 detail=True 调用 complete_text。"""

    def __init__(self, client, *, on_reply=None, error=None):
        self.client = client
        self.on_reply = on_reply
        self.error = error
        self.calls = []

    def reply(self, snapshot, *, detail=False, **options):
        self.calls.append({"detail": detail, "options": options})
        if self.on_reply is not None:
            self.on_reply()
        if self.error is not None:
            raise self.error
        return self.client.complete_text("系统", "提问", detail=detail)


def use_dialogue(dialogue):
    """替换插件内的 Dialogue 构造，隔离 W2 的实现进度。"""
    return patch("plugins.mention_chat.Dialogue", lambda client: dialogue)


def make_ctx(plugin, client):
    ctx = MagicMock()
    ctx.logger = logging.getLogger("MentionDedupeTests")
    ctx.members = plugin.members
    ctx.personality.return_value = MagicMock(name="人格")
    ctx.personality_guard.return_value = MagicMock()
    ctx.response_guard.return_value = MagicMock()
    ctx.knowledge.member_entity.return_value = {"id": 1}
    ctx.knowledge.search.return_value = []
    ctx.knowledge.lookup_people.return_value = []
    ctx.knowledge.style.return_value = None
    ctx.llm_for.return_value = client
    ctx.reply_at.return_value = True
    ctx.reply.return_value = True
    return ctx


def make_plugin():
    plugin = Plugin()
    plugin.members = MagicMock(account="wxid_robot", groups={ROOM, OTHER_ROOM})
    plugin.room_state = None
    plugin.members.conversation_snapshot.return_value = ConversationSnapshot(
        ROOM, "2026-10-26", 0, "小明", (), "提问", ())
    plugin.members.at_display_name.return_value = "小明"
    return plugin


def make_message(text="这个方案怎么落地", *, sender=SENDER, room=ROOM, received_at=None):
    extra = {} if received_at is None else {"received_at": received_at}
    return Message("@机器人 " + text, room, sender_id=sender, is_group=True,
                   data={"is_at_self": True, "at_members": ("wxid_robot",)}, **extra)


class FakeClock:
    """可控的 time.monotonic，由用例按处理阶段推进。"""

    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class DedupeTests(unittest.TestCase):
    def setUp(self):
        self.plugin = make_plugin()
        self.client = FakeClient()
        self.ctx = make_ctx(self.plugin, self.client)

    def test_same_question_during_generation_is_not_generated_twice(self):
        started, release = threading.Event(), threading.Event()
        dialogue = FakeDialogue(self.client, on_reply=lambda: (started.set(), release.wait(5)))
        with use_dialogue(dialogue):
            worker = threading.Thread(target=self.plugin.on_message, args=(make_message(), self.ctx))
            worker.start()
            self.assertTrue(started.wait(5), "首次生成没有开始")
            # 生成尚未结束，相同提问再次到达：不生成、不回复。
            self.plugin.on_message(make_message(), self.ctx)
            self.assertEqual(len(dialogue.calls), 1)
            self.ctx.reply_at.assert_not_called()
            release.set()
            worker.join(5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(len(dialogue.calls), 1)
        self.assertEqual(self.ctx.reply_at.call_count, 1)

    def test_question_queued_during_the_first_reply_is_skipped(self):
        # 生产分发线程串行处理消息：重复提问常在首次回复结束后才轮到，
        # 但它到达时首次回复尚未结束，同样不另行生成。
        first, second = make_message(), make_message()
        self.plugin.on_message(first, self.ctx)
        self.plugin.on_message(second, self.ctx)
        self.assertEqual(self.ctx.reply_at.call_count, 1)

    def test_same_question_asked_after_the_reply_generates_again(self):
        self.plugin.on_message(make_message(), self.ctx)
        self.plugin.on_message(make_message(), self.ctx)
        self.assertEqual(self.ctx.reply_at.call_count, 2)

    def test_different_speaker_or_question_is_not_affected(self):
        self.plugin.on_message(make_message(), self.ctx)
        self.plugin.on_message(make_message(sender=OTHER_SENDER), self.ctx)
        self.plugin.on_message(make_message("另一个问题"), self.ctx)
        self.assertEqual(self.ctx.reply_at.call_count, 3)

    def test_pending_state_is_cleared_after_an_error(self):
        failing = FakeDialogue(self.client, error=LLMError("大模型连接失败或超时"))
        with use_dialogue(failing), self.assertLogs("MentionDedupeTests", level="WARNING"):
            self.plugin.on_message(make_message(), self.ctx)
        self.ctx.reply.assert_called_once()
        self.assertIn("暂时无法回复", self.ctx.reply.call_args.args[1])
        self.assertEqual(self.plugin._pending, {})
        # 异常后立即重试会重新生成，而不是被当成仍在生成。
        working = FakeDialogue(self.client)
        with use_dialogue(working):
            self.plugin.on_message(make_message(), self.ctx)
        self.assertEqual(len(working.calls), 1)
        self.assertEqual(self.ctx.reply_at.call_count, 1)

    def test_detail_question_passes_detail_flag(self):
        self.assertFalse(wants_detail("这个方案怎么落地"))
        for query in ("详细说说这个方案", "展开讲讲", "给我完整方案", "请一步一步说明"):
            with self.subTest(query=query):
                self.assertTrue(wants_detail(query))
        dialogue = FakeDialogue(self.client)
        with use_dialogue(dialogue):
            self.plugin.on_message(make_message("详细说说这个方案"), self.ctx)
            self.plugin.on_message(make_message("随便聊聊"), self.ctx)
        self.assertTrue(dialogue.calls[0]["detail"])
        self.assertFalse(dialogue.calls[1]["detail"])
        self.assertEqual(self.ctx.reply_at.call_count, 2)

    def run_queued(self, duplicate_arrival):
        """首条 100 到达、排队到 120 才处理、130 回复完；再按 on_message 消费一条相同提问。"""
        clock = FakeClock(120)
        dialogue = FakeDialogue(self.client, on_reply=lambda: setattr(clock, "now", 130))
        with use_dialogue(dialogue), patch("plugins.mention_chat.time.monotonic", clock):
            self.plugin.on_message(make_message(received_at=100), self.ctx)
            clock.now = 131  # 重复提问在首次回复结束后才被分发线程取出。
            dialogue.on_reply = None
            self.plugin.on_message(make_message(received_at=duplicate_arrival), self.ctx)
        return dialogue

    def test_duplicate_arriving_during_queued_generation_is_skipped(self):
        dialogue = self.run_queued(125)
        self.assertEqual(len(dialogue.calls), 1)
        self.assertEqual(self.ctx.reply_at.call_count, 1)

    def test_duplicate_arriving_exactly_at_reply_end_is_skipped(self):
        dialogue = self.run_queued(130)
        self.assertEqual(len(dialogue.calls), 1)
        self.assertEqual(self.ctx.reply_at.call_count, 1)

    def test_question_asked_after_queued_reply_ended_generates_again(self):
        dialogue = self.run_queued(130.5)
        self.assertEqual(len(dialogue.calls), 2)
        self.assertEqual(self.ctx.reply_at.call_count, 2)


if __name__ == "__main__":
    unittest.main()
