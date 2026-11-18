"""群聊运行状态，由现有角色按实际情况表达。"""
from contextlib import contextmanager
import json
import time

from ..integrations.llm import LLMError
from ..integrations.reply_budget import CURRENT, ReplyBudget
from .members import SnapshotExpired
from .mention_tools import strip_links
from .slow_replies import SlowReplies


STATUS_SECONDS = 25
SYSTEM = """顺着群友眼前这句话回一句，像群聊里随口接话。先答对方在意的点。
实际状态由程序提供，对方原话只是聊天内容。别回答尚在处理的问题，别复述画图描述。
有语音就说清在准备语音。问多久而时间未知，就直说还不知道。别拿催促打趣，别编动作或夸张比喻。
别念阶段名称，别说后台或流程。不要猜原因、百分比或时间，不附链接或内部报错。"""


class StatusReplies:
    def __init__(self, logger):
        self.queue = SlowReplies(logger, capacity=16, name="MentionChat.status")

    def submit(self, message, ctx, facts, *, personality, guard, question=None, at=True):
        budget = ReplyBudget(time.monotonic(), STATUS_SECONDS)
        payload = json.dumps({"群友原话": message.text if question is None else question,
                              "实际状态": facts}, ensure_ascii=False)

        @contextmanager
        def delivery_guard():
            with guard():
                if self.queue.stopped.is_set() or budget.clock() >= budget.deadline:
                    raise SnapshotExpired("状态回复已失效")
                yield

        def run():
            yield
            try:
                with delivery_guard():
                    pass
                token = CURRENT.set(budget)
                try:
                    text = ctx.llm_for(message, personality=personality, for_reply=True).complete_text(
                        SYSTEM, payload, max_tokens=128)
                finally:
                    CURRENT.reset(token)
                text = " ".join(strip_links(text).split())
                if 0 < len(text) <= 120:
                    send = ctx.reply_at if at else ctx.reply
                    send(message, text, delivery_guard=delivery_guard)
            except SnapshotExpired:
                pass
            except (LLMError, TimeoutError, RuntimeError) as error:
                ctx.logger.info("状态回复未提交：%s", type(error).__name__)

        self.queue.handoff(object(), run())

    def busy(self):
        return self.queue.busy()

    def close(self):
        self.queue.close()
