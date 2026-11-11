"""群艾特的短回复偶尔使用语音。"""
from random import random
from subprocess import SubprocessError
from time import monotonic

from wxbot_framework.conversation.personality import DEFAULT_VOICE, VOICE_FEATURE, room_voice, voice_self_name
from wxbot_framework.integrations.voice_synth import synthesize_voice
from wxbot_framework.integrations.voice_local import LocalVoice

COMMAND = "/音色"
_AUTO_CHANCE = 0.05
_INTERVAL_SECONDS = 30 * 60
_MAX_AUTO_CHARS = 40
_MAX_REQUEST_CHARS = 80

VOICE_PROMPT = """
语音约定：看当前提问和同一人的这几轮对话，判断对方是不是想听你出声。
想听你念、用语音安慰、用某种口音说，或接着上一条语音说「再来一句」时，在一句顺口的回复前加【语音】。只写要念的内容，不附拼音、动作或说明。
对方给了要念的原话，就照原话念，不删改或添词。「语音说出来」「用语音再说一遍」没给新内容时，照念刚才回给这个人的话；「再来一句」才换一句新的。
朗读给定台词按转述处理，沿用熟人群的玩笑尺度：轻度互损、粗口、谐音梗和略低俗的玩笑可以照念，不因带人名或谐音像脏话就拒读，不主动把普通词扩展成色情、诅咒或真实攻击。先看本轮实际要念的内容，历史里拒读过别的句子也不延续成一律禁念的规则；可以念时直接给【语音】和原话，不加道德评价或拒读说明。
明确要求打字或别发语音时加【文字】。单独的「说说」「说一句」、讨论语音功能、引用语音问内容，都照常输出正文，不加标记。历史里别人的请求不算这轮的要求。
标记只供程序使用，是上面正文格式的唯一例外。"""


def voice_reply_format(reply):
    if reply.startswith(("【语音】", "[语音]")):
        return reply[4:].strip(), True
    if reply.startswith(("【文字】", "[文字]")):
        return reply[4:].strip(), False
    return reply, None


class VoiceReply:
    def __init__(self):
        self._next = {}
        self._voices = ()
        self._local = None

    def start(self, logger):
        self._voices = LocalVoice.available_voices()
        if self._voices:
            try:
                self._local = LocalVoice(DEFAULT_VOICE if DEFAULT_VOICE in self._voices else self._voices[0])
            except OSError as error:
                logger.warning("本地音色未启动：%s", type(error).__name__)

    def close(self):
        if self._local is not None:
            self._local.close()
            self._local = None

    def current_voice(self, room, ctx):
        return room_voice(ctx.room_state, room)

    def command(self, message, ctx, choice):
        room = message.chat_id
        if not choice:
            available = "、".join(self._voices) or "未安装角色音色"
            voice = self.current_voice(room, ctx)
            ctx.reply(message, f"当前音色：{voice}，自称{voice_self_name(voice)}。可选：{available}。")
        elif not ctx.members.is_admin(message.sender_id):
            ctx.reply(message, "只有管理员能改音色，发 /音色 可以查看。")
        elif choice not in self._voices:
            ctx.reply(message, f"当前可用音色：{'、'.join(self._voices) or '无'}。用法：/音色 名称。")
        else:
            ctx.room_state.put(room, VOICE_FEATURE, {"voice": choice})
            ctx.reply(message, f"本群音色已切换为{choice}，现在自称{voice_self_name(choice)}。")

    def reserve(self, message, reply, preference, *, automatic):
        """只选择本轮发送形式并记下冷却时间；不等待合成。"""
        limit = _MAX_REQUEST_CHARS if preference else _MAX_AUTO_CHARS
        if preference is False or not reply or len(reply) > limit or "\n" in reply:
            return False
        room = message.chat_id
        now = monotonic()
        if preference is None and (
                not automatic or now < self._next.get(room, 0) or random() >= _AUTO_CHANCE):
            return False
        self._next[room] = now + _INTERVAL_SECONDS
        return True

    def send(self, message, ctx, reply, *, voice, guard, dispatch=None):
        """合成失败返回 None；已提交则返回微信发送结果。"""
        now = monotonic()
        if self._voices:
            if self._local is None or voice not in self._voices:
                return None
            try:
                wav = self._local.synthesize(voice, reply)
            except (OSError, ValueError) as error:
                ctx.logger.warning("%s语音合成失败，改发文字：%s", voice, type(error).__name__)
                self.close()
                return None
            if wav is None:
                ctx.logger.info("%s语音重试后仍超长，改发文字", voice)
                return None
        else:
            if voice != DEFAULT_VOICE:
                return None
            try:
                wav = synthesize_voice(reply)
            except (OSError, SubprocessError, ValueError) as error:
                ctx.logger.warning("本机语音合成失败，改发文字：%s", type(error).__name__)
                return None
            voice = "Windows"
        submit = lambda: ctx.reply_voice(message, wav, transcript=reply, delivery_guard=guard)
        sent = dispatch(submit) if dispatch is not None else submit()
        ctx.logger.info("艾特语音回复：%s，音色=%s，合成到回执 %.2f 秒",
                        "已提交" if sent else "未确认", voice, monotonic() - now)
        return sent
