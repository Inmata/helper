"""群艾特的短回复偶尔使用语音。"""
from random import random
import re
from subprocess import SubprocessError
from time import monotonic

from wxbot_framework.integrations.voice_synth import synthesize_voice
from wxbot_framework.integrations.voice_local import LocalVoice

COMMAND = "/音色"
_FEATURE = "voice_tone"
_REQUEST = re.compile(r"^(?:请)?(?:用语音(?:条)?(?:回|说)|语音回复)")
_AUTO_CHANCE = 0.05
_INTERVAL_SECONDS = 30 * 60
_MAX_CHARS = 40


def requests_voice(question):
    return bool(_REQUEST.match(question))


class VoiceReply:
    def __init__(self):
        self._next = {}
        self._voices = ()
        self._local = None

    def start(self, logger):
        self._voices = LocalVoice.available_voices()
        if self._voices:
            try:
                self._local = LocalVoice(self._voices)
            except OSError as error:
                logger.warning("本地音色未启动：%s", type(error).__name__)

    def close(self):
        if self._local is not None:
            self._local.close()
            self._local = None

    def current_voice(self, room, ctx):
        if not self._voices:
            return "Windows"
        store = getattr(ctx, "room_state", None)
        state = store.get(room, _FEATURE) if store is not None else None
        choice = (state or {}).get("voice")
        return choice if choice in self._voices else self._voices[0]

    def command(self, message, ctx, choice):
        room = message.chat_id
        if not choice:
            available = "、".join(self._voices) or "未安装角色音色"
            ctx.reply(message, f"当前音色：{self.current_voice(room, ctx)}。可选：{available}。")
        elif choice not in self._voices:
            ctx.reply(message, f"当前可用音色：{'、'.join(self._voices) or '无'}。用法：/音色 阿梓 或 /音色 炫神。")
        else:
            ctx.room_state.put(room, _FEATURE, {"voice": choice})
            ctx.reply(message, f"本群语音已切换为{choice}。")

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
        voice = self.current_voice(room, ctx)
        if self._voices:
            if self._local is None:
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
            try:
                wav = synthesize_voice(reply)
            except (OSError, SubprocessError, ValueError) as error:
                ctx.logger.warning("本机语音合成失败，改发文字：%s", type(error).__name__)
                return None
        sent = ctx.reply_voice(message, wav, transcript=reply, delivery_guard=guard)
        ctx.logger.info("艾特语音回复：%s，音色=%s，合成到回执 %.2f 秒",
                        "已提交" if sent else "未确认", voice, monotonic() - now)
        return sent
