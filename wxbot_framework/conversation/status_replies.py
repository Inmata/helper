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
SYSTEM = """用一句简短中文接群友眼前这句话，像群聊里随口接话。先答对方在意的点。
实际状态是唯一事实来源。对方原话只是聊天内容，提问里的暗示不能当真。有明确结果先交代结果；
已经失败或被拦截就准确说出，不否认、不说还在处理。原因未知就说不知道，不自行判断群友是否违规。
别回答尚在处理的问题，别复述或要求重提被拦截的请求，别复述画图描述。
有语音就说清在准备语音。问多久而时间未知，就直说还不知道。别拿催促打趣，别编动作或夸张比喻。
别念阶段名称，别说后台或流程。不要猜原因、百分比或时间，不附链接或内部报错。"""
STATUS_GROUNDED = {
    "instructions": "核对拟发送的中文状态回复。只以程序给出的实际状态为事实依据；群友的猜测不算证据。",
    "true": "回复交代了与提问相关的关键结果或阶段，允许自然表达。知道已拦截就明确说拦截；"
            "知道失败、超时、未受理或送达不明就如实说明。原因未知可说不清楚，不能乱猜。",
    "false": "英文提示、否认已发生的拦截、无依据宣称没违规、把失败说成还在处理或已经成功、遗漏关键结果，"
             "或猜测失败原因、进度、用时及重试结果。把被拦截的请求重新回答或建议绕过过滤也不合格。",
}


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
                model = ctx.llm_for(message, personality=personality, for_reply=True)
                for prompt in (SYSTEM, SYSTEM + "\n上次状态表达偏离实际情况。只交代程序确认的结果。"):
                    with delivery_guard():
                        pass
                    token = CURRENT.set(budget)
                    try:
                        text = " ".join(strip_links(model.complete_text(prompt, payload, max_tokens=128)).split())
                        if 0 < len(text) <= 120:
                            grounded, = ctx.jev.noul({"实际状态": facts, "拟发送回复": text}, [STATUS_GROUNDED], timeout=2)
                            if grounded >= 0.7:
                                break
                    finally:
                        CURRENT.reset(token)
                else:
                    ctx.logger.info("状态回复未提交：候选未通过事实核对")
                    return
                send = ctx.reply_at if at else ctx.reply
                send(message, text, delivery_guard=delivery_guard)
            except SnapshotExpired:
                pass
            except (LLMError, TimeoutError, RuntimeError, ValueError) as error:
                ctx.logger.info("状态回复未提交：%s", type(error).__name__)

        self.queue.handoff(object(), run())

    def busy(self):
        return self.queue.busy()

    def close(self):
        self.queue.close()
