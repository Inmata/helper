"""群艾特的短回复偶尔使用语音。"""
from random import random
import re
from subprocess import SubprocessError
from time import monotonic

from wxbot_framework.integrations.voice_synth import synthesize_voice
from wxbot_framework.integrations.voice_xuanshen import XuanShenVoice

_REQUEST = re.compile(r"^(?:请)?(?:用语音(?:条)?(?:回|说)|语音回复)")
_AUTO_CHANCE = 0.05
_INTERVAL_SECONDS = 30 * 60
_MAX_CHARS = 40


def requests_voice(question):
    return bool(_REQUEST.match(question))


class VoiceReply:
    def __init__(self):
        self._next = {}
        self._xuanshen = None

    def start(self, logger):
        if XuanShenVoice.available():
            try:
                self._xuanshen = XuanShenVoice()
            except OSError as error:
                logger.warning("炫神音色未启动：%s", type(error).__name__)

    def close(self):
        if self._xuanshen is not None:
            self._xuanshen.close()
            self._xuanshen = None

    def send(self, message, ctx, question, reply, *, automatic, guard):
        """不选语音或合成失败返回 None；已提交则返回微信发送结果。"""
        if ("voice.send" not in getattr(ctx, "capabilities", ()) or not reply or
                len(reply) > _MAX_CHARS or "\n" in reply):
            return None
        room = message.chat_id
        now = monotonic()
        if not requests_voice(question) and (
                not automatic or now < self._next.get(room, 0) or random() >= _AUTO_CHANCE):
            return None
        self._next[room] = now + _INTERVAL_SECONDS
        wav, voice = None, "Windows"
        if self._xuanshen is not None:
            try:
                wav = self._xuanshen.synthesize(reply)
                if wav is not None:
                    voice = "炫神"
            except (OSError, ValueError) as error:
                ctx.logger.warning("炫神语音合成失败，改用 Windows 音色：%s", type(error).__name__)
                self.close()
        try:
            if wav is None:
                wav = synthesize_voice(reply)
        except (OSError, SubprocessError, ValueError) as error:
            ctx.logger.warning("本机语音合成失败，改发文字：%s", type(error).__name__)
            return None
        sent = ctx.reply_voice(message, wav, transcript=reply, delivery_guard=guard)
        ctx.logger.info("艾特语音回复：%s，音色=%s，合成到回执 %.2f 秒",
                        "已提交" if sent else "未确认", voice, monotonic() - now)
        return sent
